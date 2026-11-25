"""Manager.io (Cloud and Local) as a ledger-coding source.

Status: connection, chart of accounts and a diagnostic are wired; READING THE LEDGER IS NOT MAPPED YET.
Manager.io's API v2 list/read routes could not be confirmed from public documentation (the existing export code
is likewise marked unverified), and guessing would risk showing wrong data. Run `diagnose()` (exposed at
GET /api/ledger/providers/<manager_cloud|manager_local>/diagnose) against your instance: it reports which API
routes answer, and the ledger mapping can then be finished against real responses.
"""
import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlmodel import Session

from app.database.models import User
from app.services import connection_store as store
from app.services.manager_service import EP_ACCOUNTS, EP_TEST, ManagerError, ManagerService
from app.services.providers.base import LedgerProvider, Progress, ProviderError

DIAGNOSE_PATHS = ["", "/swagger.json", "/openapi.json", "/swagger/v1/swagger.json", EP_TEST, EP_ACCOUNTS]


class ManagerLedgerProvider(LedgerProvider):
    verified = False
    supports_apply = False
    note = "Setup needed: reading the Manager.io ledger is not mapped yet. Use Diagnose to see which API routes your instance exposes."

    def __init__(self, db: Session, user: User, integration: str, label: str):
        self.key, self.label = integration, label
        self._db, self._user = db, user

    def _values(self) -> Dict[str, Any]:
        return store.resolve(self._db, self._user, self.key)

    def _service(self) -> ManagerService:
        return ManagerService(self._values())

    def default_client_id(self) -> Optional[str]:
        v = self._values()
        return v.get("business_id") or v.get("url")

    def list_clients(self) -> List[Dict[str, str]]:
        v = self._values()
        if not (v.get("url") and v.get("secret")):
            return []
        cid = v.get("business_id") or v["url"]
        return [{"id": str(cid), "name": str(v.get("business_id") or v["url"])}]

    def list_accounts(self, client_id: Optional[str]) -> List[Dict[str, Any]]:
        try:
            resp = self._service()._send("GET", EP_ACCOUNTS)
        except (ManagerError, httpx.HTTPError) as exc:
            raise ProviderError(str(exc))
        if resp.status_code != 200:
            raise ProviderError(f"Manager.io returned HTTP {resp.status_code} for the chart of accounts.")
        payload = resp.json()
        rows = payload if isinstance(payload, list) else next((v for v in payload.values() if isinstance(v, list)), [])
        out = []
        for r in rows:
            key = r.get("key") or r.get("Key") or r.get("id")
            name = r.get("name") or r.get("Name")
            if key and name:
                out.append({"code": str(key), "name": str(name), "type": None, "class": None})
        return out

    def fetch_ledger(self, client_id: Optional[str], date_from: datetime.date, date_to: datetime.date,
                     progress: Progress = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        raise ProviderError(self.note)

    def apply_changes(self, client_id: Optional[str], changes: List[Dict[str, Any]],
                      cache: Dict[str, Any]) -> Dict[str, Optional[str]]:
        raise ProviderError("Writing back to Manager.io is not supported yet.")

    def diagnose(self) -> List[Dict[str, Any]]:
        """Which API routes answer on this instance? (status + the start of the body; no credentials are echoed)."""
        svc = self._service()
        out = []
        for path in DIAGNOSE_PATHS:
            try:
                resp = svc._send("GET", path)
                out.append({"path": f"/api2{path}", "status": resp.status_code, "body": resp.text[:400]})
            except (ManagerError, httpx.HTTPError) as exc:
                out.append({"path": f"/api2{path}", "status": None, "body": str(exc)[:200]})
        return out
