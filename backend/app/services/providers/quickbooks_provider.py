"""QuickBooks Online ledger coding.

QuickBooks has no single "bank transaction" entity, so the ledger is built from the two that carry a category
per line: Purchase (expense / cheque / credit-card spend, money out) and Deposit (money in). Item-based
purchase lines, deposits that bundle customer payments, transfers and journal entries are shown as locked so
the totals still tie out, but are never changed.

A QuickBooks login is tied to ONE company (realm). Several clients means connecting each company; today the
selector lists the connected company. Account "code" here is the QuickBooks account Id (stable and unique;
AcctNum is optional) and the name carries the account number when there is one.

NOT YET VERIFIED against a live QuickBooks company: read and update shapes follow Intuit's documented schema
and are covered by mocked-API tests only.
"""
import copy
import datetime
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session

from app.database.models import User
from app.services import connection_store as store
from app.services.providers.base import LedgerProvider, Progress, ProviderError, stored_values
from app.services.quickbooks_service import QuickBooksError, QuickBooksService

# (entity, REST path, line detail key, account ref key on the header that a sparse update must repeat)
_PURCHASE = ("Purchase", "/purchase", "AccountBasedExpenseLineDetail", ("PaymentType", "AccountRef"))
_DEPOSIT = ("Deposit", "/deposit", "DepositLineDetail", ("DepositToAccountRef",))
_NOT_CATEGORIES = {"Bank", "Credit Card"}  # payment sources, not categories


class QuickBooksLedgerProvider(LedgerProvider):
    key = "quickbooks"
    label = "QuickBooks Online"
    verified = False
    note = "Beta: not yet verified against a live QuickBooks company. Check the first apply on a small range."

    def __init__(self, db: Session, user: User, service: Optional[QuickBooksService] = None):
        self._db, self._user = db, user
        self._injected = service

    def _service(self) -> QuickBooksService:
        if self._injected:
            return self._injected

        def persist(tokens: Dict[str, Any]) -> None:
            store.save(self._db, self._user, "quickbooks",
                       public={"realm_id": tokens.get("realm_id"), "company_name": tokens.get("company_name"),
                               "environment": tokens.get("environment"), "expires_at": tokens.get("expires_at")},
                       secret={"access_token": tokens.get("access_token"), "refresh_token": tokens.get("refresh_token")})

        return QuickBooksService(store.resolve(self._db, self._user, "quickbooks"), persist_tokens=persist,
                                 reload_tokens=lambda: stored_values(self._user.id, "quickbooks"))

    def _wrap(self, fn):
        try:
            return fn()
        except QuickBooksError as exc:
            raise ProviderError(str(exc))

    # ---------------------------------------------------------------- clients / accounts
    def default_client_id(self) -> Optional[str]:
        return store.resolve(self._db, self._user, "quickbooks").get("realm_id")

    def list_clients(self) -> List[Dict[str, str]]:
        v = store.resolve(self._db, self._user, "quickbooks")
        if not v.get("realm_id"):
            return []
        return [{"id": str(v["realm_id"]), "name": v.get("company_name") or f"Company {v['realm_id']}"}]

    def _accounts_raw(self, client_id: Optional[str]) -> List[Dict[str, Any]]:
        self._check_client(client_id)
        return self._wrap(lambda: self._service().query_all("Account", "where Active = true"))

    def _check_client(self, client_id: Optional[str]) -> None:
        current = self.default_client_id() if not self._injected else self._injected.realm_id
        if client_id and current and str(client_id) != str(current):
            raise ProviderError("That QuickBooks company is not the one connected. Reconnect QuickBooks to switch company.")

    def list_accounts(self, client_id: Optional[str]) -> List[Dict[str, Any]]:
        out = []
        for a in self._accounts_raw(client_id):
            if a.get("AccountType") in _NOT_CATEGORIES:
                continue
            num = a.get("AcctNum")
            name = a.get("FullyQualifiedName") or a.get("Name", "")
            out.append({"code": str(a["Id"]), "name": f"{num} {name}".strip() if num else name,
                        "type": a.get("AccountType"), "class": a.get("Classification")})
        return out

    def list_suppliers(self, client_id: Optional[str]) -> List[Dict[str, Any]]:
        """Vendors. The ABN is read from `BusinessNumber` (Australian companies); unverified against a live AU company."""
        self._check_client(client_id)
        vendors = self._wrap(lambda: self._service().query_all("Vendor", "where Active = true"))
        return [{"id": str(v["Id"]), "name": v.get("DisplayName") or v.get("CompanyName") or "",
                 "abn": v.get("BusinessNumber")} for v in vendors]

    # ---------------------------------------------------------------- ledger
    def fetch_ledger(self, client_id: Optional[str], date_from: datetime.date, date_to: datetime.date,
                     progress: Progress = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        self._check_client(client_id)
        svc = self._service()
        where = f"where TxnDate >= '{date_from.isoformat()}' and TxnDate <= '{date_to.isoformat()}'"
        if progress:
            progress("Downloading purchases from QuickBooks...")
        purchases = self._wrap(lambda: svc.query_all("Purchase", where))
        if progress:
            progress(f"Downloaded {len(purchases)} purchases; downloading deposits...")
        deposits = self._wrap(lambda: svc.query_all("Deposit", where))
        return build_items(purchases, deposits)

    def apply_changes(self, client_id: Optional[str], changes: List[Dict[str, Any]],
                      cache: Dict[str, Any]) -> Dict[str, Optional[str]]:
        self._check_client(client_id)
        svc = self._service()
        by_tx: Dict[str, Dict[int, str]] = defaultdict(dict)
        for c in changes:
            by_tx[c["tx_id"]][c["line_index"]] = c["proposed_code"]

        results: Dict[str, Optional[str]] = {}
        for tx_id, codes in by_tx.items():
            tx = cache[tx_id]
            lines = copy.deepcopy(tx["Lines"])
            for idx, code in codes.items():
                lines[idx][tx["DetailKey"]]["AccountRef"] = {"value": str(code)}
            # Sparse update, but QuickBooks still wants the header fields that define the transaction.
            body = {"Id": tx["Id"], "SyncToken": tx["SyncToken"], "sparse": True, **tx["Required"], "Line": lines}
            try:
                results[tx_id] = self._wrap(lambda: svc.update_entity(tx["Path"], body))
            except ProviderError as exc:
                results[tx_id] = str(exc)
        return results


def build_items(purchases: List[Dict[str, Any]], deposits: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Pure mapping from QuickBooks Purchase/Deposit JSON to ledger items + the write-back cache."""
    items: List[Dict[str, Any]] = []
    cache: Dict[str, Any] = {}

    def add(entity_spec, tx, sign: float, contact_of, bank_name: str, kind: str, recodable_of):
        entity, path, detail_key, required_keys = entity_spec
        tx_id = f"{entity}:{tx['Id']}"
        lines = tx.get("Line", []) or []
        cache[tx_id] = {
            "Entity": entity, "Path": path, "DetailKey": detail_key, "Id": tx["Id"], "SyncToken": tx.get("SyncToken", "0"),
            "Date": tx.get("TxnDate", ""), "Lines": lines,
            "Required": {k: tx[k] for k in required_keys if k in tx},
        }
        for idx, ln in enumerate(lines):
            if ln.get("DetailType") == "SubTotalLineDetail":
                continue  # QuickBooks' own subtotal row, not a ledger line
            detail = ln.get(detail_key) or {}
            current = (detail.get("AccountRef") or {}).get("value")
            recodable, why = recodable_of(ln, detail)
            items.append({
                "id": f"{tx_id}:{idx}", "tx_id": tx_id, "line_index": idx,
                "date": tx.get("TxnDate", ""),
                "description": (ln.get("Description") or tx.get("PrivateNote") or tx.get("DocNumber") or "").strip(),
                "contact": contact_of(tx, detail), "reference": tx.get("DocNumber") or "",
                "type": kind, "direction": "spend" if sign < 0 else "receive",
                "amount": round(sign * float(ln.get("Amount") or 0.0), 2),
                "current_code": str(current) if current else None,
                "tax_code": (detail.get("TaxCodeRef") or {}).get("value"),
                "proposed_code": None if recodable else (str(current) if current else None),
                "source": None if recodable else "locked", "confidence": None,
                "reason": "" if recodable else why,
                "status": "unassigned" if recodable else "locked",
                "bank_account": bank_name,
            })

    def purchase_ok(ln, d):
        if ln.get("DetailType") != "AccountBasedExpenseLineDetail" or not d.get("AccountRef"):
            return False, "Item/product line - not recodable here"
        return True, ""

    def deposit_ok(ln, d):
        if ln.get("LinkedTxn"):
            return False, "Deposit of a customer payment - not recodable here"
        if ln.get("DetailType") != "DepositLineDetail" or not d.get("AccountRef"):
            return False, "Not a plain deposit line"
        return True, ""

    for p in purchases:
        sign = 1.0 if p.get("Credit") else -1.0  # a vendor credit paid back to the bank is money in
        add(_PURCHASE, p, sign, lambda tx, d: (tx.get("EntityRef") or {}).get("name") or "",
            (p.get("AccountRef") or {}).get("name") or "", p.get("PaymentType") or "Purchase", purchase_ok)
    for d_ in deposits:
        add(_DEPOSIT, d_, 1.0, lambda tx, d: ((d.get("Entity") or {}).get("name")) or "",
            (d_.get("DepositToAccountRef") or {}).get("name") or "", "Deposit", deposit_ok)

    items.sort(key=lambda i: (i["date"], i["id"]))
    return items, cache
