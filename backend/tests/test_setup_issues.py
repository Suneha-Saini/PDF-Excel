"""Ledger-setup issues table. Run: python -m unittest discover -s tests -v  (from backend/)"""
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

from app.api.endpoints import ledger as ledger_api
from app.database import connection as dbconn
from app.database.models import HistoryImport, LedgerRun, User
from app.security import auth
from app.services import providers as prov
from app.services.coding import setup_issues as si
from app.services.providers.base import LedgerProvider

ACCOUNTS = [
    {"code": "200", "name": "Sales - Residential", "class": "REVENUE"},
    {"code": "260", "name": "Interest Income", "class": "REVENUE"},
    {"code": "310", "name": "Contract Installations", "class": "EXPENSE"},
    {"code": "404", "name": "Bank Fees", "class": "EXPENSE"},
    {"code": "477", "name": "Wages and Salaries", "class": "EXPENSE"},
    {"code": "800", "name": "Current Liabilities", "class": "LIABILITY"},
]

_n = 0


def line(code, amount, tax="__absent__", contact="Acme", desc="payment", **kw):
    """tax: a live line's TaxType (None = no tax); "__absent__" = an old line fetched before tax was captured."""
    global _n
    _n += 1
    it = {"id": f"l{_n}", "description": desc, "contact": contact, "reference": "", "amount": amount, "current_code": code, "date": "2026-03-01"}
    if tax != "__absent__":
        it["tax_code"] = tax
    it.update(kw)
    return it


def many(n, *a, **kw):
    return [line(*a, **kw) for _ in range(n)]


def run(lines):
    return si.analyse(lines, ACCOUNTS)


def kinds(result, severity=None):
    return sorted(i["kind"] for i in result["issues"] if severity in (None, i["severity"]))


class Helpers(unittest.TestCase):
    def test_xero_codes_and_export_labels_become_the_same_label(self):
        self.assertEqual(si.canon_tax("INPUT2"), "GST on Expenses")
        self.assertEqual(si.canon_tax("GST on Expenses"), "GST on Expenses")
        self.assertEqual(si.canon_tax("exemptoutput"), "GST Free Income")
        self.assertIsNone(si.canon_tax(None))
        self.assertIsNone(si.canon_tax("  "))

    def test_tax_side_and_gst_bearing(self):
        self.assertEqual([si.tax_side(x) for x in ("GST on Income", "GST Free Income", "GST on Expenses", "GST Free Expenses", "GST on Capital", "BAS Excluded", "(none)", None)],
                         ["income", "income", "expense", "expense", "expense", None, None, None])
        self.assertEqual([si.gst_bearing(x) for x in ("GST on Income", "GST on Expenses", "GST Free Expenses", "BAS Excluded", "(none)", "NON", None)],
                         [True, True, False, False, False, False, False])


class HighSeverity(unittest.TestCase):
    def test_tax_code_on_the_wrong_side_of_the_transaction(self):
        r = run(many(5, "310", -100.0, "OUTPUT", contact="ITP Home Energy") + many(4, "200", 100.0, "INPUT2", contact="Customer"))
        high = [i for i in r["issues"] if i["severity"] == "High"]
        self.assertEqual(sorted(i["issue"] for i in high), ["an expense tax code on money in", "an income tax code on money out"])
        out = next(i for i in high if "money out" in i["issue"])
        self.assertEqual((out["payee"], out["direction"], out["account_name"], out["tax"], out["times"]),
                         ("ITP Home Energy", "Spend", "Contract Installations", "GST on Income", 5))

    def test_correct_sides_and_neutral_codes_are_not_flagged(self):
        r = run(many(5, "310", -50.0, "INPUT2") + many(5, "200", 50.0, "OUTPUT") + many(5, "310", -50.0, "BASEXCLUDED") + many(3, "310", -50.0, "EXEMPTINPUT"))
        self.assertEqual(kinds(r, "High"), [])

    def test_imported_labels_are_judged_like_live_codes(self):
        imported = [line("310", -40.0, contact="X", tax_label="GST on Income") for _ in range(3)]       # no tax_code key: an import
        self.assertEqual(kinds(run(imported), "High"), ["tax_direction"])


class MediumSeverity(unittest.TestCase):
    def test_money_out_to_an_income_account_and_money_in_to_an_expense_account(self):
        r = run(many(2, "200", -80.0, contact="Refunded Customer") + many(2, "310", 80.0, contact="Supplier Refund") + many(3, "200", 80.0))
        texts = sorted(i["issue"] for i in r["issues"] if i["kind"] == "direction_vs_account")
        self.assertEqual(len(texts), 2)
        self.assertTrue(any("refund to a customer" in t for t in texts) and any("refund from a supplier" in t for t in texts))

    def test_gst_on_an_account_that_is_usually_gst_free(self):
        r = run(many(4, "404", -12.0, "INPUT2", contact="Bank") + many(3, "477", -900.0, "INPUT2", contact="Staff") +
                many(3, "404", -12.0, "EXEMPTINPUT", contact="Bank 2") + many(3, "310", -100.0, "INPUT2", contact="Tradie"))
        flagged = sorted(i["account_name"] for i in r["issues"] if i["kind"] == "gst_on_gst_free_account")
        self.assertEqual(flagged, ["Bank Fees", "Wages and Salaries"])
        self.assertIn("usually GST free or no GST", next(i for i in r["issues"] if i["kind"] == "gst_on_gst_free_account")["issue"])

    def test_the_gst_heuristic_does_not_run_for_a_client_that_is_not_on_gst(self):
        r = run(many(5, "404", -12.0, "NON", contact="Bank") + many(5, "310", -12.0, "TAX", contact="Shop"))
        self.assertEqual(kinds(r), [])
        self.assertFalse(r["coverage"]["gst_checks_ran"])

    def test_the_same_payee_with_two_tax_codes_on_one_account(self):
        r = run(many(10, "200", 110.0, "OUTPUT", contact="Bridge Select") + many(3, "200", 110.0, "EXEMPTOUTPUT", contact="Bridge Select"))
        row = next(i for i in r["issues"] if i["kind"] == "payee_two_tax_codes")
        self.assertEqual((row["severity"], row["payee"], row["times"]), ("Medium", "Bridge Select", 13))
        self.assertIn("'GST on Income' 10", row["issue"])
        self.assertIn("'GST Free Income' 3", row["issue"])

    def test_two_tax_codes_across_different_payees_is_only_info(self):
        r = run(many(10, "200", 110.0, "OUTPUT", contact="A") + many(4, "200", 110.0, "EXEMPTOUTPUT", contact="B"))
        self.assertNotIn("payee_two_tax_codes", kinds(r))
        self.assertEqual(kinds(r, "Medium"), [])


class NoiseReduction(unittest.TestCase):
    def test_direction_vs_account_is_one_row_per_payee_and_account_whatever_the_tax_label(self):
        ls = many(2, "310", 80.0, None, contact="Supplier Refund") + many(3, "310", 80.0, "NON", contact="Supplier Refund")
        rows = [i for i in run(ls)["issues"] if i["kind"] == "direction_vs_account"]
        self.assertEqual((len(rows), rows[0]["times"]), (1, 5))

    def test_no_tax_next_to_a_non_taxable_code_is_not_a_second_tax_code(self):
        ls = many(14, "310", -50.0, "NON", contact="A") + many(7, "310", -50.0, None, contact="B")      # QuickBooks, no GST
        r = run(ls)
        self.assertEqual((r["multi_tax_accounts"], kinds(r)), ([], []))
        same_payee = many(6, "310", -50.0, "NON", contact="C") + many(4, "310", -50.0, None, contact="C")
        self.assertNotIn("payee_two_tax_codes", kinds(run(same_payee)))

    def test_no_tax_next_to_a_gst_code_is_still_a_real_inconsistency(self):
        r = run(many(10, "310", -50.0, "INPUT2", contact="A") + many(4, "310", -50.0, None, contact="B"))
        self.assertEqual(len(r["multi_tax_accounts"]), 1)
        self.assertEqual([i["payee"] for i in r["issues"] if i["kind"] == "account_multi_tax"], ["B"])


class InfoSeverity(unittest.TestCase):
    def test_accounts_with_more_than_one_tax_code_are_tabulated_and_only_minority_payees_listed(self):
        r = run(many(59, "200", 110.0, "OUTPUT", contact="Bridge Select") + many(20, "200", 110.0, "EXEMPTOUTPUT", contact="Solar Smart"))
        t = r["multi_tax_accounts"]
        self.assertEqual((len(t), t[0]["account_name"], t[0]["lines"]), (1, "Sales - Residential", 79))
        self.assertEqual([(x["tax"], x["lines"]) for x in t[0]["tax_codes"]], [("GST on Income", 59), ("GST Free Income", 20)])
        info = [i for i in r["issues"] if i["kind"] == "account_multi_tax"]
        self.assertEqual([(i["payee"], i["tax"], i["times"]) for i in info], [("Solar Smart", "GST Free Income", 20)])   # not the 59
        self.assertIn("confirm which is right per payee", info[0]["issue"])

    def test_live_codes_and_imported_labels_of_the_same_meaning_are_not_two_tax_codes(self):
        live = many(10, "310", -50.0, "INPUT2", contact="Tradie")
        imported = [line("310", -50.0, contact="Tradie", tax_label="GST on Expenses") for _ in range(10)]
        self.assertEqual(run(live + imported)["multi_tax_accounts"], [])

    def test_a_payee_coded_to_several_accounts(self):
        r = run(many(6, "310", -50.0, contact="Hardware Co") + many(4, "404", -50.0, contact="Hardware Co"))
        row = next(i for i in r["issues"] if i["kind"] == "payee_split_accounts")
        self.assertEqual((row["severity"], row["payee"], row["times"]), ("Info", "Hardware Co", 10))
        self.assertIn("2 accounts", row["issue"])

    def test_a_dominant_account_or_too_few_lines_is_not_a_split(self):
        r = run(many(19, "310", -50.0, contact="Hardware Co") + many(1, "404", -50.0, contact="Hardware Co") + many(2, "310", -9.0, contact="Rare") + many(2, "404", -9.0, contact="Rare"))
        self.assertNotIn("payee_split_accounts", kinds(r))


class CoverageAndOrdering(unittest.TestCase):
    def test_lines_without_tax_info_are_skipped_not_called_untaxed(self):
        old = many(30, "404", -12.0, contact="Bank")                          # fetched before tax capture
        blank_import = [line("404", -12.0, contact="Bank", tax_label="") for _ in range(10)]
        r = run(old + blank_import)
        self.assertEqual(r["coverage"], {"coded_lines": 40, "lines_with_tax_info": 0, "gst_checks_ran": False,
                                         "gst_registration": "unknown", "entity_type": "unknown"})
        self.assertEqual(kinds(r), [])
        # direction/account-type checks still work without tax
        r2 = run(many(3, "200", -10.0, contact="Refund"))
        self.assertEqual(kinds(r2), ["direction_vs_account"])

    def test_no_tax_on_a_live_line_is_a_real_value_but_not_a_gst_code(self):
        r = run(many(5, "404", -12.0, None, contact="Bank") + many(5, "310", -50.0, "INPUT2", contact="Shop"))
        self.assertEqual(r["coverage"]["lines_with_tax_info"], 10)
        self.assertEqual(kinds(r), [])

    def test_sorted_high_then_medium_then_info_and_by_times(self):
        r = run(many(2, "310", -50.0, "OUTPUT", contact="High A") + many(9, "310", -50.0, "OUTPUT", contact="High B") +
                many(3, "200", -10.0, "BASEXCLUDED", contact="Med") + many(6, "310", -5.0, "INPUT2", contact="Split") + many(4, "404", -5.0, "INPUT2", contact="Split"))
        sev = [i["severity"] for i in r["issues"]]
        self.assertEqual(sev, sorted(sev, key=("High", "Medium", "Info").index))
        highs = [i for i in r["issues"] if i["severity"] == "High"]
        self.assertEqual([h["times"] for h in highs], [9, 2])
        self.assertEqual(r["summary"], {"High": sum(1 for s in sev if s == "High"), "Medium": sev.count("Medium"), "Info": sev.count("Info")})

    def test_accounts_not_in_the_chart_and_empty_input_are_ignored_safely(self):
        self.assertEqual(run(many(3, "999", -10.0, "OUTPUT"))["issues"], [])
        r = run([])
        self.assertEqual((r["issues"], r["multi_tax_accounts"], r["summary"]), ([], [], {"High": 0, "Medium": 0, "Info": 0}))
        self.assertIn("questions for an accountant", r["note"])

    def test_sample_line_ids_are_included_for_drilling_in(self):
        ls = many(8, "310", -50.0, "OUTPUT", contact="X")
        row = run(ls)["issues"][0]
        self.assertEqual(len(row["line_ids"]), 5)
        self.assertTrue(set(row["line_ids"]) <= {l["id"] for l in ls})


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
            self.user = User(username="u", hashed_password="x")
            s.add(self.user)
            s.commit()
            s.refresh(self.user)
            self.uid = self.user.id
            live = many(5, "310", -50.0, "OUTPUT", contact="ITP Home Energy")
            for it in live:
                it.update(date="2026-03-02", status="ok", source=None, proposed_code=None)
            s.add(LedgerRun(user_id=self.user.id, provider="xero", client_id="C1", organization="Acme", date_from="a", date_to="b",
                            items=live, accounts=ACCOUNTS))
            imp = [dict(line("200", 80.0, contact=f"Cust {i}", tax_label="GST on Income"), origin="import") for i in range(3)]
            s.add(HistoryImport(user_id=self.user.id, provider="xero", client_id="C1", filename="f.csv", items=imp, lines=3))
            s.commit()
        app = FastAPI()
        app.include_router(ledger_api.router, prefix="/ledger")

        def test_db():
            with Session(self.eng) as s:
                yield s

        app.dependency_overrides[dbconn.get_db] = test_db
        from types import SimpleNamespace
        app.dependency_overrides[auth.get_current_active_user] = lambda: SimpleNamespace(id=self.uid)   # only .id is used
        self.http = TestClient(app)
        p = mock.patch.object(prov, "provider_for", return_value=FakeSystem())
        p.start()
        self.addCleanup(p.stop)

    def test_the_endpoint_learns_from_live_runs_and_imports_together(self):
        r = self.http.get("/ledger/setup-issues", params={"provider": "xero", "client_id": "C1"})
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertEqual((b["client"], b["history_lines"]), ("Acme", 8))
        self.assertEqual(b["summary"]["High"], 1)
        self.assertEqual(b["issues"][0]["payee"], "ITP Home Energy")
        self.assertEqual(b["coverage"]["lines_with_tax_info"], 8)

    def test_a_client_the_login_does_not_have_is_rejected(self):
        self.assertEqual(self.http.get("/ledger/setup-issues", params={"provider": "xero", "client_id": "NOT-MINE"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
