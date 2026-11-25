"""Client workflow. Run: python -m unittest discover -s tests -v  (from backend/)"""
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

from app.api.endpoints import workflow as workflow_api
from app.database import connection as dbconn
from app.database.models import AuditLog, ClientWorkflow, HistoryImport, LedgerRun, User, WorkflowFinding
from app.security import auth
from app.services import client_settings as cs
from app.services import providers as prov
from app.services import workflow as wfs
from app.services.providers.base import LedgerProvider

D = datetime.date
ACCOUNTS = [{"code": "200", "name": "Sales", "class": "REVENUE"}, {"code": "310", "name": "Contract Installations", "class": "EXPENSE"},
            {"code": "404", "name": "Bank Fees", "class": "EXPENSE"}]
_n = 0


def line(amount, tax="__absent__", code="310", date="2026-08-10", status="ok", contact="Acme"):
    global _n
    _n += 1
    it = {"id": f"l{_n}", "tx_id": f"l{_n}", "line_index": 0, "date": date, "description": "payment", "contact": contact, "reference": "",
          "amount": amount, "current_code": code, "proposed_code": code, "source": None, "status": status, "direction": "spend" if amount < 0 else "receive"}
    if tax != "__absent__":
        it["tax_code"] = tax
    return it


# ---------------------------------------------------------------- calendar
class Calendar(unittest.TestCase):
    def test_periods(self):
        self.assertEqual(wfs.bounds("quarterly", D(2026, 8, 15)), (D(2026, 7, 1), D(2026, 9, 30)))
        self.assertEqual(wfs.bounds("quarterly", D(2026, 12, 31)), (D(2026, 10, 1), D(2026, 12, 31)))
        self.assertEqual(wfs.bounds("quarterly", D(2026, 2, 1)), (D(2026, 1, 1), D(2026, 3, 31)))
        self.assertEqual(wfs.bounds("quarterly", D(2026, 5, 30)), (D(2026, 4, 1), D(2026, 6, 30)))
        self.assertEqual(wfs.bounds("monthly", D(2028, 2, 10)), (D(2028, 2, 1), D(2028, 2, 29)))       # leap year
        self.assertEqual(wfs.bounds("annual", D(2026, 8, 1)), (D(2026, 7, 1), D(2027, 6, 30)))
        self.assertEqual(wfs.bounds("annual", D(2026, 3, 1)), (D(2025, 7, 1), D(2026, 6, 30)))
        with self.assertRaises(ValueError):
            wfs.bounds("weekly", D(2026, 1, 1))

    def test_default_is_the_most_recent_period_that_has_ended(self):
        self.assertEqual(wfs.default_period("quarterly", D(2026, 11, 18)), (D(2026, 7, 1), D(2026, 9, 30)))
        self.assertEqual(wfs.default_period("quarterly", D(2026, 12, 31)), (D(2026, 7, 1), D(2026, 9, 30)))   # Oct-Dec has not ended yet
        self.assertEqual(wfs.default_period("quarterly", D(2027, 1, 1)), (D(2026, 10, 1), D(2026, 12, 31)))
        self.assertEqual(wfs.default_period("monthly", D(2026, 11, 18)), (D(2026, 10, 1), D(2026, 10, 31)))
        self.assertEqual(wfs.default_period("monthly", D(2027, 1, 5)), (D(2026, 12, 1), D(2026, 12, 31)))

    def test_standard_ato_due_dates(self):
        due = lambda f, end: wfs.due_date(f, end)
        self.assertEqual(due("quarterly", D(2026, 9, 30)), D(2026, 10, 28))
        self.assertEqual(due("quarterly", D(2026, 12, 31)), D(2027, 2, 28))                # the Oct-Dec quarter is due 28 February
        self.assertEqual(due("quarterly", D(2027, 3, 31)), D(2027, 4, 28))
        self.assertEqual(due("quarterly", D(2027, 6, 30)), D(2027, 7, 28))
        self.assertEqual(due("monthly", D(2026, 10, 31)), D(2026, 11, 21))
        self.assertEqual(due("monthly", D(2026, 12, 31)), D(2027, 1, 21))
        self.assertEqual(due("annual", D(2027, 6, 30)), D(2027, 10, 31))

    def test_labels(self):
        self.assertEqual(wfs.label("quarterly", D(2026, 7, 1), D(2026, 9, 30)), "Jul-Sep 2026")
        self.assertEqual(wfs.label("monthly", D(2026, 10, 1), D(2026, 10, 31)), "Oct 2026")
        self.assertEqual(wfs.label("annual", D(2025, 7, 1), D(2026, 6, 30)), "FY 2025-26")


# ---------------------------------------------------------------- BAS estimate
class Estimate(unittest.TestCase):
    def test_gst_is_one_eleventh_of_the_gross_and_free_and_capital_are_separate(self):
        e = wfs.bas_estimate([
            line(1100.0, "OUTPUT", "200"), line(550.0, "OUTPUT", "200"), line(500.0, "EXEMPTOUTPUT", "200"),     # sales: 1650 GST, 500 free
            line(-220.0, "INPUT2"), line(-110.0, "INPUT2"), line(-330.0, "EXEMPTINPUT"), line(-1100.0, "CAPEXINPUT2"),
        ])
        self.assertEqual((e["G1"], e["G3"], e["1A"]), (2150.0, 500.0, 150.0))
        self.assertEqual((e["G10"], e["G11"], e["1B"]), (1100.0, 660.0, 130.0))                 # (220+110+1100)/11
        self.assertEqual((e["lines"], e["lines_with_tax"]), (7, 7))

    def test_refunds_reduce_and_excluded_or_untaxed_lines_are_left_out(self):
        e = wfs.bas_estimate([line(1100.0, "OUTPUT", "200"), line(-110.0, "OUTPUT", "200"),                         # a sale refund
                              line(-100.0, "BASEXCLUDED"), line(-50.0, None), line(-40.0), line(300.0, "Input Taxed Sales", "200")])
        self.assertEqual((e["G1"], e["1A"], e["G3"]), (1290.0, 90.0, 0.0))                       # input taxed sales: in G1, not G3, no GST
        self.assertEqual((e["lines"], e["lines_with_tax"]), (6, 5))                              # BASEXCLUDED and (none) are tax info; absent is not

    def test_a_bare_input_taxed_label_is_neutral_not_a_purchase(self):
        from app.services.coding import setup_issues as si
        self.assertIsNone(si.tax_side("Input Taxed"))
        self.assertEqual(si.tax_side("Input Taxed Sales"), "income")
        self.assertEqual(si.tax_side("Input Taxed Purchases"), "expense")
        self.assertEqual(si.tax_side("GST on Expenses"), "expense")                              # unchanged
        e = wfs.bas_estimate([line(300.0, "Input Taxed", "200")])                                # unknown side: left out rather than guessed
        self.assertEqual((e["G1"], e["G11"]), (0.0, 0.0))

    def test_empty_is_safe(self):
        e = wfs.bas_estimate([])
        self.assertEqual((e["G1"], e["1A"], e["1B"], e["lines"]), (0.0, 0.0, 0.0, 0))

    def test_comparison_thresholds(self):
        est = {"G1": 10000.0, "1A": 909.09, "1B": 400.0}
        self.assertEqual(wfs.compare_figures(est, {"G1": 10100, "1A": "909.09", "1B": 400}), [])                  # within 2%
        out = wfs.compare_figures(est, {"G1": 10500, "1A": 1100, "1B": None, "G10": 5})
        self.assertEqual([(f["severity"], f["detail"]["figure"]) for f in out], [("Medium", "G1"), ("High", "1A")])
        self.assertIn("difference -500.00", out[0]["text"])
        self.assertEqual(wfs.compare_figures(est, {"G1": "abc"}), [])
        self.assertEqual(wfs.compare_figures(est, {}), [])


# ---------------------------------------------------------------- data-backed
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

    def wf(self, start="2026-07-01", end="2026-09-30", **kw):
        w = ClientWorkflow(user_id=self.uid, provider="xero", client_id="C1", client_name="Acme", period_start=start, period_end=end,
                           label="Jul-Sep 2026", due_date="2026-10-28", checklist={}, bas_figures={}, checks={}, **kw)
        self.db.add(w)
        self.db.commit()
        self.db.refresh(w)
        return w

    def run_(self, items, date_from="2026-07-01", date_to="2026-09-30", client="C1"):
        r = LedgerRun(user_id=self.uid, provider="xero", client_id=client, organization="Acme", date_from=date_from, date_to=date_to,
                      status="ready", items=items, accounts=ACCOUNTS)
        self.db.add(r)
        self.db.commit()
        return r

    def settings(self, **v):
        cs.save(self.db, self.uid, "xero", "C1", v)
        self.db.commit()
        return cs.get(self.db, self.uid, "xero", "C1")


class Findings(Base):
    def p(self, text="x", sev="High", kind="k", parts=("a",)):
        return {"severity": sev, "kind": kind, "key_parts": list(parts), "text": text}

    def rows(self, w):
        return {f.text: f for f in self.db.query(WorkflowFinding).filter(WorkflowFinding.workflow_id == w.id).all()}

    def test_new_findings_open_and_a_persons_decision_survives_a_rerun(self):
        w = self.wf()
        self.assertEqual(wfs.store_findings(self.db, w, "gl", [self.p("A"), self.p("B", parts=("b",))]), {"new": 2, "reopened": 0, "fixed": 0})
        a = self.rows(w)["A"]
        a.status, a.note = "accepted", "client confirmed"
        self.db.add(a)
        self.db.commit()
        self.assertEqual(wfs.store_findings(self.db, w, "gl", [self.p("A (reworded)"), self.p("B", parts=("b",))]), {"new": 0, "reopened": 0, "fixed": 0})
        self.db.commit()
        rows = {f.key: f for f in self.db.query(WorkflowFinding).all()}
        self.assertEqual(len(rows), 2)
        kept = next(f for f in rows.values() if f.note == "client confirmed")
        self.assertEqual((kept.status, kept.text), ("accepted", "A (reworded)"))

    def test_a_finding_that_stops_being_produced_is_fixed_only_if_it_was_open(self):
        w = self.wf()
        wfs.store_findings(self.db, w, "gl", [self.p("A"), self.p("B", parts=("b",)), self.p("C", parts=("c",))])
        self.db.commit()
        r = self.rows(w)
        r["B"].status = "resolved"
        r["C"].status = "accepted"
        for f in r.values():
            self.db.add(f)
        self.db.commit()
        self.assertEqual(wfs.store_findings(self.db, w, "gl", [])["fixed"], 1)
        self.db.commit()
        self.assertEqual({k: v.status for k, v in self.rows(w).items()}, {"A": "fixed", "B": "resolved", "C": "accepted"})

    def test_a_fixed_finding_that_comes_back_reopens(self):
        w = self.wf()
        wfs.store_findings(self.db, w, "gl", [self.p("A")])
        wfs.store_findings(self.db, w, "gl", [])
        self.db.commit()
        self.assertEqual(wfs.store_findings(self.db, w, "gl", [self.p("A")])["reopened"], 1)
        self.db.commit()
        self.assertEqual(self.rows(w)["A"].status, "open")

    def test_stages_are_kept_apart(self):
        w = self.wf()
        wfs.store_findings(self.db, w, "gl", [self.p("A")])
        wfs.store_findings(self.db, w, "bas", [])               # a clean BAS run must not touch the GL finding
        self.db.commit()
        self.assertEqual(self.rows(w)["A"].status, "open")


class Coverage(Base):
    def test_runs_and_imports_count_and_gaps_are_reported(self):
        w = self.wf("2026-07-01", "2026-07-31")
        self.assertEqual(wfs.ledger_coverage(self.db, w)["covered_days"], 0)
        self.run_([line(-10.0)], "2026-07-01", "2026-07-10")
        self.db.add(HistoryImport(user_id=self.uid, provider="xero", client_id="C1", filename="f", items=[], lines=0,
                                  date_from="2026-07-20", date_to="2026-07-31"))
        self.db.commit()
        c = wfs.ledger_coverage(self.db, w)
        self.assertEqual((c["covered_days"], c["total_days"], c["complete"], c["gaps"]), (22, 31, False, [("2026-07-11", "2026-07-19")]))
        self.run_([line(-10.0)], "2026-07-11", "2026-07-19")
        self.assertTrue(wfs.ledger_coverage(self.db, w)["complete"])

    def test_other_clients_runs_do_not_count_and_best_run_has_the_most_overlap(self):
        w = self.wf()
        self.run_([line(-10.0)], "2026-07-01", "2026-09-30", client="OTHER")
        self.assertIsNone(wfs.best_run(self.db, w))
        small, big = self.run_([line(-10.0)], "2026-09-01", "2026-09-30"), self.run_([line(-10.0)], "2026-07-01", "2026-09-30")
        self.assertEqual(wfs.best_run(self.db, w).id, big.id)
        self.assertNotEqual(small.id, big.id)


class Stages(Base):
    def stage(self, w, settings=None, key=None):
        s = {x["key"]: x for x in wfs.stages(self.db, w, settings or self.settings())}
        return s[key] if key else s

    def test_files_need_full_ledger_coverage_and_the_checklist(self):
        w = self.wf()
        self.assertEqual(self.stage(w, key="files")["status"], "needs_you")
        self.run_([line(-10.0)])
        s = self.stage(w, key="files")
        self.assertEqual(s["status"], "needs_you")
        self.assertIn("add: BAS figures", s["summary"])
        w.checklist = {"bas_summary": True, "profit_and_loss": True, "balance_sheet": True}
        self.db.add(w)
        self.db.commit()
        self.assertEqual(self.stage(w, key="files")["status"], "done")

    def test_a_client_not_registered_for_gst_does_not_need_bas_figures(self):
        w = self.wf()
        self.run_([line(-10.0)])
        w.checklist = {"profit_and_loss": True, "balance_sheet": True}
        self.db.add(w)
        self.db.commit()
        self.assertEqual(self.stage(w, key="files")["status"], "needs_you")
        self.assertEqual(self.stage(w, self.settings(gst_registration="not_registered"), key="files")["status"], "done")

    def test_coding_check_counts_uncoded_and_review_lines_in_the_period_only(self):
        w = self.wf()
        self.assertIn("No ledger", self.stage(w, key="coding")["summary"])
        self.run_([line(-10.0, status="unassigned"), line(-10.0, status="review"), line(-10.0, status="ok"),
                   line(-10.0, status="unassigned", date="2026-12-01")])                       # outside the period: ignored
        s = self.stage(w, key="coding")
        self.assertEqual((s["status"], s["detail"]["lines"], s["detail"]["unassigned"], s["detail"]["review"]), ("needs_you", 3, 1, 1))
        self.run_([line(-10.0, status="ok"), line(5.0, status="locked")])
        self.assertEqual(self.stage(w, key="coding")["status"], "done")

    def test_gl_review_rates_red_amber_green_from_open_findings(self):
        w = self.wf()
        self.assertEqual(self.stage(w, key="gl")["status"], "not_run")
        w.checks = {"gl": {"ran_at": "t", "lines": 40, "lines_with_tax": 40}}
        wfs.store_findings(self.db, w, "gl", [{"severity": "Medium", "kind": "k", "key_parts": ["m"], "text": "m"}])
        self.db.commit()
        self.assertEqual(self.stage(w, key="gl")["rating"], "AMBER")
        wfs.store_findings(self.db, w, "gl", [{"severity": "Medium", "kind": "k", "key_parts": ["m"], "text": "m"},
                                              {"severity": "High", "kind": "h", "key_parts": ["h"], "text": "h"}])
        self.db.commit()
        s = self.stage(w, key="gl")
        self.assertEqual((s["rating"], s["status"]), ("RED", "done"))
        self.assertIn("RED - 1 High item(s)", s["summary"])
        for f in self.db.query(WorkflowFinding).all():
            f.status = "accepted"
            self.db.add(f)
        self.db.commit()
        self.assertEqual(self.stage(w, key="gl")["rating"], "GREEN")

    def test_a_review_that_checked_nothing_is_never_green(self):
        w = self.wf()
        cash = self.settings(gst_registration="cash")
        wfs.run_gl_review(self.db, w, ACCOUNTS, cash)                 # no lines in the period at all
        self.db.commit()
        gl = self.stage(w, cash, "gl")
        self.assertEqual((gl["status"], gl.get("rating")), ("needs_you", None))
        self.assertIn("nothing to review", gl["summary"])
        self.assertTrue(any("no lines to check" in b for b in wfs.signoff_blockers(wfs.stages(self.db, w, cash), wfs.findings_of(self.db, w))))

    def test_bas_with_no_lines_or_no_tax_information_does_not_pretend_to_have_an_estimate(self):
        w = self.wf()
        cash = self.settings(gst_registration="cash")
        wfs.run_bas_review(self.db, w, cash)
        self.db.commit()
        self.assertIn("nothing to estimate", self.stage(w, cash, "bas")["summary"])
        self.run_([line(-100.0), line(-50.0)])                                         # lines, but no tax codes captured
        wfs.run_bas_review(self.db, w, cash)
        self.db.commit()
        s = self.stage(w, cash, "bas")
        self.assertEqual(s["status"], "needs_you")
        self.assertIn("no estimate is possible", s["summary"])
        self.assertNotIn("Estimate ready", s["summary"])

    def test_tax_information_without_any_gst_code_is_called_out_not_shown_as_a_zero_estimate(self):
        w = self.wf()
        cash = self.settings(gst_registration="cash")
        self.run_([line(-100.0, "NONE"), line(-50.0, "NONE"), line(200.0, "NONE", "200")])        # every line "No GST"
        e = wfs.run_bas_review(self.db, w, cash)["estimate"]
        self.db.commit()
        self.assertEqual((e["lines_with_tax"], e["lines_with_gst_codes"], e["G1"], e["1B"]), (3, 0, 0.0, 0.0))
        s = self.stage(w, cash, "bas")
        self.assertEqual(s["status"], "needs_you")
        self.assertIn("none has a GST code", s["summary"])
        self.assertIn("missing their GST codes", s["summary"])
        self.assertNotIn("Estimate ready", s["summary"])

    def test_a_mix_with_some_gst_codes_still_produces_a_normal_estimate(self):
        w = self.wf()
        cash = self.settings(gst_registration="cash")
        self.run_([line(1100.0, "OUTPUT", "200"), line(-50.0, "NONE")])
        wfs.run_bas_review(self.db, w, cash)
        self.db.commit()
        self.assertIn("Enter G1, 1A and 1B", self.stage(w, cash, "bas")["summary"])

    def test_bas_stage_by_gst_registration(self):
        w = self.wf()
        self.assertEqual(self.stage(w, key="bas")["status"], "needs_you")                          # not set
        self.assertEqual(self.stage(w, self.settings(gst_registration="not_registered"), key="bas")["status"], "na")
        acc = self.stage(w, self.settings(gst_registration="accrual"), key="bas")
        self.assertEqual(acc["status"], "na")
        self.assertIn("invoices and bills", acc["summary"])                                       # says why, rather than guessing
        self.assertEqual(self.stage(w, self.settings(gst_registration="cash"), key="bas")["status"], "not_run")

    def test_bas_needs_the_figures_then_reconciles(self):
        w = self.wf()
        cash = self.settings(gst_registration="cash")
        self.run_([line(1100.0, "OUTPUT", "200"), line(-220.0, "INPUT2")])
        wfs.run_bas_review(self.db, w, cash)
        self.db.commit()
        self.assertIn("Enter G1, 1A and 1B", self.stage(w, cash, "bas")["summary"])
        w.bas_figures = {"G1": 1100, "1A": 100, "1B": 20}
        self.db.add(w)
        wfs.run_bas_review(self.db, w, cash)
        self.db.commit()
        self.assertEqual(self.stage(w, cash, "bas")["status"], "done")
        w.bas_figures = {"G1": 1100, "1A": 100, "1B": 90}
        wfs.run_bas_review(self.db, w, cash)
        self.db.commit()
        s = self.stage(w, cash, "bas")
        self.assertEqual((s["status"], s["summary"]), ("needs_you", "1 open finding(s)"))

    def test_incomplete_tax_information_is_called_out(self):
        w = self.wf()
        cash = self.settings(gst_registration="cash")
        self.run_([line(1100.0, "OUTPUT", "200"), line(-50.0), line(-60.0)])
        r = wfs.run_bas_review(self.db, w, cash)
        self.db.commit()
        self.assertEqual(r["new"], 1)
        self.assertIn("2 of 3 lines in the period have no tax information", self.db.query(WorkflowFinding).one().text)

    def test_sign_off_waits_for_everything_then_becomes_ready(self):
        w = self.wf()
        cash = self.settings(gst_registration="cash")
        s = self.stage(w, cash, "signoff")
        self.assertEqual(s["status"], "waiting")
        self.assertTrue(any("files" in b for b in s["detail"]))
        self.run_([line(1100.0, "OUTPUT", "200"), line(-220.0, "INPUT2")])
        w.checklist = {"bas_summary": True, "profit_and_loss": True, "balance_sheet": True}
        w.bas_figures = {"G1": 1100, "1A": 100, "1B": 20}
        self.db.add(w)
        wfs.run_gl_review(self.db, w, ACCOUNTS, cash)
        wfs.run_bas_review(self.db, w, cash)
        self.db.commit()
        self.assertEqual(self.stage(w, cash, "signoff")["status"], "needs_you")
        w.status, w.signed_off_by, w.signed_off_at = "signed_off", "Jatin", datetime.datetime(2026, 11, 1)
        self.assertIn("Signed off by Jatin", self.stage(w, cash, "signoff")["summary"])

    def test_an_open_high_finding_blocks_sign_off_until_accepted_with_a_note(self):
        w = self.wf()
        cash = self.settings(gst_registration="cash")
        self.run_([line(-100.0, "OUTPUT", "310") for _ in range(3)])                              # income tax code on money out: High
        w.checklist = {"bas_summary": True, "profit_and_loss": True, "balance_sheet": True}
        self.db.add(w)
        wfs.run_gl_review(self.db, w, ACCOUNTS, cash)
        wfs.run_bas_review(self.db, w, cash)
        self.db.commit()
        blockers = wfs.signoff_blockers(wfs.stages(self.db, w, cash), wfs.findings_of(self.db, w))
        self.assertTrue(any("High finding" in b for b in blockers))


# ---------------------------------------------------------------- API
class FakeSystem(LedgerProvider):
    key, label = "xero", "Xero"

    def list_clients(self): return [{"id": "C1", "name": "Acme"}]
    def default_client_id(self): return "C1"
    def list_accounts(self, client_id): return ACCOUNTS
    def fetch_ledger(self, *a, **k): return [], {}
    def apply_changes(self, *a, **k): return {}


class Api(Base):
    P = {"provider": "xero", "client_id": "C1"}

    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(workflow_api.router, prefix="/workflow")

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

    def create(self, start="2026-07-01", **settings):
        self.settings(**{"bas_frequency": "quarterly", "gst_registration": "cash", **settings})
        return self.http.post("/workflow", json={**self.P, "period_start": start})

    def test_creating_needs_bas_frequency_and_defaults_the_period_and_due_date(self):
        r = self.http.post("/workflow", json=self.P)
        self.assertEqual(r.status_code, 400)
        self.assertIn("BAS frequency", r.json()["detail"])
        r = self.create()
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertEqual((b["period"]["label"], b["period"]["start"], b["period"]["end"], b["due_date"], b["status"]),
                         ("Jul-Sep 2026", "2026-07-01", "2026-09-30", "2026-10-28", "not_started"))
        self.assertEqual([s["key"] for s in b["stages"]], ["files", "coding", "gl", "bas", "signoff"])

    def test_period_validation_and_duplicates_and_client_check(self):
        self.settings(bas_frequency="quarterly")
        self.assertEqual(self.http.post("/workflow", json={**self.P, "period_start": "2026-08-01"}).status_code, 400)       # not a quarter start
        self.assertEqual(self.http.post("/workflow", json={**self.P, "period_start": "nonsense"}).status_code, 400)
        self.assertEqual(self.http.post("/workflow", json={**self.P, "period_start": "2026-07-01"}).status_code, 200)
        self.assertEqual(self.http.post("/workflow", json={**self.P, "period_start": "2026-07-01"}).status_code, 409)
        self.assertEqual(self.http.post("/workflow", json={**self.P, "client_id": "NOT-MINE", "period_start": "2026-07-01"}).status_code, 400)

    def test_list_summarises_each_review(self):
        self.create()
        b = self.http.get("/workflow", params=self.P).json()
        self.assertEqual((len(b["workflows"]), b["bas_frequency"], b["gst_registration"]), (1, "quarterly", "cash"))
        self.assertEqual([s["status"] for s in b["workflows"][0]["stages"]][:2], ["needs_you", "needs_you"])

    def test_patch_edits_validates_and_the_bas_figures_trigger_a_recheck(self):
        wid = self.create().json()["id"]
        r = self.http.patch(f"/workflow/{wid}", json={"assigned_to": "Jatin", "notes": "n", "due_date": "2026-11-25", "status": "in_review",
                                                      "checklist": {"profit_and_loss": True}})
        b = r.json()
        self.assertEqual((b["assigned_to"], b["due_date"], b["status"], b["checklist"]["profit_and_loss"], b["checklist"]["balance_sheet"]), ("Jatin", "2026-11-25", "in_review", True, False))
        self.assertEqual(self.http.patch(f"/workflow/{wid}", json={"status": "signed_off"}).status_code, 400)
        self.assertEqual(self.http.patch(f"/workflow/{wid}", json={"due_date": "soon"}).status_code, 400)
        self.assertEqual(self.http.patch(f"/workflow/{wid}", json={"checklist": {"bogus": True}}).status_code, 400)
        self.assertEqual(self.http.patch(f"/workflow/{wid}", json={"bas_figures": {"G99": 1}}).status_code, 400)

        self.run_([line(1100.0, "OUTPUT", "200")])
        self.http.post(f"/workflow/{wid}/run", json={"stage": "bas"})
        b = self.http.patch(f"/workflow/{wid}", json={"bas_figures": {"G1": 5000, "1A": 100}}).json()
        self.assertEqual({f["detail"]["figure"] for f in b["findings"] if f["kind"] == "bas_figure_differs"}, {"G1"})   # re-ran by itself
        b = self.http.patch(f"/workflow/{wid}", json={"bas_figures": {"G1": 1100}}).json()
        self.assertEqual([f["status"] for f in b["findings"] if f["kind"] == "bas_figure_differs"], ["fixed"])

    def test_running_the_reviews_produces_findings_from_the_periods_lines_only(self):
        wid = self.create().json()["id"]
        self.run_([line(-100.0, "OUTPUT", "310") for _ in range(3)] + [line(-100.0, "OUTPUT", "310", date="2026-12-01") for _ in range(9)])
        b = self.http.post(f"/workflow/{wid}/run", json={"stage": "all"}).json()
        self.assertEqual(b["status"], "in_review")
        gl = next(s for s in b["stages"] if s["key"] == "gl")
        self.assertEqual((gl["status"], gl["rating"], gl["detail"]["lines"]), ("done", "RED", 3))                # the 9 December lines are outside
        self.assertEqual(next(f for f in b["findings"] if f["severity"] == "High")["detail"]["times"], 3)
        self.assertEqual(self.http.post(f"/workflow/{wid}/run", json={"stage": "tax"}).status_code, 400)

    def test_findings_can_be_resolved_or_accepted_but_acceptance_needs_a_reason(self):
        wid = self.create().json()["id"]
        self.run_([line(-100.0, "OUTPUT", "310") for _ in range(3)])
        fid = self.http.post(f"/workflow/{wid}/run", json={"stage": "gl"}).json()["findings"][0]["id"]
        self.assertEqual(self.http.post(f"/workflow/{wid}/findings/{fid}", json={"status": "accepted"}).status_code, 400)
        b = self.http.post(f"/workflow/{wid}/findings/{fid}", json={"status": "accepted", "note": "Supplier refund, confirmed with client"}).json()
        f = b["findings"][0]
        self.assertEqual((f["status"], f["note"], b["findings_summary"]["high_open"]), ("accepted", "Supplier refund, confirmed with client", 0))
        self.assertEqual(self.http.post(f"/workflow/{wid}/findings/9999", json={"status": "resolved"}).status_code, 404)
        self.assertEqual(self.http.post(f"/workflow/{wid}/findings/{fid}", json={"status": "weird"}).status_code, 400)

    def test_full_path_to_sign_off_then_it_locks_and_can_be_reopened(self):
        wid = self.create().json()["id"]
        blocked = self.http.post(f"/workflow/{wid}/sign-off", json={})
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("files are incomplete", blocked.json()["detail"])

        self.run_([line(1100.0, "OUTPUT", "200", status="ok"), line(-220.0, "INPUT2", status="ok")])
        self.http.patch(f"/workflow/{wid}", json={"assigned_to": "Jatin", "checklist": {"bas_summary": True, "profit_and_loss": True, "balance_sheet": True},
                                                 "bas_figures": {"G1": 1100, "1A": 100, "1B": 20}})
        v = self.http.post(f"/workflow/{wid}/run", json={"stage": "all"}).json()
        self.assertEqual((v["can_sign_off"], v["blockers"]), (True, []))
        done = self.http.post(f"/workflow/{wid}/sign-off", json={}).json()
        self.assertEqual((done["status"], done["signed_off"]["by"]), ("signed_off", "Jatin"))
        self.assertEqual([s["status"] for s in done["stages"]], ["done", "done", "done", "done", "done"])

        self.assertEqual(self.http.patch(f"/workflow/{wid}", json={"notes": "late edit"}).status_code, 409)         # locked
        self.assertEqual(self.http.post(f"/workflow/{wid}/run", json={"stage": "gl"}).status_code, 409)
        self.assertEqual(self.http.delete(f"/workflow/{wid}").status_code, 409)
        self.assertEqual(self.http.post(f"/workflow/{wid}/reopen").json()["status"], "in_review")
        self.assertEqual(self.http.post(f"/workflow/{wid}/reopen").status_code, 409)
        with Session(self.eng) as s:
            self.assertTrue({"workflow_signoff", "workflow_reopen"} <= {a.action for a in s.query(AuditLog).all()})

    def test_an_accruals_client_gets_an_honest_na_not_a_made_up_bas(self):
        wid = self.create(gst_registration="accrual").json()["id"]
        b = self.http.post(f"/workflow/{wid}/run", json={"stage": "all"}).json()
        bas = next(s for s in b["stages"] if s["key"] == "bas")
        self.assertEqual((bas["status"], b["bas_estimate"]), ("na", None))

    def test_delete_removes_findings_and_other_users_cannot_see_a_review(self):
        wid = self.create().json()["id"]
        self.run_([line(-100.0, "OUTPUT", "310") for _ in range(3)])
        self.http.post(f"/workflow/{wid}/run", json={"stage": "gl"})
        self.assertEqual(self.http.delete(f"/workflow/{wid}").status_code, 200)
        with Session(self.eng) as s:
            self.assertEqual((s.query(ClientWorkflow).count(), s.query(WorkflowFinding).count()), (0, 0))
        self.assertEqual(self.http.get(f"/workflow/{wid}").status_code, 404)
        self.assertEqual(self.http.get("/workflow/not-a-uuid").status_code, 404)


if __name__ == "__main__":
    unittest.main()
