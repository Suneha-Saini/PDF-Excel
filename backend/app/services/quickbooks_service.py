"""QuickBooks Online export via the Intuit Accounting API (OAuth2 authorization-code flow).

Flow: authorize_url() -> user consents -> exchange_code() stores tokens + realmId (company) ->
export_rows() finds/creates the bank account and suspense/expense account, then creates
Purchases (money out) / Deposits (money in) via QuickBooks Batch API.
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

logger = logging.getLogger("app.services.quickbooks")

# QuickBooks also rotates refresh tokens. See the same lock in xero_service for why refreshes are serialised.
_REFRESH_LOCK = threading.RLock()

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SANDBOX_API_URL = "https://sandbox-quickbooks.api.intuit.com/v3/company"
PRODUCTION_API_URL = "https://quickbooks.api.intuit.com/v3/company"
BATCH_SIZE = 30  # QuickBooks Online Batch API supports up to 30 operations


class QuickBooksError(Exception):
    pass


class QuickBooksService:
    def __init__(self, values: Dict[str, Any], persist_tokens: Optional[Callable[[Dict[str, Any]], None]] = None,
                 reload_tokens: Optional[Callable[[], Dict[str, Any]]] = None):
        self._reload = reload_tokens   # returns the CURRENTLY stored values (fresh DB read)
        self.client_id = values.get("client_id")
        self.client_secret = values.get("client_secret")
        self.access_token = values.get("access_token")
        self.refresh_token = values.get("refresh_token")
        self.realm_id = values.get("realm_id")
        self.company_name = values.get("company_name")
        self.environment = (values.get("environment") or settings.QUICKBOOKS_ENVIRONMENT or "sandbox").strip().lower()
        self.expires_at = values.get("expires_at")
        self.default_account_id = values.get("default_account_id")
        self._persist = persist_tokens

    @property
    def api_base_url(self) -> str:
        return PRODUCTION_API_URL if self.environment == "production" else SANDBOX_API_URL

    # ---------------------------------------------------------------- OAuth
    def _require_client(self) -> None:
        if not self.client_id or not self.client_secret:
            raise QuickBooksError("QuickBooks Client ID / Client Secret are not configured.")

    def _basic(self) -> str:
        return "Basic " + base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()

    def authorize_url(self, state: str) -> str:
        self._require_client()
        query = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": settings.QUICKBOOKS_REDIRECT_URI,
            "scope": settings.QUICKBOOKS_SCOPES,
            "state": state,
        })
        return f"{AUTH_URL}?{query}"

    def _store_tokens(self, payload: Dict[str, Any]) -> None:
        self.access_token = payload["access_token"]
        self.refresh_token = payload.get("refresh_token", self.refresh_token)
        expires_in = int(payload.get("expires_in", 3600))
        self.expires_at = (datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in)).isoformat()
        if self._persist:
            self._persist({
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "realm_id": self.realm_id,
                "company_name": self.company_name,
                "environment": self.environment,
            })

    def exchange_code(self, code: str, realm_id: Optional[str] = None) -> None:
        self._require_client()
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                TOKEN_URL,
                headers={"Authorization": self._basic(), "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": settings.QUICKBOOKS_REDIRECT_URI},
            )
            if resp.status_code != 200:
                raise QuickBooksError(f"QuickBooks token exchange failed: {resp.text}")
            payload = resp.json()

        if realm_id:
            self.realm_id = realm_id

        self._store_tokens(payload)

        # Lookup company details if realm_id is available
        if self.realm_id:
            try:
                info = self._request("GET", f"/companyinfo/{self.realm_id}")
                company = info.get("json", {}).get("CompanyInfo", {})
                self.company_name = company.get("CompanyName") or company.get("LegalName") or f"Company {self.realm_id}"
                if self._persist:
                    self._persist({
                        "access_token": self.access_token,
                        "refresh_token": self.refresh_token,
                        "expires_at": self.expires_at,
                        "realm_id": self.realm_id,
                        "company_name": self.company_name,
                        "environment": self.environment,
                    })
            except Exception as e:
                logger.warning(f"Could not fetch company info: {e}")
                if not self.company_name:
                    self.company_name = f"Company {self.realm_id}"

    def _token_fresh(self) -> bool:
        try:
            expires = datetime.datetime.fromisoformat(self.expires_at) if self.expires_at else None
        except ValueError:
            expires = None
        return bool(self.access_token and expires and expires - datetime.datetime.utcnow() >= datetime.timedelta(seconds=60))

    def _refresh(self) -> None:
        self._require_client()
        if not self.refresh_token:
            raise QuickBooksError("QuickBooks is not connected. Click Connect QuickBooks in Settings > Deployment.")
        with _REFRESH_LOCK:
            if self._reload:
                latest = self._reload() or {}
                if latest.get("refresh_token") and latest["refresh_token"] != self.refresh_token:
                    self.refresh_token = latest["refresh_token"]
                    self.access_token = latest.get("access_token")
                    self.expires_at = latest.get("expires_at")
                    if self._token_fresh():
                        return
            with httpx.Client(timeout=30) as client:
                resp = client.post(
                    TOKEN_URL,
                    headers={"Authorization": self._basic(), "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                    data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
                )
            if resp.status_code != 200:
                raise QuickBooksError(f"QuickBooks token refresh failed (reconnect QuickBooks): {resp.text}")
            self._store_tokens(resp.json())

    def _ensure_token(self) -> None:
        if not self.access_token:
            self._refresh()
            return
        if not self._token_fresh():
            self._refresh()

    # ---------------------------------------------------------------- HTTP
    def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        if not self.realm_id:
            raise QuickBooksError("QuickBooks is not connected. Click Connect QuickBooks in Settings > Deployment.")
        self._ensure_token()
        url = f"{self.api_base_url}/{self.realm_id}{path}"
        for attempt in (1, 2):
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            with httpx.Client(timeout=60) as client:
                resp = client.request(method, url, headers=headers, **kwargs)
            if resp.status_code == 401 and attempt == 1:
                self._refresh()
                continue
            if resp.status_code >= 400 and resp.status_code != 400:
                raise QuickBooksError(f"QuickBooks API {resp.status_code}: {resp.text[:400]}")
            return {"status": resp.status_code, "json": resp.json() if resp.content else {}}
        raise QuickBooksError("QuickBooks request failed after token refresh.")

    # ---------------------------------------------------------------- test
    def test(self) -> str:
        data = self._request("GET", f"/companyinfo/{self.realm_id}")
        company = data["json"].get("CompanyInfo", {})
        name = company.get("CompanyName") or self.company_name or f"Company {self.realm_id}"
        return f"Connected to QuickBooks company: {name} (Environment: {self.environment})"

    # ---------------------------------------------------------------- lookups
    def _find_or_create_bank_account(self, ledger: Dict[str, Any]) -> Dict[str, str]:
        label = bank_account_label(ledger)
        digits = account_digits(ledger)
        query = "select * from Account where AccountType = 'Bank' maxresults 200"
        res = self._request("GET", f"/query?query={urllib.parse.quote(query)}")
        accounts = res["json"].get("QueryResponse", {}).get("Account", [])

        # Match by account digits or name
        for acc in accounts:
            if not acc.get("Active", True):
                continue
            acc_num = "".join(ch for ch in str(acc.get("AcctNum") or "") if ch.isdigit())
            if digits and acc_num and acc_num == digits:
                return {"id": str(acc["Id"]), "name": acc.get("Name", label)}
            if acc.get("Name", "").strip().lower() == label.lower():
                return {"id": str(acc["Id"]), "name": acc.get("Name", label)}

        # Create bank account if none matched
        num = digits if digits and len(digits) >= 4 else "0000"
        payload = {
            "Name": label,
            "AccountType": "Bank",
            "AccountSubType": "Checking",
            "AcctNum": num,
        }
        created = self._request("POST", "/account", json=payload)
        acc_data = created["json"].get("Account")
        if created["status"] >= 400 or not acc_data or not acc_data.get("Id"):
            # Check if creation failed because an account with this name already exists
            fault = created["json"].get("Fault", {})
            err_msg = ""
            if fault.get("Error"):
                err_msg = fault["Error"][0].get("Message", "")
            if "already exists" in err_msg.lower() or "duplicate" in err_msg.lower():
                name_q = f"select * from Account where Name = '{label}' maxresults 1"
                r = self._request("GET", f"/query?query={urllib.parse.quote(name_q)}")
                matched = r["json"].get("QueryResponse", {}).get("Account", [])
                if matched:
                    return {"id": str(matched[0]["Id"]), "name": matched[0]["Name"]}
            raise QuickBooksError(f"Could not create QuickBooks bank account '{label}': {created['json']}")

        return {"id": str(acc_data["Id"]), "name": acc_data["Name"]}

    def _find_or_create_suspense_account(self) -> str:
        if self.default_account_id:
            return self.default_account_id

        # Look for existing Expense / Suspense account
        query = "select * from Account where AccountType in ('Expense', 'Other Expense') maxresults 200"
        res = self._request("GET", f"/query?query={urllib.parse.quote(query)}")
        accounts = res["json"].get("QueryResponse", {}).get("Account", [])

        for acc in accounts:
            if not acc.get("Active", True):
                continue
            name = acc.get("Name", "").strip().lower()
            if "suspense" in name or "ask my accountant" in name:
                return str(acc["Id"])

        if accounts:
            # Pick first active expense account
            active = [a for a in accounts if a.get("Active", True)]
            if active:
                return str(active[0]["Id"])

        # Create Suspense Expense account if none found
        payload = {
            "Name": "Suspense",
            "AccountType": "Expense",
            "AccountSubType": "OtherBusinessExpenses",
        }
        created = self._request("POST", "/account", json=payload)
        acc_data = created["json"].get("Account")
        if created["status"] < 400 and acc_data and acc_data.get("Id"):
            return str(acc_data["Id"])

        # Fallback to general query
        fallback_q = "select * from Account maxresults 1"
        fb_res = self._request("GET", f"/query?query={urllib.parse.quote(fallback_q)}")
        first = fb_res["json"].get("QueryResponse", {}).get("Account", [])
        if first:
            return str(first[0]["Id"])

        raise QuickBooksError("No usable QuickBooks account found. Set QUICKBOOKS_DEFAULT_ACCOUNT_ID.")

    # ---------------------------------------------------------------- chart of accounts
    def list_accounts(self) -> List[Dict[str, Any]]:
        query = "select * from Account where Active = true maxresults 1000"
        res = self._request("GET", f"/query?query={urllib.parse.quote(query)}")
        accounts = res["json"].get("QueryResponse", {}).get("Account", [])
        return [
            {
                "code": a.get("AcctNum") or a.get("Id"),
                "name": a.get("Name", ""),
                "type": a.get("AccountType"),
                "class": a.get("Classification"),
            }
            for a in accounts
            if a.get("AccountType") != "Bank"
        ]

    # ---------------------------------------------------------------- ledger coding
    PAGE = 1000

    def query_all(self, entity: str, where: str = "") -> List[Dict[str, Any]]:
        """Run `select * from <entity> [where ...]` and follow STARTPOSITION paging until exhausted."""
        out: List[Dict[str, Any]] = []
        start = 1
        while True:
            q = f"select * from {entity} {where} startposition {start} maxresults {self.PAGE}".replace("  ", " ")
            res = self._request("GET", f"/query?query={urllib.parse.quote(q)}")
            if res["status"] == 400:
                raise QuickBooksError(f"QuickBooks rejected the {entity} query: {self.fault_text(res['json'])}")
            batch = res["json"].get("QueryResponse", {}).get(entity, [])
            out.extend(batch)
            if len(batch) < self.PAGE:
                return out
            start += self.PAGE

    @staticmethod
    def fault_text(payload: Dict[str, Any]) -> str:
        errors = (payload.get("Fault") or {}).get("Error") or []
        if errors:
            return "; ".join(f"{e.get('Message', '')} {e.get('Detail', '')}".strip() for e in errors)[:400]
        return str(payload)[:300]

    def update_entity(self, path: str, body: Dict[str, Any]) -> Optional[str]:
        """POST an (sparse) update. Returns None on success, otherwise the error text."""
        res = self._request("POST", path, json=body)
        if res["status"] >= 400 or (res["json"] or {}).get("Fault"):
            return self.fault_text(res["json"]) or f"HTTP {res['status']}"
        return None

    # ---------------------------------------------------------------- export
    def export_rows(self, ledger: Dict[str, Any], rows: List[LedgerRow]) -> Dict[str, Any]:
        bank = self._find_or_create_bank_account(ledger)
        suspense_id = self._find_or_create_suspense_account()

        imported_hashes: List[str] = []
        errors: List[str] = []

        # Process in batches using QuickBooks Batch API
        for start in range(0, len(rows), BATCH_SIZE):
            chunk = rows[start:start + BATCH_SIZE]
            batch_items = []

            for idx, r in enumerate(chunk):
                b_id = f"item_{start + idx}"
                amount = round(r.amount, 2)
                desc = (r.description or "Bank transaction")[:4000]
                ref = (r.reference or r.description or "")[:4000]

                if r.is_credit:
                    # Deposit for money received
                    deposit_payload = {
                        "TxnDate": r.date.isoformat(),
                        "DepositToAccountRef": {"value": bank["id"]},
                        "PrivateNote": ref,
                        "Line": [{
                            "Amount": amount,
                            "Description": desc,
                            "DetailType": "DepositLineDetail",
                            "DepositLineDetail": {
                                "AccountRef": {"value": suspense_id}
                            }
                        }]
                    }
                    batch_items.append({
                        "bId": b_id,
                        "operation": "create",
                        "Deposit": deposit_payload
                    })
                else:
                    # Purchase (Cash expense) for money spent
                    purchase_payload = {
                        "PaymentType": "Cash",
                        "AccountRef": {"value": bank["id"]},
                        "TxnDate": r.date.isoformat(),
                        "PrivateNote": ref,
                        "Line": [{
                            "Amount": amount,
                            "Description": desc,
                            "DetailType": "AccountBasedExpenseLineDetail",
                            "AccountBasedExpenseLineDetail": {
                                "AccountRef": {"value": suspense_id}
                            }
                        }]
                    }
                    batch_items.append({
                        "bId": b_id,
                        "operation": "create",
                        "Purchase": purchase_payload
                    })

            resp = self._request("POST", "/batch", json={"BatchItemRequest": batch_items})
            results = resp["json"].get("BatchItemResponse", [])

            if not results:
                errors.append(f"Batch starting at row {start + 1}: {str(resp['json'])[:300]}")
                continue

            # Map responses back to rows
            res_by_id = {item.get("bId"): item for item in results}
            for idx, r in enumerate(chunk):
                b_id = f"item_{start + idx}"
                item_res = res_by_id.get(b_id)
                if not item_res:
                    errors.append(f"{r.date} {r.description[:40]}: No response from QuickBooks")
                    continue

                fault = item_res.get("Fault")
                if fault:
                    err_msgs = "; ".join(
                        e.get("Message", "") + (f": {e.get('Detail')}" if e.get("Detail") else "")
                        for e in fault.get("Error", [])
                    )
                    errors.append(f"{r.date} {r.description[:40]}: {err_msgs or 'Rejected by QuickBooks'}")
                else:
                    imported_hashes.append(r.tx_hash)

        # Deep link URL into QuickBooks Online Register
        qbo_domain = "sandbox.qbo.intuit.com" if self.environment == "sandbox" else "app.qbo.intuit.com"
        view_url = f"https://{qbo_domain}/app/register?accountId={bank['id']}"

        return {
            "organization": self.company_name or f"Company {self.realm_id}",
            "bank_account": bank["name"],
            "imported_hashes": imported_hashes,
            "failed": len(rows) - len(imported_hashes),
            "errors": errors[:20],
            "url": view_url,
        }

