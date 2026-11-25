"""Xero export via the Accounting API (OAuth2 authorization-code flow).

Flow: authorize_url() -> user consents -> exchange_code() stores tokens + organisation ->
export_rows() finds/creates the bank account and suspense contact, then creates
SPEND (debit) / RECEIVE (credit) bank transactions.
"""
import base64
import datetime
import logging
import threading
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

import httpx

from app.config import settings
from app.services.ledger import LedgerRow, account_digits, bank_account_label

logger = logging.getLogger("app.services.xero")

# Xero ROTATES refresh tokens: using an already-used one fails and breaks the connection. With background syncs
# and webhooks running beside the UI, two threads can refresh at once, so refreshes are serialised and the loser
# adopts the token the winner just stored instead of reusing a dead one.
_REFRESH_LOCK = threading.RLock()

AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
API_URL = "https://api.xero.com/api.xro/2.0"
BATCH_SIZE = 50


class XeroError(Exception):
    pass


class XeroService:
    def __init__(self, values: Dict[str, Any], persist_tokens: Optional[Callable[[Dict[str, Any]], None]] = None,
                 reload_tokens: Optional[Callable[[], Dict[str, Any]]] = None):
        self._reload = reload_tokens   # returns the CURRENTLY stored values (fresh DB read), see _refresh()
        self.client_id = values.get("client_id")
        self.client_secret = values.get("client_secret")
        self.access_token = values.get("access_token")
        self.refresh_token = values.get("refresh_token")
        self.tenant_id = values.get("tenant_id")
        self.tenant_name = values.get("tenant_name")
        self.expires_at = values.get("expires_at")
        self.default_account_code = values.get("default_account_code")
        self._persist = persist_tokens

    # ---------------------------------------------------------------- OAuth
    def _require_client(self) -> None:
        if not self.client_id or not self.client_secret:
            raise XeroError("Xero Client ID / Client Secret are not configured.")

    def _basic(self) -> str:
        return "Basic " + base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()

    def authorize_url(self, state: str) -> str:
        self._require_client()
        query = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": settings.XERO_REDIRECT_URI,
            "scope": settings.XERO_SCOPES,
            "state": state,
        })
        return f"{AUTH_URL}?{query}"

    def _store_tokens(self, payload: Dict[str, Any]) -> None:
        self.access_token = payload["access_token"]
        self.refresh_token = payload.get("refresh_token", self.refresh_token)
        expires_in = int(payload.get("expires_in", 1800))
        self.expires_at = (datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in)).isoformat()
        if self._persist:
            self._persist({
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "tenant_id": self.tenant_id,
                "tenant_name": self.tenant_name,
            })

    def exchange_code(self, code: str) -> None:
        self._require_client()
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                TOKEN_URL,
                headers={"Authorization": self._basic(), "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": settings.XERO_REDIRECT_URI},
            )
            if resp.status_code != 200:
                raise XeroError(f"Xero token exchange failed: {resp.text}")
            payload = resp.json()
            self.access_token = payload["access_token"]
            conns = client.get(CONNECTIONS_URL, headers={"Authorization": f"Bearer {self.access_token}"})
            if conns.status_code != 200:
                raise XeroError(f"Could not list Xero organisations: {conns.text}")
            tenants = conns.json()
            if not tenants:
                raise XeroError("No Xero organisation was authorised. Tick an organisation on the consent screen.")
            self.tenant_id = tenants[0]["tenantId"]
            self.tenant_name = tenants[0].get("tenantName")
        self._store_tokens(payload)

    def _token_fresh(self) -> bool:
        try:
            expires = datetime.datetime.fromisoformat(self.expires_at) if self.expires_at else None
        except ValueError:
            expires = None
        return bool(self.access_token and expires and expires - datetime.datetime.utcnow() >= datetime.timedelta(seconds=60))

    def _refresh(self) -> None:
        self._require_client()
        if not self.refresh_token:
            raise XeroError("Xero is not connected. Click Connect Xero in Settings > Deployment.")
        with _REFRESH_LOCK:
            if self._reload:
                latest = self._reload() or {}
                if latest.get("refresh_token") and latest["refresh_token"] != self.refresh_token:
                    # Someone else refreshed while we waited: our refresh token is spent, theirs is current.
                    self.refresh_token = latest["refresh_token"]
                    self.access_token = latest.get("access_token")
                    self.expires_at = latest.get("expires_at")
                    if self._token_fresh():
                        return
            with httpx.Client(timeout=30) as client:
                resp = client.post(
                    TOKEN_URL,
                    headers={"Authorization": self._basic(), "Content-Type": "application/x-www-form-urlencoded"},
                    data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
                )
            if resp.status_code != 200:
                raise XeroError(f"Xero token refresh failed (reconnect Xero): {resp.text}")
            self._store_tokens(resp.json())   # persists the rotated pair before the lock is released

    def _ensure_token(self) -> None:
        if not self.access_token:
            self._refresh()
            return
        if not self._token_fresh():
            self._refresh()

    # ---------------------------------------------------------------- HTTP
    def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        if not self.tenant_id:
            raise XeroError("Xero is not connected. Click Connect Xero in Settings > Deployment.")
        self._ensure_token()
        for attempt in (1, 2):
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "xero-tenant-id": self.tenant_id,
                "Accept": "application/json",
            }
            with httpx.Client(timeout=60) as client:
                resp = client.request(method, f"{API_URL}{path}", headers=headers, **kwargs)
            if resp.status_code == 401 and attempt == 1:
                self._refresh()
                continue
            if resp.status_code >= 400 and resp.status_code != 400:
                raise XeroError(f"Xero API {resp.status_code}: {resp.text[:400]}")
            return {"status": resp.status_code, "json": resp.json() if resp.content else {}}
        raise XeroError("Xero request failed after token refresh.")

    def list_tenants(self) -> List[Dict[str, Any]]:
        """Every Xero organisation this login has authorised (the 'clients' an accountant can switch between)."""
        self._ensure_token()
        for attempt in (1, 2):
            with httpx.Client(timeout=30) as client:
                resp = client.get(CONNECTIONS_URL, headers={"Authorization": f"Bearer {self.access_token}",
                                                            "Accept": "application/json"})
            if resp.status_code == 401 and attempt == 1:
                self._refresh()
                continue
            if resp.status_code != 200:
                raise XeroError(f"Could not list Xero organisations: HTTP {resp.status_code} {resp.text[:200]}")
            return resp.json()
        raise XeroError("Could not list Xero organisations after token refresh.")

    # ---------------------------------------------------------------- test
    def test(self) -> str:
        data = self._request("GET", "/Organisation")
        orgs = data["json"].get("Organisations", [])
        name = orgs[0].get("Name") if orgs else self.tenant_name
        return f"Connected to Xero organisation: {name}"

    # ---------------------------------------------------------------- lookups
    def _find_or_create_bank_account(self, ledger: Dict[str, Any]) -> Dict[str, str]:
        label = bank_account_label(ledger)
        digits = account_digits(ledger)
        accounts = self._request("GET", "/Accounts", params={"where": 'Type=="BANK"'})["json"].get("Accounts", [])

        for acc in accounts:
            if acc.get("Status") == "ARCHIVED":
                continue
            acc_digits = "".join(ch for ch in str(acc.get("BankAccountNumber") or "") if ch.isdigit())
            if (digits and acc_digits and acc_digits == digits) or acc.get("Name", "").strip().lower() == label.lower():
                return {"id": acc["AccountID"], "name": acc["Name"]}

        number = digits if len(digits) >= 5 else "0000000000"
        created = self._request("PUT", "/Accounts", json={
            "Name": label, "Type": "BANK", "BankAccountNumber": number,
        })
        accs = created["json"].get("Accounts") or []
        if created["status"] >= 400 or not accs or not accs[0].get("AccountID"):
            raise XeroError(f"Could not create Xero bank account '{label}': {created['json']}")
        return {"id": accs[0]["AccountID"], "name": accs[0]["Name"]}

    def _find_or_create_contact(self, name: str) -> str:
        safe = name.replace('"', "")
        found = self._request("GET", "/Contacts", params={"where": f'Name=="{safe}"'})["json"].get("Contacts", [])
        if found:
            return found[0]["ContactID"]
        created = self._request("PUT", "/Contacts", json={"Contacts": [{"Name": safe}]})["json"].get("Contacts", [])
        if not created or not created[0].get("ContactID"):
            raise XeroError(f"Could not create Xero contact '{safe}'.")
        return created[0]["ContactID"]

    def _line_account_code(self) -> str:
        if self.default_account_code:
            return self.default_account_code
        accounts = self._request("GET", "/Accounts")["json"].get("Accounts", [])
        active = [a for a in accounts if a.get("Status") == "ACTIVE" and a.get("Code")]
        for a in active:
            if a.get("Name", "").strip().lower() == "suspense":
                return a["Code"]
        for a in active:
            if a.get("Class") == "EXPENSE":
                return a["Code"]
        if active:
            return active[0]["Code"]
        raise XeroError("No usable Xero account code found. Set XERO_DEFAULT_ACCOUNT_CODE.")

    # ---------------------------------------------------------------- ledger coding
    def list_accounts(self) -> List[Dict[str, Any]]:
        """Chart of accounts usable on transaction lines (bank accounts and archived ones excluded)."""
        resp = self._request("GET", "/Accounts")
        if resp["status"] == 400:
            raise XeroError(f"Could not read the chart of accounts: {str(resp['json'])[:300]}")
        return [
            {"code": a["Code"], "name": a.get("Name", ""), "type": a.get("Type"), "class": a.get("Class")}
            for a in resp["json"].get("Accounts", [])
            if a.get("Code") and a.get("Status") != "ARCHIVED" and a.get("Type") != "BANK"
        ]

    def list_bank_transactions(self, date_from: datetime.date, date_to: datetime.date,
                               progress: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
        """All AUTHORISED bank transactions dated within [date_from, date_to] (Xero pages by 100)."""
        where = (
            f'Status=="AUTHORISED"&&Date>=DateTime({date_from.year},{date_from.month},{date_from.day})'
            f'&&Date<=DateTime({date_to.year},{date_to.month},{date_to.day})'
        )
        out: List[Dict[str, Any]] = []
        page = 1
        while True:
            resp = self._request("GET", "/BankTransactions", params={"where": where, "page": page, "order": "Date"})
            if resp["status"] == 400:
                raise XeroError(f"Could not read bank transactions: {str(resp['json'])[:300]}")
            batch = resp["json"].get("BankTransactions", [])
            out.extend(batch)
            if progress:
                progress(f"Downloaded {len(out)} bank transactions from Xero...")
            if len(batch) < 100:
                return out
            page += 1

    def list_suppliers(self) -> List[Dict[str, Any]]:
        """Supplier contacts with their tax number (in Australia that is the ABN), 100 per page."""
        out: List[Dict[str, Any]] = []
        page = 1
        while True:
            resp = self._request("GET", "/Contacts", params={"where": "IsSupplier==true", "page": page})
            if resp["status"] == 400:
                raise XeroError(f"Could not read suppliers: {str(resp['json'])[:300]}")
            batch = resp["json"].get("Contacts", [])
            out.extend({"id": c["ContactID"], "name": c.get("Name", ""), "abn": c.get("TaxNumber")}
                       for c in batch if c.get("ContactStatus") != "ARCHIVED")
            if len(batch) < 100:
                return out
            page += 1

    def update_bank_transactions(self, payloads: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
        """POST changed transactions. Returns {BankTransactionID: None if ok else error text}."""
        results: Dict[str, Optional[str]] = {}
        for start in range(0, len(payloads), 25):
            chunk = payloads[start:start + 25]
            resp = self._request("POST", "/BankTransactions", params={"summarizeErrors": "false"},
                                 json={"BankTransactions": chunk})
            returned = resp["json"].get("BankTransactions", [])
            if not returned:
                msg = str(resp["json"])[:300]
                for p in chunk:
                    results[p["BankTransactionID"]] = msg or f"Xero returned HTTP {resp['status']}"
                continue
            for sent, got in zip(chunk, returned):
                if got.get("HasValidationErrors") or got.get("StatusAttributeString") == "ERROR":
                    errs = "; ".join(e.get("Message", "") for e in got.get("ValidationErrors", []))
                    results[sent["BankTransactionID"]] = errs or "Rejected by Xero"
                else:
                    results[sent["BankTransactionID"]] = None
        return results

    # ---------------------------------------------------------------- export
    def export_rows(self, ledger: Dict[str, Any], rows: List[LedgerRow]) -> Dict[str, Any]:
        bank = self._find_or_create_bank_account(ledger)
        contact_id = self._find_or_create_contact(settings.XERO_SUSPENSE_CONTACT_NAME)
        code = self._line_account_code()

        imported_hashes: List[str] = []
        errors: List[str] = []

        for start in range(0, len(rows), BATCH_SIZE):
            chunk = rows[start:start + BATCH_SIZE]
            payload = {"BankTransactions": [{
                "Type": "RECEIVE" if r.is_credit else "SPEND",
                "Contact": {"ContactID": contact_id},
                "Date": r.date.isoformat(),
                "Reference": r.reference[:255] if r.reference else r.description[:255],
                "BankAccount": {"AccountID": bank["id"]},
                "LineAmountTypes": "NoTax",
                "LineItems": [{
                    "Description": (r.description or "Bank transaction")[:4000],
                    "Quantity": 1,
                    "UnitAmount": round(r.amount, 2),
                    "AccountCode": code,
                    "TaxType": "NONE",
                }],
            } for r in chunk]}

            resp = self._request("PUT", "/BankTransactions", params={"summarizeErrors": "false"}, json=payload)
            results = resp["json"].get("BankTransactions", [])
            if not results:
                errors.append(f"Batch starting at row {start + 1}: {str(resp['json'])[:300]}")
                continue
            for row, result in zip(chunk, results):
                if result.get("HasValidationErrors") or result.get("StatusAttributeString") == "ERROR":
                    msgs = "; ".join(e.get("Message", "") for e in result.get("ValidationErrors", []))
                    errors.append(f"{row.date} {row.description[:40]}: {msgs or 'rejected by Xero'}")
                else:
                    imported_hashes.append(row.tx_hash)

        return {
            "organization": self.tenant_name,
            "bank_account": bank["name"],
            "imported_hashes": imported_hashes,
            "failed": len(rows) - len(imported_hashes),
            "errors": errors[:20],
            "url": f"https://go.xero.com/Bank/BankTransactions.aspx?accountID={bank['id']}",
        }
