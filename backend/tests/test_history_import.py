"""Ledger-export import. Run: python -m unittest discover -s tests -v  (from backend/)

The fixtures imitate Xero "Account Transactions", QuickBooks "Transaction Detail by Account" and a flat CSV from
knowledge of those report layouts; they are NOT captured from real files (see ledger_import.py).
"""
import datetime
import io
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

from app.api.endpoints import history as history_api
from app.database import connection as dbconn
from app.database.models import HistoryImport, User
from app.security import auth
from app.services import ledger_coding as lc
from app.services import providers as prov
from app.services.coding import evaluate, ledger_import as li
from app.services.coding.history import HistoryIndex
from app.services.coding.store import load_history
from app.services.providers.base import LedgerProvider


# ---------------------------------------------------------------- fixtures
def xero_style_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Account Transactions"])
    ws.append(["Solar4Life"])
    ws.append(["For the period 1 November 2025 to 31 October 2026"])
    ws.append([])
    ws.append(["Date", "Source", "Description", "Reference", "Debit", "Credit", "Running Balance", "Gross", "GST"])
    ws.append(["Sales (200)"])
    ws.append(["Opening Balance", None, None, None, None, None, 0])
    for i in range(1, 4):
        ws.append([datetime.datetime(2026, 3, i), "Receive Money", f"BRIDGE SELECT INV {1000 + i}", None, None, 110.0, 110.0 * i, 110.0, 10.0])
    ws.append(["Total Sales", None, None, None, 0, 330.0])
    ws.append(["Bank Fees (404)"])
    ws.append([datetime.datetime(2026, 3, 9), "Spend Money", "Bank fee monthly", None, 12.5, None, 12.5, 12.5, 0.0])
    ws.append([datetime.datetime(2026, 3, 10), "Spend Money", "Refund of bank fee", None, None, 12.5, 0.0, 12.5, 0.0])
    ws.append(["Total Bank Fees", None, None, None, 12.5, 12.5])
    ws.append(["Business Bank Account"])
    ws.append([datetime.datetime(2026, 3, 1), "Receive Money", "BRIDGE SELECT INV 1001", None, 110.0, None, 110.0])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


QBO_STYLE_CSV = (
    "Transaction Detail by Account\n"
    "Sandbox Company\n"
    "March 1-31, 2026\n"
    "\n"
    "Date,Transaction Type,Num,Name,Memo/Description,Split,Amount,Balance\n"
    "Expenses:Repairs\n"
    "03/14/2026,Expense,101,Bunnings,Timber and screws,Visa,-80.00,-80.00\n"
    "03/15/2026,Expense,102,Bunnings,Paint,Visa,\"-1,250.50\",\"-1,330.50\"\n"
    "Total for Expenses:Repairs,,,,,,\"-1,330.50\",\n"
    "Sales\n"
    "03/20/2026,Deposit,,Acme,Job 5 payment,Cheque,300.00,300.00\n"
    "Total for Sales,,,,,,300.00,\n"
    "Visa\n"
    "03/14/2026,Expense,101,Bunnings,Timber and screws,Expenses:Repairs,-80.00,-80.00\n"
)

FLAT_CSV = (
    "Date,Description,Payee,Account,Debit,Credit,Tax Code\n"
    "25/03/2026,Card purchase,Officeworks,Office Supplies,45.00,,GST on Expenses\n"
    "26/03/2026,Card purchase,Officeworks,Office Supplies,20.00,,GST on Expenses\n"
    "27/03/2026,Customer receipt,Acme,Sales,,500.00,GST on Income\n"
)

CHART = [
    {"code": "200", "name": "Sales", "type": "REVENUE", "class": "REVENUE"},
    {"code": "404", "name": "Bank Fees", "type": "EXPENSE", "class": "EXPENSE"},
    {"code": "7", "name": "6100 Expenses:Repairs", "type": "Expense", "class": "Expense"},
    {"code": "9", "name": "Sales", "type": "Income", "class": "Revenue"},
    {"code": "12", "name": "Office Supplies", "type": "Expense", "class": "Expense"},
]


def parse(rows, **kw):
    header_idx, cols = li.detect_columns(rows)
    return header_idx, cols, li.parse_rows(rows, header_idx, cols, **kw)


# ---------------------------------------------------------------- parsing
class Reading(unittest.TestCase):
    def test_csv_and_xlsx_are_read_and_unsupported_types_are_refused(self):
        self.assertGreater(len(li.read_rows("a.csv", QBO_STYLE_CSV.encode())), 10)
        self.assertEqual(li.read_rows("a.xlsx", xero_style_xlsx())[4][0], "Date")
        with self.assertRaisesRegex(li.ImportFormatError, "Unsupported"):
            li.read_rows("a.pdf", b"%PDF")
        with self.assertRaisesRegex(li.ImportFormatError, "empty"):
            li.read_rows("a.csv", b"")

    def test_semicolon_and_tab_delimiters_and_cp1252(self):
        rows = li.read_rows("a.csv", "Date;Description;Amount\n01/03/2026;Café;10,00\n".encode("cp1252"))
        self.assertEqual(rows[1][1], "Café")
        self.assertEqual(li.read_rows("a.tsv", b"Date\tAmount\n01/03/2026\t5")[0], ["Date", "Amount"])


class Cells(unittest.TestCase):
    def test_numbers(self):
        for raw, want in [("1,234.56", 1234.56), ("(120.00)", -120.0), ("-5", -5.0), ("$ 10", 10.0), (7, 7.0), ("12-", -12.0)]:
            self.assertEqual(li.parse_number(raw), want, raw)
        for raw in (None, "", "-", "abc", True):
            self.assertIsNone(li.parse_number(raw), repr(raw))

    def test_dates_respect_day_first_and_excel_datetimes(self):
        self.assertEqual(li.parse_date("03/04/2026", dayfirst=True), datetime.date(2026, 4, 3))
        self.assertEqual(li.parse_date("03/04/2026", dayfirst=False), datetime.date(2026, 3, 4))
        self.assertEqual(li.parse_date("17 Nov 2026"), datetime.date(2026, 11, 17))
        self.assertEqual(li.parse_date("2026-11-17"), datetime.date(2026, 11, 17))
        self.assertEqual(li.parse_date(datetime.datetime(2026, 1, 2, 5)), datetime.date(2026, 1, 2))
        self.assertIsNone(li.parse_date("Opening Balance"))

    def test_account_codes_and_names(self):
        self.assertEqual(li.extract_code("Sales (200)"), "200")
        self.assertEqual(li.extract_code("6100 Repairs"), "6100")
        self.assertIsNone(li.extract_code("Repairs"))
        self.assertEqual(li.normalise_account("6100 Expenses : Repairs"), "expenses:repairs")


class XeroStyle(unittest.TestCase):
    def test_grouped_sections_signs_and_skipped_rows(self):
        rows = li.read_rows("x.xlsx", xero_style_xlsx())
        header_idx, cols, p = parse(rows)
        self.assertEqual((header_idx, p["grouped"]), (4, True))
        self.assertEqual(p["sections"], {"Sales (200)": 3, "Bank Fees (404)": 2, "Business Bank Account": 1})
        sales = [l for l in p["lines"] if l["account"] == "Sales (200)"]
        self.assertTrue(all(l["amount"] == 110.0 for l in sales))                 # credit to revenue = money in
        fees = {l["description"]: l["amount"] for l in p["lines"] if l["account"] == "Bank Fees (404)"}
        self.assertEqual(fees, {"Bank fee monthly": -12.5, "Refund of bank fee": 12.5})   # debit out, credit back in
        self.assertEqual({l["code_in_file"] for l in sales}, {"200"})
        self.assertEqual(p["skipped"], {"total/balance/other rows": 3})       # the date-less "Opening Balance" row + two Total rows
        self.assertEqual(sales[0]["date"], "2026-03-01")

    def test_flip_sign_option(self):
        rows = li.read_rows("x.xlsx", xero_style_xlsx())
        _, _, p = parse(rows, amount_sign="flip")
        self.assertEqual(next(l for l in p["lines"] if l["description"] == "Bank fee monthly")["amount"], 12.5)


class QboStyle(unittest.TestCase):
    def test_grouped_csv_with_month_first_dates_and_signed_amounts(self):
        rows = li.read_rows("q.csv", QBO_STYLE_CSV.encode())
        _, cols, p = parse(rows, dayfirst=False)
        self.assertEqual(p["sections"], {"Expenses:Repairs": 2, "Sales": 1, "Visa": 1})
        rep = [l for l in p["lines"] if l["account"] == "Expenses:Repairs"]
        self.assertEqual([l["amount"] for l in rep], [-80.0, -1250.5])
        self.assertEqual((rep[0]["contact"], rep[0]["reference"], rep[0]["date"]), ("Bunnings", "101", "2026-03-14"))
        self.assertEqual(cols["contact"], 3)


ACCOUNT_NORMAL_CSV = (
    "Date,Transaction Type,Num,Name,Memo/Description,Split,Amount,Balance\n"
    "Expenses:Repairs\n"
    + "".join(f"03/{d:02d}/2026,Expense,{d},Bunnings,Timber {d},Visa,{50 + d}.00,0\n" for d in range(10, 17))     # 7 expenses, positive
    + "03/20/2026,Expense,99,Bunnings,Returned timber,Visa,-20.00,0\n"                                              # a refund, negative
    + "Sales\n"
    "03/21/2026,Deposit,,Acme,Job payment,Cheque,300.00,0\n"
)


class SignByAccountType(unittest.TestCase):
    CLASSES = {"7": "Expense", "9": "Revenue"}

    def test_class_sign(self):
        self.assertEqual([li.class_sign(c) for c in ("Expense", "EXPENSE", "Cost of Goods Sold", "Asset", "Revenue", "Liability", "Equity", None)],
                         [-1, -1, -1, -1, 1, 1, 1, None])

    def test_account_normal_amounts_become_money_out_and_money_in(self):
        rows = li.read_rows("q.csv", ACCOUNT_NORMAL_CSV.encode())
        lines = parse(rows, dayfirst=False)[2]["lines"]
        fixed = li.apply_sign_by_class(lines, {"Expenses:Repairs": "7", "Sales": "9"}, self.CLASSES)
        repairs = [l["amount"] for l in fixed if l["account"] == "Expenses:Repairs"]
        self.assertEqual(repairs[0], -60.0)                       # +60 expense -> money out
        self.assertEqual(repairs[-1], 20.0)                       # -20 expense refund -> money in
        self.assertEqual(next(l["amount"] for l in fixed if l["account"] == "Sales"), 300.0)
        self.assertEqual(lines[0]["amount"], 60.0)                # the originals are not mutated

    def test_unmapped_or_unknown_class_keeps_the_files_sign(self):
        lines = [{"account": "X", "amount": 5.0}, {"account": "Y", "amount": 6.0}]
        out = li.apply_sign_by_class(lines, {"X": None, "Y": "1"}, {"1": None})
        self.assertEqual([l["amount"] for l in out], [5.0, 6.0])

    def test_hint_fires_for_account_normal_files_only(self):
        rows = li.read_rows("q.csv", ACCOUNT_NORMAL_CSV.encode())
        lines = parse(rows, dayfirst=False)[2]["lines"]
        self.assertIn("By account type", li.sign_hint(lines, {"Expenses:Repairs": "7", "Sales": "9"}, self.CLASSES))
        signed = li.apply_sign_by_class(lines, {"Expenses:Repairs": "7", "Sales": "9"}, self.CLASSES)
        self.assertIsNone(li.sign_hint(signed, {"Expenses:Repairs": "7", "Sales": "9"}, self.CLASSES))


class BalanceRows(unittest.TestCase):
    CSV = ("Date,Description,Debit,Credit\nTools\n"
           "01/03/2026,OPENING BALANCE,,500.00\n"
           "02/03/2026,Total Tools drill bits,30.00,\n"
           "03/03/2026,Balance Bay Cafe lunch,12.00,\n"
           "04/03/2026,Total for Tools,42.00,\n")

    def test_real_payees_starting_with_total_or_balance_are_kept_only_balance_rows_are_skipped(self):
        p = parse(li.read_rows("t.csv", self.CSV.encode()))[2]
        self.assertEqual([l["description"] for l in p["lines"]], ["Total Tools drill bits", "Balance Bay Cafe lunch"])
        self.assertEqual(p["skipped"], {"opening/closing balance rows": 2})


class FlatCsv(unittest.TestCase):
    def test_account_column_drives_the_account_and_tax_label_is_kept(self):
        rows = li.read_rows("f.csv", FLAT_CSV.encode())
        _, _, p = parse(rows)
        self.assertFalse(p["grouped"])
        self.assertEqual(p["sections"], {"Office Supplies": 2, "Sales": 1})
        self.assertEqual([l["amount"] for l in p["lines"]], [-45.0, -20.0, 500.0])
        self.assertEqual(p["lines"][0]["tax_label"], "GST on Expenses")
        self.assertEqual(p["lines"][0]["date"], "2026-03-25")

    def test_undetectable_files_return_none(self):
        self.assertIsNone(li.detect_columns([["Hello", "World"], ["1", "2"]]))


# ---------------------------------------------------------------- mapping to the chart
class SectionMapping(unittest.TestCase):
    def test_suggestions(self):
        s = li.suggest_sections(["Sales (200)", "Bank Fees (404)", "Expenses:Repairs", "Repairs", "Business Bank Account",
                                 "Mystery Account", "Visa"], CHART)
        self.assertEqual((s["Sales (200)"]["code"], s["Sales (200)"]["how"]), ("200", "account code"))
        self.assertEqual(s["Bank Fees (404)"]["code"], "404")
        self.assertEqual(s["Expenses:Repairs"]["code"], "7")                       # QBO naming: "6100 Expenses:Repairs"
        self.assertEqual(s["Repairs"]["code"], "7")                                # unique last segment
        self.assertTrue(s["Business Bank Account"]["ignore"] and s["Visa"]["ignore"])
        self.assertEqual((s["Mystery Account"]["code"], s["Mystery Account"]["ignore"]), (None, False))

    def test_ambiguous_last_segment_is_not_guessed(self):
        chart = [{"code": "1", "name": "Income:Sales"}, {"code": "2", "name": "Other:Sales"}]
        self.assertIsNone(li.suggest_sections(["Sales"], chart)["Sales"]["code"])


class BuildItems(unittest.TestCase):
    def lines(self):
        rows = li.read_rows("f.csv", FLAT_CSV.encode())
        return parse(rows)[2]["lines"]

    def test_only_mapped_accounts_become_history_and_ids_are_stable_content_hashes(self):
        items, stats = li.build_items(self.lines(), {"Office Supplies": "12", "Sales": None})
        self.assertEqual((len(items), stats["ignored_lines"]), (2, 1))
        self.assertEqual(items[0]["current_code"], "12")
        self.assertEqual(items[0]["origin"], "import")
        again, _ = li.build_items(self.lines(), {"Office Supplies": "12"})
        self.assertEqual([i["id"] for i in items], [i["id"] for i in again])          # re-import => same ids => no double count

    def test_identical_rows_in_one_file_stay_distinct(self):
        ln = self.lines()[0]
        items, _ = li.build_items([ln, dict(ln), dict(ln)], {"Office Supplies": "12"})
        self.assertEqual(len({i["id"] for i in items}), 3)


# ---------------------------------------------------------------- history behaviour
def live(desc="BRIDGE SELECT INV", contact="Bridge Select", amount=110.0, date="2026-06-01", **kw):
    it = {"id": f"live{desc}{date}{amount}", "tx_id": "t", "line_index": 0, "date": date, "description": desc,
          "contact": contact, "reference": "", "type": "RECEIVE", "direction": "receive", "amount": amount,
          "current_code": None, "tax_code": None, "proposed_code": None, "source": None, "confidence": None,
          "reason": "", "status": "unassigned", "bank_account": "Cheque"}
    it.update(kw)
    return it


def imported(n, code="200", desc="BRIDGE SELECT INV", contact="Bridge Select", amount=110.0, start=datetime.date(2025, 11, 1)):
    d = (start + datetime.timedelta(days=n)).isoformat()
    return {"id": f"imp:{n}:{code}", "tx_id": f"imp:{n}", "line_index": 0, "date": d, "description": f"{desc} {n}",
            "contact": contact, "reference": "", "type": "IMPORT", "direction": "receive", "amount": amount,
            "current_code": code, "tax_label": "", "origin": "import"}


ACCOUNTS = [{"code": "200", "name": "Sales", "class": "REVENUE"}, {"code": "260", "name": "Other Revenue", "class": "REVENUE"}]


class HistoryFromImports(unittest.TestCase):
    def test_imported_history_drives_a_high_level_suggestion_on_a_live_line(self):
        target = live()
        lc.apply_history([target], [imported(i) for i in range(40)], ACCOUNTS)
        self.assertEqual((target["proposed_code"], target["source"], target["level"]), ("200", "history", "high"))

    def test_a_live_line_never_predicts_itself_through_its_imported_twin(self):
        target = live(date="2026-03-05", amount=110.0)
        twin = imported(0, code="260")
        twin["date"], twin["description"] = "2026-03-05", "BRIDGE SELECT INV"        # same transaction, seen in the export
        lc.apply_history([target], [twin], ACCOUNTS)
        self.assertIsNone(target["source"])                                           # no evidence, rather than a self-fulfilling one
        # ...but the twin is excluded only for that line: other imported lines still count
        target2 = live(date="2026-03-05", amount=110.0)
        lc.apply_history([target2], [twin] + [imported(i) for i in range(10, 40)], ACCOUNTS)
        self.assertEqual(target2["proposed_code"], "200")

    def test_imported_lines_can_still_be_backtested_against_each_other(self):
        r = evaluate.backtest([imported(i) for i in range(30)], {"200": "Sales"})
        self.assertEqual(r["by_level"]["high"]["agree_with_books_pct"], 100.0)

    def test_the_backtest_is_capped_so_big_imports_stay_fast(self):
        items = [imported(i, code="200" if i % 2 else "260", desc="STORE A" if i % 2 else "STORE B", contact="") for i in range(6500)]
        t0 = time.time()
        r = evaluate.backtest(items, {"200": "Sales", "260": "Other Revenue"})
        self.assertEqual((r["coded_lines"], r["evaluated"] <= 3000), (6500, True))
        self.assertLess(time.time() - t0, 60)

    def test_very_common_tokens_do_not_blow_up_candidate_search(self):
        pool = [imported(i, desc="COMMONWORD", contact="") for i in range(5000)]
        idx = HistoryIndex(pool)
        t0 = time.time()
        idx.evidence_for(live(desc="COMMONWORD", contact=""))
        self.assertLess(time.time() - t0, 5)


class ImportedTwinsOfLiveLines(unittest.TestCase):
    def test_an_imported_line_that_is_the_same_transaction_as_a_live_line_is_counted_once(self):
        from app.database.models import LedgerRun
        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(eng)
        with Session(eng) as s:
            u = User(username="u", hashed_password="x")
            s.add(u)
            s.commit()
            s.refresh(u)
            lv = live(desc="Timber and screws", contact="Bunnings", amount=-80.0, date="2026-03-14", current_code="7")
            s.add(LedgerRun(user_id=u.id, provider="xero", client_id="C1", organization="Acme", date_from="a", date_to="b",
                            items=[lv], accounts=[]))
            twin = imported(0, code="7", desc="Timber and screws", contact="Bunnings", amount=-80.0)
            twin.update(date="2026-03-14", description="Timber and screws")
            other_day = imported(1, code="7", desc="Timber and screws", contact="Bunnings", amount=-80.0)        # same payee/amount, other date
            same_day_other_payee = imported(2, code="7", desc="Cafe lunch", contact="Cafe", amount=-80.0)
            same_day_other_payee.update(date="2026-03-14")
            s.add(HistoryImport(user_id=u.id, provider="xero", client_id="C1", filename="f", lines=3,
                                items=[twin, other_day, same_day_other_payee]))
            s.commit()
            ids = {i["id"] for i in load_history(s, u.id, "xero", "C1", "Acme")}
            self.assertNotIn(twin["id"], ids)                                   # duplicate of the live line
            self.assertIn(other_day["id"], ids)                                 # genuine repeat purchase: kept
            self.assertIn(same_day_other_payee["id"], ids)                      # coincidence on date+amount: kept
            self.assertIn(lv["id"], ids)


class LoadHistoryWithImports(unittest.TestCase):
    def test_imports_are_included_per_client_and_dedupe_with_live_lines(self):
        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(eng)
        with Session(eng) as s:
            u = User(username="u", hashed_password="x")
            s.add(u)
            s.commit()
            s.refresh(u)
            one, two = [imported(i) for i in range(3)], [imported(i) for i in range(2, 5)]   # overlapping exports (ids 2 shared)
            for client, items in (("C1", one), ("C1", two), ("C2", [imported(99)])):
                s.add(HistoryImport(user_id=u.id, provider="xero", client_id=client, filename="f.csv", items=items, lines=len(items)))
            s.commit()
            got = load_history(s, u.id, "xero", "C1", "Acme")
            self.assertEqual(sorted(i["id"] for i in got), sorted({i["id"] for i in one + two}))
            self.assertEqual(len(got), 5)                                                     # not 6: the shared line counted once
            self.assertEqual(len(load_history(s, u.id, "xero", "C2", "Other")), 1)           # never crosses clients


# ---------------------------------------------------------------- API
class FakeSystem(LedgerProvider):
    key, label = "xero", "Xero"

    def list_clients(self): return [{"id": "T1", "name": "Solar4Life"}]
    def default_client_id(self): return "T1"
    def list_accounts(self, client_id): return CHART
    def fetch_ledger(self, *a, **k): return [], {}
    def apply_changes(self, *a, **k): return {}


class ImportApi(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            self.user = User(username="u", hashed_password="x")
            s.add(self.user)
            s.commit()
            s.refresh(self.user)
        app = FastAPI()
        app.include_router(history_api.router, prefix="/history")

        def test_db():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[dbconn.get_db] = test_db
        app.dependency_overrides[auth.get_current_active_user] = lambda: self.user
        self.http = TestClient(app)
        overview = [{"key": "xero", "label": "Xero", "status": "connected", "ready": True, "verified": True, "supports_apply": True, "note": ""}]
        for p in (mock.patch.object(prov, "provider_overview", return_value=overview),
                  mock.patch.object(prov, "provider_for", return_value=FakeSystem())):
            p.start()
            self.addCleanup(p.stop)

    def post(self, path, data, name="x.xlsx", content=None, **extra):
        content = content if content is not None else xero_style_xlsx()
        return self.http.post(f"/history/{path}", data={"provider": "xero", "client_id": "T1", **data, **extra},
                              files={"file": (name, content, "application/octet-stream")})

    def test_preview_detects_layout_columns_and_proposes_account_mapping_without_storing_anything(self):
        r = self.post("preview", {})
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertEqual((b["layout"], b["header_row"], b["lines"], b["client"]), ("grouped", 4, 6, "Solar4Life"))
        sec = {s["name"]: s for s in b["sections"]}
        self.assertEqual((sec["Sales (200)"]["code"], sec["Bank Fees (404)"]["code"]), ("200", "404"))
        self.assertTrue(sec["Business Bank Account"]["ignore"])
        self.assertEqual((b["date_from"], b["date_to"]), ("2026-03-01", "2026-03-10"))
        self.assertEqual(b["columns"]["debit"], 4)
        self.assertTrue(any("No payee/name column" in w for w in b["warnings"]))
        with Session(self.engine) as s:
            self.assertEqual(s.query(HistoryImport).count(), 0)

    def test_unrecognised_file_returns_rows_so_the_user_can_pick_the_header(self):
        b = self.post("preview", {}, name="x.csv", content=b"Hello,World\nfoo,bar\n").json()
        self.assertFalse(b["detected"])
        self.assertEqual(b["preview_rows"][0], ["Hello", "World"])
        # the user points at row 0 and maps columns by hand
        b = self.post("preview", {"mapping": '{"header_row":0,"columns":{"date":0,"amount":1}}'}, name="x.csv",
                      content=b"Hello,World\n01/03/2026,10\n").json()
        self.assertEqual((b["valid"], b["lines"]), (True, 0))                         # valid mapping, but no account => none importable
        self.assertTrue(any("No transaction rows" in w for w in b["warnings"]))

    def test_import_stores_only_the_mapped_accounts_as_one_undoable_batch(self):
        smap = '{"Sales (200)":"200","Bank Fees (404)":"404","Business Bank Account":null}'
        r = self.post("import", {"section_map": smap})
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertEqual((b["imported"], b["ignored"], b["history_lines_total"]), (5, 1, 5))
        with Session(self.engine) as s:
            batch = s.query(HistoryImport).one()
            self.assertEqual((batch.client_id, batch.layout, batch.lines, batch.date_from), ("T1", "grouped", 5, "2026-03-01"))
            self.assertEqual({i["current_code"] for i in batch.items}, {"200", "404"})

        again = self.post("import", {"section_map": smap}).json()                    # same file again: no double counting
        self.assertEqual(again["history_lines_total"], 5)

        listing = self.http.get("/history", params={"provider": "xero", "client_id": "T1"}).json()
        self.assertEqual((len(listing["imports"]), listing["history_lines_total"]), (2, 5))
        self.assertEqual(self.http.delete(f"/history/{listing['imports'][0]['id']}").status_code, 200)
        self.assertEqual(self.http.delete(f"/history/{listing['imports'][1]['id']}").status_code, 200)
        self.assertEqual(self.http.get("/history", params={"provider": "xero", "client_id": "T1"}).json()["history_lines_total"], 0)

    def test_account_normal_file_gets_a_hint_and_by_account_class_fixes_the_stored_sign(self):
        raw = ACCOUNT_NORMAL_CSV.encode()
        p = self.post("preview", {}, name="q.csv", content=raw).json()
        self.assertEqual(p["amount_sign"], "as_is")
        self.assertIn("By account type", p["sign_hint"])
        self.assertEqual(p["sample"][0]["amount"], 60.0)                              # as in the file

        m = '{"amount_sign":"by_account_class"}'
        fixed = self.post("preview", {"mapping": m}, name="q.csv", content=raw).json()
        self.assertIsNone(fixed["sign_hint"])
        self.assertEqual(fixed["sample"][0]["amount"], -60.0)

        smap = '{"Expenses:Repairs":"7","Sales":"200"}'                               # "Sales" resolved by the user, 200 is Revenue
        self.assertEqual(self.post("import", {"mapping": m, "section_map": smap}, name="q.csv", content=raw).status_code, 200)
        with Session(self.engine) as s:
            items = s.query(HistoryImport).one().items
            self.assertEqual(sorted(i["amount"] for i in items if i["current_code"] == "7")[:2], [-66.0, -65.0])
            self.assertEqual(next(i["amount"] for i in items if i["current_code"] == "200"), 300.0)
        self.assertEqual(self.post("preview", {"mapping": '{"amount_sign":"sideways"}'}, name="q.csv", content=raw).status_code, 400)

    def test_validation(self):
        smap = '{"Sales (200)":"200"}'
        self.assertEqual(self.post("import", {"section_map": '{"Sales (200)":"999"}'}).status_code, 400)       # not in the chart
        self.assertEqual(self.post("import", {"section_map": '{"Business Bank Account":null}'}).status_code, 400)  # nothing mapped
        self.assertEqual(self.post("import", {"section_map": "not json"}).status_code, 400)
        self.assertEqual(self.post("preview", {}, name="x.pdf", content=b"%PDF").status_code, 400)
        self.assertEqual(self.post("preview", {}, client_id="NOT-MINE").status_code, 400)
        self.assertEqual(self.post("import", {"section_map": smap, "mapping": '{"columns":{"bogus":1}}'}).status_code, 400)
        self.assertEqual(self.http.delete(f"/history/{'0' * 8}-0000-0000-0000-{'0' * 12}").status_code, 404)

    def test_qbo_style_csv_end_to_end_with_month_first_dates(self):
        b = self.post("preview", {}, name="q.csv", content=QBO_STYLE_CSV.encode()).json()
        self.assertEqual((b["dayfirst"], b["dayfirst_inferred"]), (False, False))     # 03/14/2026 can only be month-first
        sec = {s["name"]: s["code"] for s in b["sections"]}
        self.assertEqual(sec["Expenses:Repairs"], "7")
        self.assertIsNone(sec["Sales"])             # this chart has TWO accounts called "Sales" (200 and 9): never guess
        self.assertEqual(b["sample"][0]["amount"], -80.0)


if __name__ == "__main__":
    unittest.main()
