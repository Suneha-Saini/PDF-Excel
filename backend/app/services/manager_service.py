"""Manager.io export (Local server and Cloud share this code; only URL/credentials differ).

!! VERIFY AGAINST YOUR INSTANCE !!
The Manager.io API v2 write schema could not be confirmed from public documentation
while this was written. Everything that depends on that schema is confined to the
constants/functions in the "API MAPPING" block below, so any mismatch is a one-place fix.
"Test Connection" in Settings shows the server's raw response to help with that.

Auth:  username + secret  -> HTTP Basic;   secret only -> `X-API-KEY` header.
"""
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.services.ledger import LedgerRow

logger = logging.getLogger("app.services.manager")

# ======================= API MAPPING (edit here if Manager rejects requests) =======================
API_ROOT = "/api2"
EP_TEST = "/bank-and-cash-accounts"        # GET: connectivity / auth probe
EP_RECEIPT = "/receipt-form"               # POST: money in
EP_PAYMENT = "/payment-form"               # POST: money out
EP_ACCOUNTS = "/chart-of-accounts"         # GET: used only to find a "Suspense" account


def receipt_body(row: LedgerRow, bank_id: str, other_account_id: str) -> Dict[str, Any]:
    return {
        "Date": f"{row.date.isoformat()}T00:00:00",
        "Reference": row.reference or None,
        "Description": row.description,
        "ReceivedIn": bank_id,
        "Lines": [{
            "Account": other_account_id,
            "LineDescription": row.description,
            "ReceivedAmount": round(row.amount, 2),
        }],
    }


def payment_body(row: LedgerRow, bank_id: str, other_account_id: str) -> Dict[str, Any]:
    return {
        "Date": f"{row.date.isoformat()}T00:00:00",
        "Reference": row.reference or None,
        "Description": row.description,
        "PaidFrom": bank_id,
        "Lines": [{
            "Account": other_account_id,
            "LineDescription": row.description,
            "PaymentAmount": round(row.amount, 2),
        }],
    }
# ===================================================================================================

MAX_CONSECUTIVE_FAILURES = 3


class ManagerError(Exception):
    pass


class ManagerService:
    def __init__(self, values: Dict[str, Any]):
        self.url = (values.get("url") or "").rstrip("/")
        self.username = values.get("username")
        self.secret = values.get("secret")
        self.business_id = values.get("business_id")
        self.bank_account_id = values.get("bank_account_id")
        self.suspense_account_id = values.get("suspense_account_id")

    def _auth(self) -> Dict[str, Any]:
        if not self.url:
            raise ManagerError("Manager.io URL is not configured.")
        if not self.secret:
            raise ManagerError("Manager.io API key / password is not configured.")
        if self.username:
            return {"auth": (self.username, self.secret), "headers": {"Accept": "application/json"}}
        return {"auth": None, "headers": {"X-API-KEY": self.secret, "Accept": "application/json"}}

    def _endpoint(self, path: str) -> str:
        return f"{self.url}{API_ROOT}{path}"

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        auth = self._auth()
        with httpx.Client(timeout=30, verify=True) as client:
            return client.request(
                method, self._endpoint(path), auth=auth["auth"], headers=auth["headers"], **kwargs
            )

    def test(self) -> str:
        resp = self._send("GET", EP_TEST)
        if resp.status_code in (401, 403):
            raise ManagerError(f"Manager.io rejected the credentials (HTTP {resp.status_code}).")
        if resp.status_code >= 400:
            raise ManagerError(f"Manager.io responded HTTP {resp.status_code} for {EP_TEST}: {resp.text[:200]}")
        return f"Connected to Manager.io at {self.url} (HTTP {resp.status_code})."

    def _suspense_id(self) -> str:
        if self.suspense_account_id:
            return self.suspense_account_id
        resp = self._send("GET", EP_ACCOUNTS)
        if resp.status_code == 200:
            try:
                payload = resp.json()
                items = payload if isinstance(payload, list) else next(
                    (v for v in payload.values() if isinstance(v, list)), []
                )
                for item in items:
                    name = str(item.get("name") or item.get("Name") or "").lower()
                    key = item.get("key") or item.get("Key") or item.get("id")
                    if name == "suspense" and key:
                        return str(key)
            except Exception:
                pass
        raise ManagerError("Set a 'Default Suspense/Category Account ID' in Manager.io settings.")

    def export_rows(self, rows: List[LedgerRow], bank_label: str) -> Dict[str, Any]:
        if not self.bank_account_id:
            raise ManagerError("Set the 'Target Bank Account ID' in Manager.io settings.")
        suspense = self._suspense_id()

        imported_hashes: List[str] = []
        errors: List[str] = []
        consecutive = 0

        for row in rows:
            body = (receipt_body if row.is_credit else payment_body)(row, self.bank_account_id, suspense)
            path = EP_RECEIPT if row.is_credit else EP_PAYMENT
            try:
                resp = self._send("POST", path, json=body)
                ok = resp.status_code < 300
                detail = "" if ok else f"HTTP {resp.status_code}: {resp.text[:200]}"
            except httpx.HTTPError as exc:
                ok, detail = False, str(exc)

            if ok:
                imported_hashes.append(row.tx_hash)
                consecutive = 0
            else:
                errors.append(f"{row.date} {row.description[:40]}: {detail}")
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    errors.append("Stopped early after repeated failures - check the Manager.io settings/API mapping.")
                    break

        return {
            "business": self.business_id or self.url,
            "bank_account": bank_label,
            "imported_hashes": imported_hashes,
            "failed": len(rows) - len(imported_hashes),
            "errors": errors[:20],
            "url": f"{self.url}/businesses",
        }
