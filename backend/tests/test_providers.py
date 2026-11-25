"""Run from the backend folder:  python -m unittest discover -s tests -v

Nothing here touches the real database or any real accounting system: APIs are faked and the DB is in-memory.
"""
import datetime
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

from app.api.endpoints import ledger as ledger_api
from app.database import connection as dbconn
from app.database.models import CodingRule, LedgerRun, User
from app.security import auth
from app.services import ledger_coding as lc
from app.services import providers as prov
from app.services.providers.base import LedgerProvider, ProviderError
from app.services.providers.quickbooks_provider import QuickBooksLedgerProvider, build_items
from app.services.quickbooks_service import QuickBooksService


def memory_engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


# ---------------------------------------------------------------- sample QuickBooks data
PURCHASES = [
    {"Id": "10", "SyncToken": "2", "TxnDate": "2026-01-15", "PaymentType": "CreditCard",
     "AccountRef": {"value": "41", "name": "Visa"}, "EntityRef": {"name": "Bunnings"}, "DocNumber": "R1",
     "Line": [
         {"Amount": 80.0, "Description": "Timber", "DetailType": "AccountBasedExpenseLineDetail",
          "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "7", "name": "Repairs"}}},
         {"Amount": 20.0, "Description": "Product line", "DetailType": "ItemBasedExpenseLineDetail",
          "ItemBasedExpenseLineDetail": {"ItemRef": {"value": "3"}}},
     ]},
    {"Id": "11", "SyncToken": "0", "TxnDate": "2026-01-20", "PaymentType": "Cash", "Credit": True,
     "AccountRef": {"value": "42"}, "Line": [
         {"Amount": 5.5, "DetailType": "AccountBasedExpenseLineDetail",
          "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "7"}}}]},
]
DEPOSITS = [
    {"Id": "20", "SyncToken": "1", "TxnDate": "2026-01-18", "DepositToAccountRef": {"value": "41", "name": "Cheque"},
     "Line": [
         {"Amount": 300.0, "Description": "Sales", "DetailType": "DepositLineDetail",
          "DepositLineDetail": {"AccountRef": {"value": "9", "name": "Sales"}, "Entity": {"name": "Acme"}}},
         {"Amount": 120.0, "DetailType": "DepositLineDetail", "LinkedTxn": [{"TxnId": "99", "TxnType": "Payment"}],
          "DepositLineDetail": {"AccountRef": {"value": "9"}}},
     ]},
]


class FakeQB(QuickBooksService):
    """A QuickBooksService whose HTTP layer is replaced; records every call."""
    def __init__(self, responder):
        super().__init__({"realm_id": "R1", "company_name": "Acme Co", "access_token": "x", "refresh_token": "y"})
        self.calls, self._responder = [], responder

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self._responder(method, path, kwargs)


def qb_provider(service):
    p = QuickBooksLedgerProvider(db=None, user=None, service=service)
    return p


class QuickBooksMapping(unittest.TestCase):
    def test_purchases_and_deposits_become_signed_lines_with_locked_ones(self):
        items, cache = build_items(PURCHASES, DEPOSITS)
        by_id = {i["id"]: i for i in items}

        spend = by_id["Purchase:10:0"]
        self.assertEqual((spend["amount"], spend["direction"], spend["current_code"], spend["status"]),
                         (-80.0, "spend", "7", "unassigned"))
        self.assertEqual((spend["contact"], spend["bank_account"], spend["reference"]), ("Bunnings", "Visa", "R1"))

        self.assertEqual(by_id["Purchase:10:1"]["status"], "locked")          # item-based line
        self.assertEqual(by_id["Purchase:11:0"]["amount"], 5.5)               # vendor credit = money in
        self.assertEqual(by_id["Deposit:20:0"]["amount"], 300.0)
        self.assertEqual(by_id["Deposit:20:0"]["contact"], "Acme")
        self.assertEqual(by_id["Deposit:20:1"]["status"], "locked")           # bundled customer payment
        self.assertEqual(len(items), 5)
        self.assertEqual(cache["Purchase:10"]["SyncToken"], "2")
        self.assertEqual(cache["Purchase:10"]["Required"]["PaymentType"], "CreditCard")

    def test_ledger_checks_tie_out_with_locked_lines(self):
        items, _ = build_items(PURCHASES, DEPOSITS)
        accounts = [{"code": "7", "name": "Repairs", "class": "EXPENSE"}, {"code": "9", "name": "Sales", "class": "REVENUE"}]
        lc.apply_rules(items, [{"id": 1, "name": "r", "keywords": "timber", "direction": "any", "account_code": "7",
                                "priority": 1, "active": True}])
        lc.refresh(items, accounts)
        report = lc.run_checks(items, accounts, system="QuickBooks Online")
        names = [c["name"] for c in report["checks"]]
        self.assertIn("All account codes exist in QuickBooks Online", names)
        self.assertAlmostEqual(report["totals"]["net"], sum(i["amount"] for i in items), places=2)


class QuickBooksIO(unittest.TestCase):
    def test_query_all_pages_until_a_short_page(self):
        pages = [[{"Id": str(i)} for i in range(1000)], [{"Id": "x"}]]

        def respond(method, path, kwargs):
            return {"status": 200, "json": {"QueryResponse": {"Purchase": pages.pop(0)}}}

        svc = FakeQB(respond)
        self.assertEqual(len(svc.query_all("Purchase", "where TxnDate >= '2026-01-01'")), 1001)
        self.assertIn("startposition%201001", svc.calls[1][1])

    def test_a_400_fault_becomes_a_readable_error(self):
        svc = FakeQB(lambda *a: {"status": 400, "json": {"Fault": {"Error": [{"Message": "bad query", "Detail": "x"}]}}})
        with self.assertRaisesRegex(Exception, "bad query"):
            svc.query_all("Purchase")

    def test_apply_sends_a_sparse_update_changing_only_the_chosen_line(self):
        svc = FakeQB(lambda *a: {"status": 200, "json": {"Purchase": {"Id": "10"}}})
        _, cache = build_items(PURCHASES, DEPOSITS)
        changes = [{"tx_id": "Purchase:10", "line_index": 0, "proposed_code": "55"}]
        results = qb_provider(svc).apply_changes("R1", changes, cache)

        self.assertEqual(results, {"Purchase:10": None})
        method, path, kwargs = svc.calls[0]
        body = kwargs["json"]
        self.assertEqual((method, path), ("POST", "/purchase"))
        self.assertTrue(body["sparse"])
        self.assertEqual((body["Id"], body["SyncToken"], body["PaymentType"]), ("10", "2", "CreditCard"))
        self.assertEqual(body["Line"][0]["AccountBasedExpenseLineDetail"]["AccountRef"], {"value": "55"})
        self.assertIn("ItemBasedExpenseLineDetail", body["Line"][1])           # untouched line preserved
        # the cache (what the run stored) is not mutated by building the request
        self.assertEqual(cache["Purchase:10"]["Lines"][0]["AccountBasedExpenseLineDetail"]["AccountRef"]["value"], "7")

    def test_apply_reports_per_transaction_failures(self):
        svc = FakeQB(lambda *a: {"status": 400, "json": {"Fault": {"Error": [{"Message": "Stale object", "Detail": ""}]}}})
        _, cache = build_items(PURCHASES, DEPOSITS)
        results = qb_provider(svc).apply_changes(
            "R1", [{"tx_id": "Deposit:20", "line_index": 0, "proposed_code": "3"}], cache)
        self.assertIn("Stale object", results["Deposit:20"])
        self.assertEqual(svc.calls[0][1], "/deposit")

    def test_wrong_company_is_refused(self):
        svc = FakeQB(lambda *a: {"status": 200, "json": {}})
        with self.assertRaises(ProviderError):
            qb_provider(svc).fetch_ledger("SOMEONE-ELSE", datetime.date(2026, 1, 1), datetime.date(2026, 1, 31))

    def test_accounts_exclude_payment_sources_and_use_ids_as_codes(self):
        raw = [{"Id": "7", "Name": "Repairs", "FullyQualifiedName": "Expenses:Repairs", "AcctNum": "6100",
                "AccountType": "Expense", "Classification": "Expense"},
               {"Id": "41", "Name": "Visa", "AccountType": "Credit Card"},
               {"Id": "42", "Name": "Cheque", "AccountType": "Bank"}]
        svc = FakeQB(lambda *a: {"status": 200, "json": {"QueryResponse": {"Account": raw}}})
        self.assertEqual(qb_provider(svc).list_accounts("R1"),
                         [{"code": "7", "name": "6100 Expenses:Repairs", "type": "Expense", "class": "Expense"}])


class Migration(unittest.TestCase):
    def test_adds_missing_columns_without_losing_rows(self):
        eng = memory_engine()
        with eng.begin() as c:
            c.execute(text("create table ledger_runs (id varchar primary key, user_id varchar, date_from varchar)"))
            c.execute(text("create table coding_rules (id integer primary key, name varchar)"))
            c.execute(text("insert into ledger_runs values ('a','u','2026-01-01')"))
            c.execute(text("insert into coding_rules values (1,'rent')"))
        dbconn.migrate_db(eng)
        dbconn.migrate_db(eng)  # idempotent
        with eng.begin() as c:
            self.assertEqual(c.execute(text("select provider, client_id from ledger_runs")).one(), ("xero", None))
            self.assertEqual(c.execute(text("select name, provider from coding_rules")).one(), ("rent", "xero"))


class RecoverInterruptedRuns(unittest.TestCase):
    def test_orphaned_runs_are_recovered_and_finished_ones_left_alone(self):
        eng = memory_engine()
        SQLModel.metadata.create_all(eng)
        with Session(eng) as s:
            u = User(username="u", hashed_password="x")
            s.add(u)
            s.commit()
            s.refresh(u)

            def run(status, items):
                r = LedgerRun(user_id=u.id, date_from="a", date_to="b", status=status, items=items, accounts=[])
                s.add(r)
                s.commit()
                return r.id

            ids = {"fetching": run("fetching", None), "categorising_empty": run("categorising", None),
                   "categorising_data": run("categorising", [{"id": "x"}]), "applying": run("applying", [{"id": "x"}]),
                   "ready": run("ready", [{"id": "x"}]), "applied": run("applied", [{"id": "x"}]), "failed": run("failed", None)}

        self.assertEqual(dbconn.recover_interrupted_runs(eng), 4)
        self.assertEqual(dbconn.recover_interrupted_runs(eng), 0)               # idempotent
        with Session(eng) as s:
            st = {k: s.get(LedgerRun, v) for k, v in ids.items()}
            self.assertEqual([st[k].status for k in ("fetching", "categorising_empty")], ["failed", "failed"])
            self.assertIn("Fetch again", st["fetching"].error)
            self.assertEqual((st["categorising_data"].status, st["categorising_data"].items), ("ready", [{"id": "x"}]))
            self.assertEqual(st["applying"].status, "ready")
            self.assertIn("re-applying is safe", st["applying"].error)
            self.assertEqual((st["ready"].status, st["applied"].status, st["failed"].status), ("ready", "applied", "failed"))


# ---------------------------------------------------------------- API
class FakeProvider(LedgerProvider):
    key, label = "quickbooks", "QuickBooks Online"
    supports_apply = True

    def list_clients(self):
        return [{"id": "R1", "name": "Acme Co"}, {"id": "R2", "name": "Beta Co"}]

    def default_client_id(self):
        return "R1"

    def list_accounts(self, client_id):
        return [{"code": "7", "name": "Repairs"}]

    def fetch_ledger(self, *a, **k):
        return [], {}

    def apply_changes(self, *a, **k):
        return {}


class LedgerApi(unittest.TestCase):
    def setUp(self):
        self.engine = memory_engine()
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            self.user = User(username="u", hashed_password="x")
            s.add(self.user)
            s.commit()
            s.refresh(self.user)
            self.user_id = self.user.id

        app = FastAPI()
        app.include_router(ledger_api.router, prefix="/ledger")
        def test_db():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[dbconn.get_db] = test_db
        app.dependency_overrides[auth.get_current_active_user] = lambda: self.user
        self.client = TestClient(app)

        # A run must never start a real background job here (it would use the real database).
        patcher = mock.patch.object(ledger_api, "_run_job", lambda run_id: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def overview(self, ready):
        return [{"key": k, "label": prov.LABELS[k], "status": "connected" if k in ready else "not_configured",
                 "ready": k in ready, "verified": k == "xero", "supports_apply": True, "note": ""} for k in prov.ORDER]

    def test_providers_endpoint_lists_all_four_systems(self):
        r = self.client.get("/ledger/providers")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([p["key"] for p in r.json()], ["xero", "quickbooks", "manager_cloud", "manager_local"])
        self.assertFalse(next(p for p in r.json() if p["key"] == "quickbooks")["verified"])

    def test_cannot_start_a_run_on_a_system_that_is_not_connected(self):
        with mock.patch.object(prov, "provider_overview", return_value=self.overview(ready={"xero"})):
            r = self.client.post("/ledger/runs", json={"date_from": "2026-01-01", "date_to": "2026-01-31", "provider": "quickbooks"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not connected", r.json()["detail"])

    def test_run_is_tied_to_the_chosen_provider_and_client(self):
        with mock.patch.object(prov, "provider_overview", return_value=self.overview(ready={"quickbooks"})), \
             mock.patch.object(prov, "provider_for", return_value=FakeProvider()):
            r = self.client.post("/ledger/runs", json={"date_from": "2026-01-01", "date_to": "2026-01-31",
                                                       "provider": "quickbooks", "client_id": "R2"})
            self.assertEqual(r.status_code, 200, r.text)
            run = self.client.get(f"/ledger/runs/{r.json()['id']}").json()
        self.assertEqual((run["provider"], run["client_id"], run["organization"], run["provider_label"]),
                         ("quickbooks", "R2", "Beta Co", "QuickBooks Online"))

    def test_a_client_the_login_does_not_have_is_rejected(self):
        with mock.patch.object(prov, "provider_overview", return_value=self.overview(ready={"quickbooks"})), \
             mock.patch.object(prov, "provider_for", return_value=FakeProvider()):
            r = self.client.post("/ledger/runs", json={"date_from": "2026-01-01", "date_to": "2026-01-31",
                                                       "provider": "quickbooks", "client_id": "NOT-MINE"})
        self.assertEqual(r.status_code, 400)

    def test_default_client_is_used_when_none_is_sent(self):
        with mock.patch.object(prov, "provider_overview", return_value=self.overview(ready={"quickbooks"})), \
             mock.patch.object(prov, "provider_for", return_value=FakeProvider()):
            r = self.client.post("/ledger/runs", json={"date_from": "2026-01-01", "date_to": "2026-01-31", "provider": "quickbooks"})
            run = self.client.get(f"/ledger/runs/{r.json()['id']}").json()
        self.assertEqual(run["client_id"], "R1")

    def test_rules_are_scoped_by_provider_and_client(self):
        def rule(name, provider, client):
            return {"name": name, "keywords": "k", "account_code": "1", "provider": provider, "client_id": client}
        for body in [rule("xero-all", "xero", None), rule("qb-all", "quickbooks", None),
                     rule("qb-acme", "quickbooks", "R1"), rule("qb-beta", "quickbooks", "R2")]:
            self.assertEqual(self.client.post("/ledger/rules", json=body).status_code, 200)

        names = lambda **q: sorted(r["name"] for r in self.client.get("/ledger/rules", params=q).json())
        self.assertEqual(names(provider="xero"), ["xero-all"])
        self.assertEqual(names(provider="quickbooks", client_id="R1"), ["qb-acme", "qb-all"])
        self.assertEqual(names(provider="quickbooks", client_id="R2"), ["qb-all", "qb-beta"])
        self.assertEqual(self.client.post("/ledger/rules", json=rule("x", "nope", None)).status_code, 400)

    def test_existing_rules_keep_working_as_xero_rules(self):
        with Session(self.engine) as s:
            s.add(CodingRule(user_id=self.user_id, name="old", keywords="rent", account_code="400"))
            s.commit()
        self.assertEqual([r["name"] for r in self.client.get("/ledger/rules").json()], ["old"])

    def test_apply_is_refused_where_write_back_is_unsupported(self):
        class ReadOnly(FakeProvider):
            supports_apply = False
        with Session(self.engine) as s:
            run = LedgerRun(user_id=self.user_id, provider="manager_cloud", client_id="b", organization="b",
                            date_from="2026-01-01", date_to="2026-01-31", status="ready", items=[], accounts=[])
            s.add(run)
            s.commit()
            rid = str(run.id)
        with mock.patch.object(prov, "provider_for", return_value=ReadOnly()):
            r = self.client.post(f"/ledger/runs/{rid}/apply")
        self.assertEqual(r.status_code, 409)
        self.assertIn("not supported", r.json()["detail"])


class RunJob(unittest.TestCase):
    """Regression: choosing an AI model must not clobber the accounting-system object used afterwards."""

    def test_run_with_an_ai_provider_completes_and_names_the_accounting_system(self):
        from types import SimpleNamespace
        eng = memory_engine()
        SQLModel.metadata.create_all(eng)
        with Session(eng) as s:
            user = User(username="u", hashed_password="x")
            s.add(user)
            s.commit()
            s.refresh(user)
            run = LedgerRun(user_id=user.id, provider="quickbooks", client_id="R1", organization="Acme Co",
                            date_from="2026-01-01", date_to="2026-01-31", ai_provider="Groq")
            s.add(run)
            s.commit()
            run_id = run.id

        ai_model = SimpleNamespace(name="Groq")                      # deliberately has no .label, like GroqProvider
        with mock.patch.object(ledger_api, "engine", eng), \
             mock.patch.object(prov, "provider_for", return_value=FakeProvider()), \
             mock.patch.object(ledger_api, "get_provider", return_value=ai_model), \
             mock.patch.object(lc, "ai_categorise", return_value=0) as ai:
            ledger_api._run_job(run_id)

        with Session(eng) as s:
            run = s.get(LedgerRun, run_id)
            self.assertEqual((run.status, run.error), ("ready", None))
            from app.database.models import AuditLog
            self.assertIn("QuickBooks Online / Acme Co", s.query(AuditLog).one().details)
        self.assertIs(ai.call_args.args[2], ai_model)                 # the AI step got the AI model


class XeroProviderTokens(unittest.TestCase):
    def test_refreshing_for_one_client_never_changes_the_saved_default_organisation(self):
        from app.services import connection_store as store
        from app.services.providers.xero_provider import XeroLedgerProvider

        eng = memory_engine()
        SQLModel.metadata.create_all(eng)
        with Session(eng) as db:
            user = User(username="u", hashed_password="x")
            db.add(user)
            db.commit()
            db.refresh(user)
            store.save(db, user, "xero", {"tenant_id": "ORG-A", "tenant_name": "Org A"},
                       {"client_id": "c", "client_secret": "s", "access_token": "a1", "refresh_token": "r1"})

            svc = XeroLedgerProvider(db, user)._service("ORG-B")
            self.assertEqual(svc.tenant_id, "ORG-B")
            svc.tenant_name = "Org B"
            svc._store_tokens({"access_token": "a2", "refresh_token": "r2", "expires_in": 1800})

            saved = store.resolve(db, user, "xero")
            self.assertEqual((saved["tenant_id"], saved["tenant_name"]), ("ORG-A", "Org A"))
            self.assertEqual((saved["access_token"], saved["refresh_token"]), ("a2", "r2"))  # tokens did persist


if __name__ == "__main__":
    unittest.main()
