"""Tax backfill. Run: python -m unittest discover -s tests -v  (from backend/)"""
import copy
import datetime
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

from app.api.endpoints import ledger as ledger_api
from app.database import connection as dbconn
from app.database.models import AuditLog, LedgerRun, User
from app.security import auth
from app.services import providers as prov
from app.services import tax_backfill as tb
from app.services.coding import setup_issues as si
from app.services.coding.store import load_history
from app.services.providers.base import LedgerProvider, ProviderError

ACCOUNTS = [{"code": "310", "name": "Repairs", "class": "EXPENSE"}]


def old_line(id_, date="2026-02-10", amount=-110.0, **kw):
    """A line stored before tax capture existed: no tax_code key at all."""
    it = {"id": id_, "tx_id": id_.split(":")[0], "line_index": 0, "date": date, "description": "Timber", "contact": "Bunnings", "reference": "",
          "amount": amount, "current_code": "310", "proposed_code": "310", "source": "history", "confidence": None, "level": "medium",
          "approved": True, "reason": "because", "status": "ok", "direction": "spend"}
    it.update(kw)
    return it


class FakeSystem(LedgerProvider):
    key, label = "xero", "Xero"

    def __init__(self, tax=None, fail=False):
        self.tax, self.fail, self.calls = tax or {}, fail, []

    def list_clients(self): return [{"id": "C1", "name": "Acme"}]
    def default_client_id(self): return "C1"
    def list_accounts(self, client_id): return ACCOUNTS
    def apply_changes(self, *a, **k): return {}

    def fetch_ledger(self, client_id, d_from, d_to, progress=None):
        self.calls.append((client_id, d_from, d_to))
        if self.fail:
            raise ProviderError("platform down")
        # what the platform returns now: the same ids, WITH tax; plus a line that was never stored
        items = [{"id": i, "date": "2026-02-10", "amount": -110.0, "tax_code": t} for i, t in self.tax.items()]
        return items, {}


class Base(unittest.TestCase):
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

    def run_(self, items, client="C1", org="Acme", status="ready", date_from="2026-02-01", date_to="2026-02-28"):
        r = LedgerRun(user_id=self.uid, provider="xero", client_id=client, organization=org, date_from=date_from, date_to=date_to,
                      status=status, items=items, accounts=ACCOUNTS)
        self.db.add(r)
        self.db.commit()
        self.db.refresh(r)
        return r

    def reload(self, r):
        self.db.expire_all()
        return self.db.get(LedgerRun, r.id)

    def backfill(self, system, name="Acme", client="C1"):
        return tb.backfill(self.db, self.uid, system, "xero", client, name)


class Plan(Base):
    def test_counts_lines_without_tax_and_their_date_range_ignoring_busy_runs(self):
        self.run_([old_line("a:0", "2026-02-03"), old_line("b:0", "2026-02-20"), old_line("c:0", tax_code="INPUT2")])
        self.run_([old_line("d:0", "2026-01-01")], status="applying")
        p = tb.plan(tb._runs(self.db, self.uid, "xero", "C1", "Acme"))
        self.assertEqual((p["lines_missing"], p["date_from"], p["date_to"]), (2, "2026-01-01", "2026-02-20"))      # busy run's date still bounds the read

    def test_nothing_missing(self):
        self.run_([old_line("a:0", tax_code="INPUT2"), old_line("b:0", tax_code=None)])
        self.assertEqual(tb.plan(tb._runs(self.db, self.uid, "xero", "C1", "Acme"))["lines_missing"], 0)


class Backfilling(Base):
    def test_adds_tax_to_lines_that_lack_it_and_touches_nothing_else(self):
        r = self.run_([old_line("a:0"), old_line("b:0")])
        before = copy.deepcopy(r.items)
        stamp = r.updated_at
        out = self.backfill(FakeSystem({"a:0": "INPUT2", "b:0": None}))
        after = self.reload(r)
        self.assertEqual((out["fetched"], out["lines_updated"], out["runs_updated"], out["lines_not_found"]), (2, 2, 1, 0))
        for old, new in zip(before, after.items):
            self.assertEqual({k: v for k, v in new.items() if k != "tax_code"}, old)                  # every other field is identical
        self.assertEqual([i["tax_code"] for i in after.items], ["INPUT2", None])                      # None = a real "no tax" value
        self.assertEqual(after.updated_at, stamp)                                                     # run ordering untouched
        self.assertEqual((after.items[0]["source"], after.items[0]["approved"], after.items[0]["status"]), ("history", True, "ok"))

    def test_lines_that_already_have_tax_are_never_overwritten(self):
        r = self.run_([old_line("a:0", tax_code="OUTPUT"), old_line("b:0")])
        self.backfill(FakeSystem({"a:0": "INPUT2", "b:0": "INPUT2"}))
        self.assertEqual([i["tax_code"] for i in self.reload(r).items], ["OUTPUT", "INPUT2"])

    def test_lines_no_longer_on_the_platform_are_counted_not_invented(self):
        r = self.run_([old_line("a:0"), old_line("gone:0")])
        out = self.backfill(FakeSystem({"a:0": "INPUT2"}))
        self.assertEqual((out["lines_updated"], out["lines_not_found"]), (1, 1))
        self.assertNotIn("tax_code", self.reload(r).items[1])

    def test_reads_exactly_the_span_of_the_missing_lines_and_is_idempotent(self):
        self.run_([old_line("a:0", "2026-02-03"), old_line("b:0", "2026-02-20")])
        system = FakeSystem({"a:0": "INPUT2", "b:0": "INPUT2"})
        self.backfill(system)
        self.assertEqual(system.calls, [("C1", datetime.date(2026, 2, 3), datetime.date(2026, 2, 20))])
        again = self.backfill(system)
        self.assertEqual((again["fetched"], again["lines_updated"]), (0, 0))
        self.assertEqual(len(system.calls), 1)                                                         # nothing missing: the platform is not called again

    def test_only_this_clients_runs_and_never_busy_runs(self):
        mine = self.run_([old_line("a:0")])
        other = self.run_([old_line("a:0")], client="OTHER", org="Other Co")
        busy = self.run_([old_line("a:0")], status="categorising")
        out = self.backfill(FakeSystem({"a:0": "INPUT2"}))
        self.assertEqual((out["runs_updated"], out["runs_skipped_busy"]), (1, 1))
        self.assertEqual("tax_code" in self.reload(mine).items[0], True)
        self.assertEqual("tax_code" in self.reload(other).items[0], False)
        self.assertEqual("tax_code" in self.reload(busy).items[0], False)

    def test_runs_saved_before_client_tracking_are_matched_by_name(self):
        legacy = self.run_([old_line("a:0")], client=None, org="Acme")
        self.backfill(FakeSystem({"a:0": "INPUT2"}))
        self.assertEqual(self.reload(legacy).items[0]["tax_code"], "INPUT2")

    def test_the_read_is_capped_to_about_two_years(self):
        self.run_([old_line("old:0", "2022-01-01"), old_line("new:0", "2026-02-10")])
        system = FakeSystem({"new:0": "INPUT2"})
        out = self.backfill(system)
        self.assertEqual(system.calls[0][1], datetime.date(2026, 2, 10) - datetime.timedelta(days=tb.MAX_SPAN_DAYS))
        self.assertEqual(out["lines_updated"], 1)

    def test_a_platform_failure_changes_nothing(self):
        r = self.run_([old_line("a:0")])
        with self.assertRaises(ProviderError):
            self.backfill(FakeSystem(fail=True))
        self.assertNotIn("tax_code", self.reload(r).items[0])

    def test_the_tax_checks_can_use_the_lines_afterwards(self):
        items = [old_line(f"x{i}:0", amount=-100.0) for i in range(4)]
        self.run_(items)
        pool = lambda: load_history(self.db, self.uid, "xero", "C1", "Acme")
        self.assertEqual(si.analyse(pool(), ACCOUNTS)["coverage"]["lines_with_tax_info"], 0)
        self.backfill(FakeSystem({i["id"]: "OUTPUT" for i in items}))                       # an income tax code on money out
        res = si.analyse(pool(), ACCOUNTS)
        self.assertEqual((res["coverage"]["lines_with_tax_info"], res["summary"]["High"]), (4, 1))


# ---------------------------------------------------------------- API
class Api(Base):
    def setUp(self):
        super().setUp()
        self.system = FakeSystem({"a:0": "INPUT2"})
        app = FastAPI()
        app.include_router(ledger_api.router, prefix="/ledger")

        def test_db():
            with Session(self.eng) as s:
                yield s

        app.dependency_overrides[dbconn.get_db] = test_db
        app.dependency_overrides[auth.get_current_active_user] = lambda: SimpleNamespace(id=self.uid)
        self.http = TestClient(app)
        p = mock.patch.object(prov, "provider_for", return_value=self.system)
        p.start()
        self.addCleanup(p.stop)

    def test_plan_is_read_only_and_post_backfills_and_audits(self):
        self.run_([old_line("a:0"), old_line("b:0")])
        plan = self.http.get("/ledger/refresh-tax", params={"provider": "xero", "client_id": "C1"}).json()
        self.assertEqual(plan["lines_missing"], 2)
        self.assertEqual(self.system.calls, [])                                               # planning never calls the platform
        r = self.http.post("/ledger/refresh-tax", json={"provider": "xero", "client_id": "C1"})
        self.assertEqual((r.status_code, r.json()["lines_updated"], r.json()["lines_not_found"]), (200, 1, 1))
        with Session(self.eng) as s:
            self.assertEqual(s.query(AuditLog).filter(AuditLog.action == "tax_backfill").count(), 1)
        self.assertEqual(self.http.get("/ledger/refresh-tax", params={"provider": "xero", "client_id": "C1"}).json()["lines_missing"], 1)

    def test_an_unknown_client_is_rejected_and_a_platform_error_is_a_502(self):
        self.assertEqual(self.http.post("/ledger/refresh-tax", json={"provider": "xero", "client_id": "NOT-MINE"}).status_code, 400)
        self.run_([old_line("a:0")])
        self.system.fail = True
        self.assertEqual(self.http.post("/ledger/refresh-tax", json={"provider": "xero", "client_id": "C1"}).status_code, 502)


if __name__ == "__main__":
    unittest.main()
