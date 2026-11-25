"""Read a ledger export (QuickBooks "Transaction Detail by Account", Xero "Account Transactions", a General Ledger
detail, or any flat CSV with an Account column) into coded history lines.

Two layouts are handled:
  grouped  one section per account (a header row, its transactions, a "Total" row). Xero and QuickBooks reports.
  flat     every row carries its own account in an Account column.

Exports differ between versions, so NOTHING is assumed silently: detect_columns() proposes a column mapping and
suggest_sections() proposes which chart account each section/account name means, and both are shown to the user to
confirm or correct before anything is stored. Pure functions: no I/O beyond reading the uploaded bytes.

Sign convention (matches live ledger items): money OUT is negative, money IN positive. In a category account's
ledger a debit is money out and a credit is money in, so amount = credit - debit.
"""
import csv
import datetime
import hashlib
import io
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

MAX_ROWS = 600_000
HEADER_SCAN_ROWS = 60
SUPPORTED = (".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls")

# Header names (lower-case) that identify each field. First match per field wins; a column is used once.
FIELD_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "date": ("date", "transaction date", "txn date", "trans date", "posting date", "tran date"),
    "description": ("description", "memo/description", "memo", "narration", "details", "particulars", "line description", "narrative"),
    "contact": ("name", "payee", "contact", "supplier", "vendor", "customer", "supplier/customer", "payee/payer"),
    "reference": ("num", "number", "reference", "ref", "doc number", "doc no", "cheque no", "invoice number"),
    "debit": ("debit", "debits", "dr"),
    "credit": ("credit", "credits", "cr"),
    "amount": ("amount", "value", "net amount"),
    "account": ("account", "account name", "category"),
    "account_code": ("account code", "code", "acct num", "account number"),
    "tax": ("tax code", "tax rate", "tax rate name", "tax type", "gst code", "gst", "tax"),
    "type": ("type", "transaction type", "source"),
}
FIELD_ORDER = ("date", "debit", "credit", "amount", "account_code", "account", "contact", "reference", "description", "tax", "type")

_SKIP_TEXT = ("opening balance", "closing balance", "total", "balance")          # section-less text rows (totals)
_BALANCE_ROWS = frozenset(("opening balance", "closing balance", "balance", "balance brought forward", "balance carried forward"))
_BANKISH = re.compile(r"\b(bank|cheque|checking|savings|credit card|visa|mastercard|amex|petty cash|cash on hand|undeposited|clearing)\b", re.I)


class ImportFormatError(Exception):
    """The file cannot be read as a ledger export (message is shown to the user)."""


# ------------------------------------------------------------------ reading the file
def read_rows(filename: str, data: bytes) -> List[List[Any]]:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in SUPPORTED:
        raise ImportFormatError(f"Unsupported file type '{ext or filename}'. Use Excel (.xlsx/.xls) or CSV.")
    if ext in (".csv", ".tsv", ".txt"):
        text = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        sample = text[:4096]
        delim = "\t" if sample.count("\t") > sample.count(",") else (";" if sample.count(";") > sample.count(",") else ",")
        rows = [list(r) for r in csv.reader(io.StringIO(text), delimiter=delim)]
    elif ext == ".xls":
        import xlrd
        book = xlrd.open_workbook(file_contents=data)
        sheet = max(book.sheets(), key=lambda s: s.nrows)
        rows = []
        for r in range(sheet.nrows):
            row = []
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    row.append(xlrd.xldate_as_datetime(cell.value, book.datemode))
                else:
                    row.append(cell.value if cell.ctype != xlrd.XL_CELL_EMPTY else None)
            rows.append(row)
    else:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = max(wb.worksheets, key=lambda s: s.max_row or 0)
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append(list(row))
            if len(rows) > MAX_ROWS:
                break
    if len(rows) > MAX_ROWS:
        raise ImportFormatError(f"File has more than {MAX_ROWS:,} rows. Export a shorter period.")
    if not rows:
        raise ImportFormatError("The file is empty.")
    return rows


# ------------------------------------------------------------------ cell helpers
def _norm_header(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "").strip().lower()).rstrip(":")


def parse_date(v: Any, dayfirst: bool = True) -> Optional[datetime.date]:
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    text = str(v or "").strip()
    if not text:
        return None
    dm = ["%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y"]
    md = ["%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"]
    named = ["%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d %H:%M:%S"]
    for fmt in named + (dm + md if dayfirst else md + dm):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_number(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    text = str(v).strip()
    if not text or text in ("-", "—"):
        return None
    neg = (text.startswith("(") and text.endswith(")")) or text.endswith("-") or text.startswith("-")
    cleaned = re.sub(r"[^0-9.]", "", text.replace(",", ""))
    if cleaned in ("", "."):
        return None
    try:
        n = float(cleaned)
    except ValueError:
        return None
    return -n if neg else n


def extract_code(name: str) -> Optional[str]:
    """'Sales (200)' -> '200';  '6100 Repairs' -> '6100'."""
    m = re.search(r"\(\s*(\d{2,8})\s*\)\s*$", name) or re.match(r"^\s*(\d{2,8})\s*[-–:]?\s+\S", name)
    return m.group(1) if m else None


def normalise_account(name: str) -> str:
    s = re.sub(r"\(\s*\d{2,8}\s*\)\s*$", "", name.lower())
    s = re.sub(r"^\s*\d{2,8}\s*[-–:]?\s+", "", s)
    return re.sub(r"\s*:\s*", ":", re.sub(r"[^a-z0-9:&]+", " ", s)).strip()


# ------------------------------------------------------------------ detecting the layout
def guess_columns(headers: List[Any]) -> Dict[str, int]:
    normed = [_norm_header(h) for h in headers]
    cols: Dict[str, int] = {}
    used = set()
    for field in FIELD_ORDER:
        for ci, h in enumerate(normed):
            if ci not in used and h in FIELD_SYNONYMS[field]:
                cols[field] = ci
                used.add(ci)
                break
    return cols


def detect_columns(rows: List[List[Any]]) -> Optional[Tuple[int, Dict[str, int]]]:
    """(header row index, {field: column index}) for the first row that looks like a ledger header."""
    for idx in range(min(len(rows), HEADER_SCAN_ROWS)):
        cols = guess_columns(rows[idx])
        if "date" in cols and ("amount" in cols or "debit" in cols or "credit" in cols):
            return idx, cols
    return None


# ------------------------------------------------------------------ parsing
def _cell(row: List[Any], cols: Dict[str, int], field: str) -> Any:
    i = cols.get(field)
    return row[i] if i is not None and i < len(row) else None


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def parse_rows(rows: List[List[Any]], header_idx: int, cols: Dict[str, int], amount_sign: str = "as_is",
               dayfirst: bool = True) -> Dict[str, Any]:
    """Returns {"lines": [...], "sections": {account name: line count}, "skipped": {reason: n}, "grouped": bool}."""
    grouped = "account" not in cols
    lines: List[Dict[str, Any]] = []
    sections: Dict[str, int] = {}
    skipped: Counter = Counter()
    section: Optional[str] = None

    for row in rows[header_idx + 1:]:
        nonempty = [(i, c) for i, c in enumerate(row) if c is not None and str(c).strip() != ""]
        if not nonempty:
            continue
        d = parse_date(_cell(row, cols, "date"), dayfirst)
        if d is None:
            only = _text(nonempty[0][1]) if len(nonempty) == 1 else ""
            if grouped and only and not only.lower().startswith(_SKIP_TEXT):
                section = only                                       # a new account section header
            else:
                skipped["total/balance/other rows"] += 1
            continue

        desc = _text(_cell(row, cols, "description"))
        # Only the report's own balance rows are skipped. A real payee can start with "Total" (Total Tools).
        if desc.lower() in _BALANCE_ROWS or desc.lower().startswith("total for "):
            skipped["opening/closing balance rows"] += 1
            continue

        if "debit" in cols or "credit" in cols:
            amount = (parse_number(_cell(row, cols, "credit")) or 0.0) - (parse_number(_cell(row, cols, "debit")) or 0.0)
        else:
            amount = parse_number(_cell(row, cols, "amount"))
        if amount is None or abs(amount) < 0.005:
            skipped["no amount"] += 1
            continue
        if amount_sign == "flip":
            amount = -amount

        account = section if grouped else _text(_cell(row, cols, "account"))
        if not account:
            skipped["no account (row before any section)"] += 1
            continue
        sections[account] = sections.get(account, 0) + 1
        code_cell = _text(_cell(row, cols, "account_code"))
        lines.append({
            "date": d.isoformat(), "description": desc, "reference": _text(_cell(row, cols, "reference")),
            "contact": _text(_cell(row, cols, "contact")), "amount": round(amount, 2), "tax_label": _text(_cell(row, cols, "tax")),
            "account": account, "code_in_file": code_cell or extract_code(account),
        })
    return {"lines": lines, "sections": sections, "skipped": dict(skipped), "grouped": grouped}


# ------------------------------------------------------------------ account mapping
def suggest_sections(names: List[str], chart: List[Dict[str, Any]], codes_in_file: Optional[Dict[str, str]] = None) -> Dict[str, Dict[str, Any]]:
    """Propose, for each account name in the file, the chart account it means (or that it is a bank account to
    ignore). {name: {"code", "chart_name", "ignore", "how"}} ; code None = needs the user's choice."""
    by_code = {str(a["code"]): a for a in chart}
    by_full: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_last: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for a in chart:
        full = normalise_account(a["name"])
        by_full[full].append(a)
        by_last[full.split(":")[-1]].append(a)

    out: Dict[str, Dict[str, Any]] = {}
    for name in names:
        hit, how = None, ""
        code = (codes_in_file or {}).get(name) or extract_code(name)
        norm = normalise_account(name)
        # A name only counts when it points at exactly one chart account: two accounts can share a name, and
        # guessing between them would silently file history under the wrong one.
        if code and code in by_code:
            hit, how = by_code[code], "account code"
        elif len(by_full.get(norm, [])) == 1:
            hit, how = by_full[norm][0], "account name"
        elif len(by_last.get(norm.split(":")[-1], [])) == 1 and norm not in by_full:
            hit, how = by_last[norm.split(":")[-1]][0], "account name (last part)"
        if hit:
            out[name] = {"code": str(hit["code"]), "chart_name": hit["name"], "ignore": False, "how": how}
        elif _BANKISH.search(name):
            out[name] = {"code": None, "chart_name": None, "ignore": True, "how": "looks like a bank / card account"}
        else:
            out[name] = {"code": None, "chart_name": None, "ignore": False, "how": "no match: choose an account"}
    return out


# ------------------------------------------------------------------ sign by account type
# Account-based reports (QuickBooks) often print ONE Amount column signed per the account's normal side: an expense
# is positive, income is positive, a refund is negative. Money out is a debit to expense/asset accounts; money in is a
# credit to revenue/liability/equity accounts. With the account's class known, that column can be turned into a
# real direction.
_OUT_CLASSES = {"EXPENSE", "ASSET", "COSTOFGOODSSOLD", "COGS", "OTHEREXPENSE", "DIRECTCOSTS", "OVERHEADS"}


def class_sign(account_class: Optional[str]) -> Optional[int]:
    c = re.sub(r"[^A-Z]", "", (account_class or "").upper())
    if not c:
        return None
    return -1 if c in _OUT_CLASSES else 1


def apply_sign_by_class(lines: List[Dict[str, Any]], section_to_code: Dict[str, Optional[str]],
                        class_by_code: Dict[str, Optional[str]]) -> List[Dict[str, Any]]:
    """Copies of `lines` with amounts re-signed by the mapped account's class. Lines whose account is unmapped or whose
    class is unknown keep the file's sign (the user is told which)."""
    out = []
    for ln in lines:
        code = section_to_code.get(ln["account"])
        sign = class_sign(class_by_code.get(code)) if code else None
        out.append({**ln, "amount": round(sign * ln["amount"], 2)} if sign is not None else dict(ln))
    return out


def sign_hint(lines: List[Dict[str, Any]], section_to_code: Dict[str, Optional[str]],
              class_by_code: Dict[str, Optional[str]]) -> Optional[str]:
    """If expense-account lines are overwhelmingly positive, the file is almost certainly account-normal."""
    exp = [ln["amount"] for ln in lines if class_sign(class_by_code.get(section_to_code.get(ln["account"]))) == -1]
    if len(exp) >= 5 and sum(1 for a in exp if a > 0) / len(exp) >= 0.8:
        return ("Lines on expense accounts are mostly positive, so this file prints amounts the way the account sees them "
                "(expenses positive). Choose “By account type” so spending is stored as money out.")
    return None


# ------------------------------------------------------------------ building history items
def build_items(lines: List[Dict[str, Any]], section_map: Dict[str, Optional[str]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Slim history items for the mapped accounts. Ids are content hashes, so importing an overlapping export twice
    does not double count; truly identical rows inside one file get a running number to stay distinct."""
    items: List[Dict[str, Any]] = []
    stats: Counter = Counter()
    seen: Counter = Counter()
    for ln in lines:
        code = section_map.get(ln["account"])
        if not code:
            stats["ignored_lines"] += 1
            continue
        base = f"{ln['date']}|{ln['description'].lower()}|{ln['amount']:.2f}|{code}|{ln['contact'].lower()}"
        n = seen[base]
        seen[base] += 1
        iid = "imp:" + hashlib.sha1(f"{base}|{n}".encode()).hexdigest()[:20]
        items.append({
            "id": iid, "tx_id": iid, "line_index": 0, "date": ln["date"], "description": ln["description"],
            "contact": ln["contact"], "reference": ln["reference"], "type": "IMPORT",
            "direction": "spend" if ln["amount"] < 0 else "receive", "amount": ln["amount"],
            "current_code": str(code), "tax_label": ln["tax_label"], "origin": "import",
        })
        stats["imported_lines"] += 1
    return items, dict(stats)
