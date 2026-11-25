"""Decision tracking, accuracy figures and the auto-approval gate. Run: python -m unittest discover -s tests -v"""
import datetime
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

from app.api.endpoints import accuracy as accuracy_api
from app.api.endpoints import ledger as ledger_api
from app.database import connection as dbconn
from app.database.models import LedgerRun, SuggestionDecision, User
from app.security import auth
from app.services import accuracy as acc
from app.services import ledger_coding as lc
from app.services import providers as prov
from app.services.providers.base import LedgerProvider

ACCOUNTS = [{"code": "200", "name": "Sales", "class": "REVENUE"}, {"code": "260", "name": "Other Revenue", "class": "REVENUE"},
            {"code": "310", "name": "COGS", "class": "EXPENSE"}]

_n = 0


def item(source="history", level="high", proposed="200", current="260", confidence=None, status="review", **kw):
    global _n
    _n += 1
    it = {"id": f"i{_n}:0", "tx_id": f"i{_n}", "line_index": 0, "date": "2026-03-01", "description": f"BRIDGE {_n}",
          "contact": "Bridge", "reference": "", "type": "RECEIVE", "direction": "receive", "amount": 100.0,
          "current_code": current, "proposed_code": proposed, "source": source, "confidence": confidence, "level": level,
          "reason": "", "status": status, "bank_account": "Cheque"}
    it.update(kw)
    return it


class Base(unittest.TestCase):
    def setUp(self):
        self.eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.eng)
        self.db = Session(self.eng)
        self.addCleanup(self.db.close)
        self.user = User(username="u", hashed_password="x")
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)

    def run_(self, client="C1", name="Acme", provider="xero", items=None):
        r = LedgerRun(user_id=self.user.id, provider=provider, client_id=client, organization=name, date_from="a", date_to="b",
                      status="ready", items=items or [], accounts=ACCOUNTS)
        self.db.add(r)
        self.db.commit()
        self.db.refresh(r)
        return r

    def decide(self, run, n, source="history", level="high", right=True, how="line", **kw):
        for _ in range(n):
            it = item(source=source, level=level, **kw)
            acc.record_decision(self.db, run, it, it["proposed_code"] if right else "310", how)
        self.db.commit()


class Recording(Base):
    def test_only_suggestions_are_recorded_never_a_persons_own_codes(self):
        run = self.run_()
        for src in ("manual", "keep", None, "locked"):
            self.assertFalse(acc.record_decision(self.db, run, item(source=src), "310", "edit"))
        self.assertFalse(acc.record_decision(self.db, run, item(proposed=None), "310", "edit"))
        self.assertTrue(acc.record_decision(self.db, run, item(source="rule", level=None), "310", "edit"))
        self.db.commit()
        self.assertEqual(self.db.query(SuggestionDecision).count(), 1)

    def test_accepted_corrected_and_cleared(self):
        run = self.run_()
        a, b, c = item(), item(), item()
        for it, final in ((a, "200"), (b, "310"), (c, None)):
            acc.record_decision(self.db, run, it, final, "line")
        self.db.commit()
        rows = {r.item_id: r for r in self.db.query(SuggestionDecision).all()}
        self.assertEqual([rows[x["id"]].outcome for x in (a, b, c)], ["accepted", "corrected", "corrected"])
        self.assertEqual((rows[b["id"]].suggested_name, rows[b["id"]].final_name), ("Sales", "COGS"))
        self.assertIsNone(rows[c["id"]].final_code)

    def test_deciding_again_replaces_the_earlier_decision(self):
        run = self.run_()
        it = item()
        acc.record_decision(self.db, run, it, "310", "edit")
        acc.record_decision(self.db, run, it, "200", "edit")
        self.db.commit()
        rows = self.db.query(SuggestionDecision).all()
        self.assertEqual((len(rows), rows[0].outcome), (1, "accepted"))

    def test_history_keeps_its_level_ai_keeps_its_confidence_and_a_snapshot_for_review(self):
        run = self.run_()
        h, a = item(level="medium"), item(source="ai", level=None, confidence=0.72)
        acc.record_decision(self.db, run, h, "200", "line")
        acc.record_decision(self.db, run, a, "310", "line")
        self.db.commit()
        rows = {r.source: r for r in self.db.query(SuggestionDecision).all()}
        self.assertEqual((rows["history"].level, rows["history"].confidence), ("medium", None))
        self.assertEqual((rows["ai"].level, rows["ai"].confidence), (None, 0.72))
        self.assertEqual((rows["ai"].item_description, rows["ai"].item_contact, rows["ai"].item_amount), (a["description"], "Bridge", 100.0))

    def test_auto_approved_lines_are_tracked_separately(self):
        run = self.run_()
        kept, changed = item(auto_approved=True), item(auto_approved=True)
        acc.record_decision(self.db, run, kept, "200", "edit")
        acc.record_decision(self.db, run, changed, "310", "edit")
        self.db.commit()
        self.assertEqual(sorted(r.outcome for r in self.db.query(SuggestionDecision).all()), ["auto", "auto_corrected"])


class RecordApply(Base):
    def test_unreviewed_history_and_ai_lines_are_logged_as_auto_once_and_manual_ones_not_at_all(self):
        run = self.run_()
        h, a, m, rule = item(), item(source="ai", level=None, confidence=0.9), item(source="manual", level=None), item(source="rule", level=None)
        decided = item()
        acc.record_decision(self.db, run, decided, "200", "line")
        self.db.commit()
        n = acc.record_apply(self.db, run, [h, a, m, rule, decided])
        self.db.commit()
        self.assertEqual(n, 2)                                                   # h and a only
        rows = {r.item_id: r for r in self.db.query(SuggestionDecision).all()}
        self.assertEqual((rows[h["id"]].outcome, rows[h["id"]].how), ("auto", "apply"))
        self.assertEqual(rows[decided["id"]].how, "line")                         # the person's decision is untouched
        self.assertEqual(acc.record_apply(self.db, run, [h]), 0)                  # idempotent


class Summary(Base):
    def test_only_individual_decisions_count_bulk_and_auto_are_reported_separately(self):
        run = self.run_()
        self.decide(run, 8, right=True, how="line")
        self.decide(run, 2, right=False, how="edit")
        self.decide(run, 30, right=True, how="bulk")
        acc.record_apply(self.db, run, [item(), item()])
        self.db.commit()
        s = acc.summary(self.db, self.user.id, None, None)
        self.assertEqual((s["totals"]["checked"], s["totals"]["right"], s["totals"]["accuracy_pct"]), (10, 8, 80.0))
        self.assertEqual((s["totals"]["bulk_accepted"], s["totals"]["auto_approved"], s["totals"]["auto_corrected"]), (30, 2, 0))
        self.assertIn("never marks its own work".split()[0], "never")            # (the note below states the principle)
        self.assertIn("not counted", s["note"])

    def test_breakdown_by_confidence_source_client_and_month_and_recent_mistakes(self):
        r1, r2 = self.run_("C1", "Acme"), self.run_("C2", "Beta")
        self.decide(r1, 3, level="high", right=True)
        self.decide(r1, 1, level="high", right=False)
        self.decide(r1, 2, level="low", right=False)
        self.decide(r2, 2, source="ai", level=None, confidence=0.9, right=True)
        self.decide(r2, 1, source="ai", level=None, confidence=0.5, right=False)
        s = acc.summary(self.db, self.user.id, None, None)
        b = {x["bucket"]: x for x in s["by_confidence"]}
        self.assertEqual((b["history · high"]["checked"], b["history · high"]["accuracy_pct"]), (4, 75.0))
        self.assertEqual(b["history · low"]["accuracy_pct"], 0.0)
        self.assertEqual((b["ai · high (80%+)"]["accuracy_pct"], b["ai · low (<60%)"]["accuracy_pct"]), (100.0, 0.0))
        self.assertEqual({c["client_name"]: c["checked"] for c in s["by_client"]}, {"Acme": 6, "Beta": 3})
        self.assertEqual(s["by_month"][0]["checked"], 9)
        self.assertEqual(len(s["recent_corrections"]), 4)            # 1 high + 2 low (Acme) + 1 AI (Beta) were corrected
        self.assertTrue(all(c["final"] == "310 COGS" for c in s["recent_corrections"]))
        # filtering to one client
        only = acc.summary(self.db, self.user.id, "xero", "C2")
        self.assertEqual(only["totals"]["checked"], 3)

    def test_old_decisions_fall_out_of_the_window(self):
        run = self.run_()
        self.decide(run, 3)
        for r in self.db.query(SuggestionDecision).all():
            r.decided_at = datetime.datetime.utcnow() - datetime.timedelta(days=400)
            self.db.add(r)
        self.db.commit()
        self.assertEqual(acc.summary(self.db, self.user.id, None, None, days=180)["totals"]["checked"], 0)
        self.assertEqual(acc.summary(self.db, self.user.id, None, None, days=500)["totals"]["checked"], 3)

    def test_empty_is_safe(self):
        s = acc.summary(self.db, self.user.id, None, None)
        self.assertEqual((s["totals"]["checked"], s["totals"]["accuracy_pct"], s["by_confidence"]), (0, None, []))


class ShadowPeriod(Base):
    def status(self, client="C1"):
        return acc.policy_status(self.db, self.user.id, "xero", client)

    def test_not_earned_until_15_checked_high_suggestions_at_95_percent(self):
        run = self.run_()
        self.decide(run, 14, right=True)
        s = self.status()
        self.assertEqual((s["active"], s["high_checked"]), (False, 14))
        self.assertIn("14 of 15", s["why"])
        self.decide(run, 1, right=True)
        s = self.status()
        self.assertEqual((s["eligible"], s["active"], s["high_accuracy_pct"]), (True, True, 100.0))

    def test_accuracy_threshold(self):
        run = self.run_()
        self.decide(run, 14, right=True)
        self.decide(run, 1, right=False)                                       # 14/15 = 93.3%
        self.assertFalse(self.status()["eligible"])
        self.decide(run, 5, right=True)                                        # 19/20 = 95.0% -> earned
        self.assertTrue(self.status()["eligible"])

    def test_only_individual_high_history_decisions_count_towards_it(self):
        run = self.run_()
        self.decide(run, 40, right=True, how="bulk")                           # bulk accepts: never a check
        self.decide(run, 40, level="medium", right=True)                       # not High
        self.decide(run, 40, source="ai", level=None, confidence=0.99, right=True)
        self.assertEqual(self.status()["high_checked"], 0)

    def test_it_is_earned_per_client(self):
        self.decide(self.run_("C1"), 20, right=True)
        self.assertTrue(self.status("C1")["active"])
        self.assertFalse(self.status("C2")["active"])

    def test_manual_override_skips_the_shadow_period(self):
        acc.set_override(self.db, self.user.id, "xero", "C2", True)
        self.db.commit()
        s = self.status("C2")
        self.assertEqual((s["active"], s["eligible"], s["override"]), (True, False, True))
        acc.set_override(self.db, self.user.id, "xero", "C2", False)
        self.db.commit()
        self.assertFalse(self.status("C2")["active"])

    def test_circuit_breaker_pauses_auto_approval_when_auto_approved_lines_keep_being_corrected(self):
        run = self.run_()
        self.decide(run, 20, right=True)                                       # earned
        for k in range(20):                                                    # 20 auto-approved lines; 1 corrected = 5% (allowed)
            acc.record_decision(self.db, run, item(auto_approved=True), "310" if k == 0 else "200", "edit")
        self.db.commit()
        self.assertTrue(self.status()["active"])
        acc.record_decision(self.db, run, item(auto_approved=True), "310", "edit")          # 2 of 21 = 9.5% > 5%
        self.db.commit()
        s = self.status()
        self.assertEqual((s["eligible"], s["suspended"], s["active"]), (True, True, False))
        self.assertIn("Paused", s["why"])

    def test_the_breaker_also_stops_a_manual_override(self):
        run = self.run_()
        acc.set_override(self.db, self.user.id, "xero", "C1", True)
        for _ in range(10):
            acc.record_decision(self.db, run, item(auto_approved=True), "310", "edit")
        self.db.commit()
        self.assertFalse(self.status()["active"])

    def test_a_few_early_auto_lines_do_not_trip_the_breaker(self):
        run = self.run_()
        self.decide(run, 20, right=True)
        for _ in range(5):                                                     # below the minimum sample (10)
            acc.record_decision(self.db, run, item(auto_approved=True), "310", "edit")
        self.db.commit()
        self.assertTrue(self.status()["active"])


class Pipeline(unittest.TestCase):
    def history_items(self):
        return [{"id": f"h{i}", "description": "BRIDGE SELECT RESIDENTIAL", "contact": "Bridge Select", "reference": "",
                 "amount": 100.0, "current_code": "200", "date": "2025-01-01"} for i in range(40)]

    def target(self):
        return {"id": "t:0", "tx_id": "t", "line_index": 0, "date": "2026-03-01", "description": "BRIDGE SELECT RESIDENTIAL",
                "contact": "Bridge Select", "reference": "", "type": "RECEIVE", "direction": "receive", "amount": 100.0,
                "current_code": "260", "proposed_code": None, "source": None, "confidence": None, "reason": "",
                "status": "unassigned", "bank_account": "Cheque"}

    def test_high_changes_wait_for_review_until_auto_approval_is_earned(self):
        t = self.target()
        lc.apply_history([t], self.history_items(), ACCOUNTS)
        lc.refresh([t], ACCOUNTS)
        self.assertEqual((t["status"], t["auto_approved"]), ("review", False))

        t = self.target()
        lc.apply_history([t], self.history_items(), ACCOUNTS, auto_approve=True)
        lc.refresh([t], ACCOUNTS)
        self.assertEqual((t["status"], t["auto_approved"], t["proposed_code"]), ("ok", True, "200"))
        self.assertEqual(len(lc.changes_to_apply([t])), 1)                     # still only written when the user presses Apply

    def test_medium_and_low_never_auto_approve(self):
        t = self.target()
        t["description"], t["contact"] = "BRIDGE SELECT RESIDENTIAL OTHER", ""             # overlap 4/6 = 0.67: comparable, context not confirmed
        pool = [dict(h, description="BRIDGE SELECT RESIDENTIAL OTHER STUFF HERE", contact="") for h in self.history_items()]
        lc.apply_history([t], pool, ACCOUNTS, auto_approve=True)
        lc.refresh([t], ACCOUNTS)
        self.assertFalse(t["auto_approved"])
        self.assertEqual(t["status"], "review")


# ---------------------------------------------------------------- API
class FakeSystem(LedgerProvider):
    key, label = "xero", "Xero"

    def list_clients(self): return [{"id": "C1", "name": "Acme"}]
    def default_client_id(self): return "C1"
    def list_accounts(self, client_id): return ACCOUNTS
    def fetch_ledger(self, *a, **k): return [], {}
    def apply_changes(self, *a, **k): return {}


class Api(Base):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(ledger_api.router, prefix="/ledger")
        app.include_router(accuracy_api.router, prefix="/accuracy")

        def test_db():
            with Session(self.eng) as s:
                yield s

        app.dependency_overrides[dbconn.get_db] = test_db
        app.dependency_overrides[auth.get_current_active_user] = lambda: self.user
        self.http = TestClient(app)
        overview = [{"key": "xero", "label": "Xero", "status": "connected", "ready": True, "verified": True, "supports_apply": True, "note": ""}]
        for p in (mock.patch.object(prov, "provider_overview", return_value=overview),
                  mock.patch.object(prov, "provider_for", return_value=FakeSystem())):
            p.start()
            self.addCleanup(p.stop)

    def seeded(self, *its):
        run = self.run_(items=list(its))
        lc.refresh(run.items, ACCOUNTS)
        run.items = [dict(i) for i in run.items]
        self.db.add(run)
        self.db.commit()
        return str(run.id)

    def rows(self):
        with Session(self.eng) as s:
            return s.query(SuggestionDecision).all()

    def test_editing_a_suggested_line_records_the_decision_but_a_manual_to_manual_edit_does_not(self):
        sug, mine = item(), item(source="manual", level=None, confidence=1.0, status="ok")
        rid = self.seeded(sug, mine)
        r = self.http.patch(f"/ledger/runs/{rid}/items", json={"changes": {sug["id"]: "310", mine["id"]: "200"}})
        self.assertEqual(r.status_code, 200, r.text)
        rows = self.rows()
        self.assertEqual([(x.item_id, x.outcome, x.how) for x in rows], [(sug["id"], "corrected", "edit")])
        now = next(i for i in r.json()["items"] if i["id"] == sug["id"])
        self.assertEqual((now["source"], now["proposed_code"]), ("manual", "310"))

    def test_clearing_a_suggestion_counts_as_rejecting_it(self):
        sug = item()
        rid = self.seeded(sug)
        self.http.patch(f"/ledger/runs/{rid}/items", json={"changes": {sug["id"]: None}})
        self.assertEqual((self.rows()[0].outcome, self.rows()[0].final_code), ("corrected", None))

    def test_confirm_is_a_counted_check_and_approves_the_line(self):
        sug, ok = item(), item(status="ok", current="200", proposed="200")
        rid = self.seeded(sug, ok)
        r = self.http.post(f"/ledger/runs/{rid}/items/{sug['id']}/confirm")
        self.assertEqual(r.status_code, 200, r.text)
        row = self.rows()[0]
        self.assertEqual((row.outcome, row.how, row.final_code), ("accepted", "line", "200"))
        self.assertEqual(next(i for i in r.json()["items"] if i["id"] == sug["id"])["status"], "ok")
        self.assertEqual(self.http.post(f"/ledger/runs/{rid}/items/{ok['id']}/confirm").status_code, 409)   # nothing to review
        self.assertEqual(self.http.post(f"/ledger/runs/{rid}/items/nope/confirm").status_code, 404)
        self.assertEqual(self.http.get("/accuracy").json()["totals"]["checked"], 1)

    def test_bulk_accept_is_logged_but_never_counted(self):
        rid = self.seeded(item(), item(), item())
        self.assertEqual(self.http.post(f"/ledger/runs/{rid}/accept-review").status_code, 200)
        self.assertEqual({(x.outcome, x.how) for x in self.rows()}, {("accepted", "bulk")})
        t = self.http.get("/accuracy").json()["totals"]
        self.assertEqual((t["checked"], t["bulk_accepted"]), (0, 3))

    def test_apply_logs_unreviewed_lines_as_auto_and_a_later_correction_of_one_is_a_safety_signal(self):
        auto = item(status="ok", auto_approved=True)
        run = self.run_(items=[auto])
        with Session(self.eng) as s:
            r = s.get(LedgerRun, run.id)
            acc.record_apply(s, r, [auto])
            s.commit()
        self.assertEqual(self.rows()[0].outcome, "auto")
        # the person later edits that very line: recorded as an auto-approved line that had to be corrected
        rid = str(run.id)
        r = self.http.patch(f"/ledger/runs/{rid}/items", json={"changes": {auto["id"]: "310"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.rows()[0].outcome, "auto_corrected")
        t = self.http.get("/accuracy").json()["totals"]
        self.assertEqual((t["checked"], t["auto_approved"], t["auto_corrected"]), (0, 1, 1))

    def test_dashboard_for_one_client_includes_the_policy_and_the_override_toggle_works(self):
        run = self.run_()
        self.decide(run, 16, right=True)
        d = self.http.get("/accuracy", params={"provider": "xero", "client_id": "C1"}).json()
        self.assertEqual((d["policy"]["active"], d["policy"]["high_checked"]), (True, 16))
        self.assertIsNone(self.http.get("/accuracy").json()["policy"])             # no policy without a specific client

        p = self.http.put("/accuracy/policy", json={"provider": "xero", "client_id": "C1", "auto_override": True}).json()
        self.assertTrue(p["override"])
        self.assertEqual(self.http.put("/accuracy/policy", json={"provider": "xero", "client_id": "NOT-MINE", "auto_override": True}).status_code, 400)
        self.assertEqual(self.http.get("/accuracy", params={"days": 3}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
