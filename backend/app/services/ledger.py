"""Shared helpers that turn the reviewed statement JSON into clean ledger rows
for the accounting destinations (Manager.io, Xero)."""
import hashlib
import datetime
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

_DATE_FORMATS = [
    "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d",
    "%d-%m-%Y", "%d %b %y", "%d-%b-%Y", "%d-%b-%y", "%b %d, %Y", "%m/%d/%Y",
]


@dataclass
class LedgerRow:
    date: datetime.date
    description: str
    reference: str
    amount: float          # always positive
    is_credit: bool        # True = money in (receipt), False = money out (payment)
    tx_hash: str


def parse_date(value: Any) -> Optional[datetime.date]:
    if not value:
        return None
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in ("-", "—"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if cleaned in ("", "-", "."):
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -abs(number) if negative else number


def _hash(date: datetime.date, description: str, reference: str, amount: float, is_credit: bool, account: str = "") -> str:
    raw = f"{account}|{date.isoformat()}|{description.strip().lower()}|{reference.strip().lower()}|{amount:.2f}|{'C' if is_credit else 'D'}"
    return hashlib.sha256(raw.encode()).hexdigest()


def build_rows(data: Dict[str, Any]) -> Tuple[List[LedgerRow], List[Dict[str, str]]]:
    """Return (importable rows, skipped rows with reasons).

    A row is skipped when it has no usable date or no debit/credit amount
    (e.g. a CLOSING BALANCE line). Opening balance rows that carry an amount
    are kept, so the destination account balance ends up equal to the statement.
    Identical rows inside one statement are kept, but get a numeric suffix in
    their hash so they are not mistaken for duplicates of each other.
    """
    rows: List[LedgerRow] = []
    skipped: List[Dict[str, str]] = []
    seen: Dict[str, int] = {}
    account = account_digits(data) or bank_account_label(data).lower()

    for tx in data.get("transactions", []) or []:
        desc = str(tx.get("description") or "").strip()
        ref = str(tx.get("reference") or "").strip()
        date = parse_date(tx.get("date"))
        debit = parse_amount(tx.get("debit"))
        credit = parse_amount(tx.get("credit"))

        if date is None:
            skipped.append({"description": desc, "reason": "Unreadable date"})
            continue

        if debit and abs(debit) > 0:
            amount, is_credit = abs(debit), False
        elif credit and abs(credit) > 0:
            amount, is_credit = abs(credit), True
        else:
            skipped.append({"description": desc, "reason": "No debit or credit amount"})
            continue

        base = _hash(date, desc, ref, amount, is_credit, account)
        n = seen.get(base, 0)
        seen[base] = n + 1
        tx_hash = base if n == 0 else hashlib.sha256(f"{base}#{n}".encode()).hexdigest()
        rows.append(LedgerRow(date, desc, ref, amount, is_credit, tx_hash))

    return rows, skipped


def bank_account_label(data: Dict[str, Any]) -> str:
    return str(data.get("bank_name") or "Imported Bank Account").strip() or "Imported Bank Account"


def account_digits(data: Dict[str, Any]) -> str:
    return re.sub(r"\D", "", str(data.get("account_number") or ""))
