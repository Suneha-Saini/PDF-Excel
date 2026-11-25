"""Per-client settings and how they drive the Setup checks. Run: python -m unittest discover -s tests -v"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

from app.api.endpoints import client_settings as settings_api
from app.api.endpoints import ledger as ledger_api
from app.database import connection as dbconn
from app.database.models import AuditLog, LedgerRun, User
from app.security import auth
from app.services import client_settings as cs
from app.services import providers as prov
from app.services.coding import setup_issues as si
from app.services.providers.base import LedgerProvider

ACCOUNTS = [
    {"code": "310", "name": "Contract Installations", "class": "EXPENSE"},
    {"code": "404", "name": "Bank Fees", "class": "EXPENSE"},
    {"code": "880", "name": "Owner A Drawings", "class": "EQUITY"},
    {"code": "881", "name": "Dividends Paid", "class": "EQUITY"},
    {"code": "882", "name": "Director's Loan", "class": "LIABILITY"},
]
_n = 0


def line(code, amount, tax="__absent__", contact="Acme"):
    global _n
    _n += 1
    it = {"id": f"l{_n}", "description": "payment", "contact": contact, "reference": "", "amount": amount, "current_code": code, "date": "2026-03-01"}
    if tax != "__absent__":
        it["tax_code"] = tax
    return it


def many(n, *a, **kw):
    return [line(*a, **kw) for _ in range(n)]


def run(lines, **settings):
    return si.analyse(lines, ACCOUNTS, settings or None)


def kinds(r, severity=None):
    return sorted(i["kind"] for i in r["issues"] if severity in (None, i["severity"]))


class Service(unittest.TestCase):
    def setUp(self):
        self.eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.eng)
        self.db = Session(self.eng)
        self.addCleanup(self.db.close)
        u = User(username="u", hashed_password="x")
        self.db.add(u)
        self.db.commit()
        self.db.refresh(u)
        self.uid = u.id

    def test_everything_defaults_to_unknown_nothing_is_assumed(self):
        self.assertEqual(cs.get(self.db, self.uid, "xero", "C1"),
                         {"entity_type": "unknown", "gst_registration": "unknown", "bas_frequency": "unknown"})
        self.assertEqual(cs.get(self.db, self.uid, "xero", None)["gst_registration"], "unknown")

    def test_save_updates_only_the_fields_given_and_is_per_client(self):
        cs.save(self.db, self.uid, "xero", "C1", {"entity_type": "company", "gst_registration": "cash"})
        cs.save(self.db, self.uid, "xero", "C1", {"gst_registration": "not_registered"})
        cs.save(self.db, self.uid, "xero", "C2", {"entity_type": "trust"})
        self.db.commit()
        c1, c2 = cs.get(self.db, self.uid, "xero", "C1"), cs.get(self.db, self.uid, "xero", "C2")
        self.assertEqual((c1["entity_type"], c1["gst_registration"], c1["bas_frequency"]), ("company", "not_registered", "unknown"))
        self.assertEqual((c2["entity_type"], c2["gst_registration"]), ("trust", "unknown"))
        self.assertIn("updated_at", c1)

    def test_invalid_values_and_unknown_fields_are_refused(self):
        with self.assertRaisesRegex(cs.InvalidSetting, "not a valid entity type"):
            cs.validate({"entity_type": "llc"})
        with self.assertRaisesRegex(cs.InvalidSetting, "Unknown setting"):
            cs.validate({"favourite_colour": "blue"})
        self.assertEqual(cs.validate({"bas_frequency": "quarterly"}), {"bas_frequency": "quarterly"})

    def test_every_setting_says_what_it_changes(self):
        self.assertEqual(set(cs.EFFECTS), set(cs.CHOICES))


class DrivesSetupChecks(unittest.TestCase):
    def test_not_registered_for_gst_flags_every_gst_code_as_high(self):
        r = run(many(4, "310", -100.0, "INPUT2") + many(3, "310", 100.0, "EXEMPTINPUT") + many(2, "310", -50.0, None), gst_registration="not_registered")
        high = [i for i in r["issues"] if i["kind"] == "gst_not_registered"]
        self.assertEqual([(i["severity"], i["times"], i["tax"]) for i in high], [("High", 4, "GST on Expenses")])    # free / none are fine
        self.assertIn("not registered for GST", high[0]["issue"])

    def test_unknown_or_registered_clients_never_get_that_flag(self):
        lines = many(4, "310", -100.0, "INPUT2")
        self.assertNotIn("gst_not_registered", kinds(run(lines)))
        self.assertNotIn("gst_not_registered", kinds(run(lines, gst_registration="cash")))

    def test_registered_turns_the_gst_free_account_check_on_even_if_codes_look_unfamiliar(self):
        lines = many(4, "404", -12.0, "TAXCODE-7")                                   # a code the heuristic can't read as GST
        self.assertFalse(run(lines)["coverage"]["gst_checks_ran"])
        r = run(lines, gst_registration="accrual")
        self.assertTrue(r["coverage"]["gst_checks_ran"])                            # context is now the accountant's word

    def test_not_registered_turns_the_gst_heuristic_off(self):
        r = run(many(5, "404", -12.0, "INPUT2"), gst_registration="not_registered")
        self.assertNotIn("gst_on_gst_free_account", kinds(r))
        self.assertFalse(r["coverage"]["gst_checks_ran"])

    def test_registered_gst_clients_still_get_the_gst_free_account_check(self):
        r = run(many(5, "404", -12.0, "INPUT2"), gst_registration="cash")
        self.assertIn("gst_on_gst_free_account", kinds(r, "Medium"))

    def test_drawings_on_a_company_and_dividends_on_a_sole_trader(self):
        lines = many(3, "880", -500.0, contact="Owner") + many(3, "881", -900.0, contact="Owner") + many(2, "882", -300.0, contact="Owner")
        company = [i["account_name"] for i in run(lines, entity_type="company")["issues"] if i["kind"] == "entity_account"]
        self.assertEqual(company, ["Owner A Drawings"])
        sole = sorted(i["account_name"] for i in run(lines, entity_type="sole_trader")["issues"] if i["kind"] == "entity_account")
        self.assertEqual(sole, ["Director's Loan", "Dividends Paid"])
        self.assertIn("director loan or dividend", next(i for i in run(lines, entity_type="company")["issues"] if i["kind"] == "entity_account")["issue"])

    def test_no_entity_checks_when_the_entity_is_unknown_or_the_account_fits(self):
        lines = many(3, "880", -500.0) + many(3, "881", -900.0)
        self.assertNotIn("entity_account", kinds(run(lines)))                                       # unknown: nothing assumed
        self.assertNotIn("entity_account", kinds(run(lines, entity_type="trust")))                   # trusts/SMSFs are not guessed at
        self.assertEqual([i["kind"] for i in run(many(3, "880", -500.0), entity_type="partnership")["issues"] if i["kind"] == "entity_account"], [])

    def test_the_report_says_which_settings_it_used(self):
        r = run(many(2, "310", -10.0), gst_registration="cash", entity_type="company")
        self.assertEqual((r["coverage"]["gst_registration"], r["coverage"]["entity_type"]), ("cash", "company"))

    def test_bas_frequency_changes_nothing_yet(self):
        lines = many(4, "310", -100.0, "INPUT2") + many(3, "880", -500.0)
        self.assertEqual(run(lines, bas_frequency="monthly")["issues"], run(lines)["issues"])


# ---------------------------------------------------------------- API
class FakeSystem(LedgerProvider):
    key, label = "xero", "Xero"

    def list_clients(self): return [{"id": "C1", "name": "Acme"}]
    def default_client_id(self): return "C1"
    def list_accounts(self, client_id): return ACCOUNTS
    def fetch_ledger(self, *a, **k): return [], {}
    def apply_changes(self, *a, **k): return {}


class Api(unittest.TestCase):
    def setUp(self):
        self.eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.eng)
        with Session(self.eng) as s:
            u = User(username="u", hashed_password="x")
            s.add(u)
            s.commit()
            s.refresh(u)
            self.uid = u.id
            items = many(4, "310", -100.0, "INPUT2")
            for it in items:
                it.update(status="ok", source=None, proposed_code=None)
            s.add(LedgerRun(user_id=self.uid, provider="xero", client_id="C1", organization="Acme", date_from="a", date_to="b",
                            items=items, accounts=ACCOUNTS))
            s.commit()
        app = FastAPI()
        app.include_router(settings_api.router, prefix="/client-settings")
        app.include_router(ledger_api.router, prefix="/ledger")

        def test_db():
            with Session(self.eng) as s:
                yield s

        app.dependency_overrides[dbconn.get_db] = test_db
        app.dependency_overrides[auth.get_current_active_user] = lambda: SimpleNamespace(id=self.uid)
        self.http = TestClient(app)
        overview = [{"key": "xero", "label": "Xero", "status": "connected", "ready": True, "verified": True, "supports_apply": True, "note": ""}]
        for p in (mock.patch.object(prov, "provider_overview", return_value=overview), mock.patch.object(prov, "provider_for", return_value=FakeSystem())):
            p.start()
            self.addCleanup(p.stop)

    P = {"provider": "xero", "client_id": "C1"}

    def test_get_returns_defaults_choices_and_what_each_setting_does(self):
        b = self.http.get("/client-settings", params=self.P).json()
        self.assertEqual(b["settings"]["gst_registration"], "unknown")
        self.assertEqual({c["value"] for c in b["choices"]["gst_registration"]}, {"unknown", "not_registered", "cash", "accrual"})
        self.assertIn("flagged as High", b["effects"]["gst_registration"])

    def test_put_saves_partial_updates_audits_them_and_validates(self):
        r = self.http.put("/client-settings", json={**self.P, "entity_type": "company", "gst_registration": "cash"})
        self.assertEqual((r.status_code, r.json()["settings"]["entity_type"]), (200, "company"))
        self.http.put("/client-settings", json={**self.P, "bas_frequency": "quarterly"})
        s = self.http.get("/client-settings", params=self.P).json()["settings"]
        self.assertEqual((s["entity_type"], s["gst_registration"], s["bas_frequency"]), ("company", "cash", "quarterly"))
        with Session(self.eng) as db:
            self.assertEqual(db.query(AuditLog).filter(AuditLog.action == "client_settings").count(), 2)
        self.assertEqual(self.http.put("/client-settings", json={**self.P, "entity_type": "llc"}).status_code, 400)
        self.assertEqual(self.http.put("/client-settings", json=self.P).status_code, 400)                                   # nothing to change
        self.assertEqual(self.http.put("/client-settings", json={**self.P, "client_id": "NOT-MINE", "entity_type": "company"}).status_code, 400)

    def test_changing_a_setting_changes_the_setup_issues_the_client_sees(self):
        before = self.http.get("/ledger/setup-issues", params=self.P).json()
        self.assertEqual(before["summary"]["High"], 0)
        self.http.put("/client-settings", json={**self.P, "gst_registration": "not_registered"})
        after = self.http.get("/ledger/setup-issues", params=self.P).json()
        self.assertEqual(after["summary"]["High"], 1)
        self.assertEqual(after["issues"][0]["kind"], "gst_not_registered")
        self.assertEqual(after["coverage"]["gst_registration"], "not_registered")


if __name__ == "__main__":
    unittest.main()
