"""Xero: every organisation the login has authorised is a selectable client."""
import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session

from app.database.models import User
from app.services import connection_store as store
from app.services import ledger_coding as lc
from app.services.providers.base import LedgerProvider, Progress, ProviderError, stored_values
from app.services.xero_service import XeroError, XeroService


class XeroLedgerProvider(LedgerProvider):
    key = "xero"
    label = "Xero"

    def __init__(self, db: Session, user: User):
        self._db, self._user = db, user

    def _service(self, client_id: Optional[str] = None) -> XeroService:
        values = store.resolve(self._db, self._user, "xero")
        if client_id:
            values = {**values, "tenant_id": client_id}

        # Refreshed tokens belong to the *login*, not to whichever client we are looking at, so never
        # write tenant_id/name back here (that would silently change the saved default organisation).
        def persist(tokens: Dict[str, Any]) -> None:
            store.save(self._db, self._user, "xero",
                       public={"expires_at": tokens.get("expires_at")},
                       secret={"access_token": tokens.get("access_token"), "refresh_token": tokens.get("refresh_token")})

        return XeroService(values, persist_tokens=persist,
                           reload_tokens=lambda: stored_values(self._user.id, "xero"))

    def _wrap(self, fn):
        try:
            return fn()
        except XeroError as exc:
            raise ProviderError(str(exc))

    def default_client_id(self) -> Optional[str]:
        return store.resolve(self._db, self._user, "xero").get("tenant_id")

    def list_clients(self) -> List[Dict[str, str]]:
        tenants = self._wrap(lambda: self._service().list_tenants())
        return [{"id": t["tenantId"], "name": t.get("tenantName") or t["tenantId"]}
                for t in tenants if t.get("tenantType", "ORGANISATION") == "ORGANISATION"]

    def list_accounts(self, client_id: Optional[str]) -> List[Dict[str, Any]]:
        return self._wrap(lambda: self._service(client_id).list_accounts())

    def list_suppliers(self, client_id: Optional[str]) -> List[Dict[str, Any]]:
        return self._wrap(lambda: self._service(client_id).list_suppliers())

    def fetch_ledger(self, client_id: Optional[str], date_from: datetime.date, date_to: datetime.date,
                     progress: Progress = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        txs = self._wrap(lambda: self._service(client_id).list_bank_transactions(date_from, date_to, progress))
        return lc.build_items(txs)

    def apply_changes(self, client_id: Optional[str], changes: List[Dict[str, Any]],
                      cache: Dict[str, Any]) -> Dict[str, Optional[str]]:
        payloads = lc.build_update_payloads(changes, cache)
        return self._wrap(lambda: self._service(client_id).update_bank_transactions(payloads))
