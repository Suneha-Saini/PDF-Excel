"""One interface for every accounting system the Ledger page can work on.

The ledger pipeline (rules -> AI -> checks -> review -> apply) only ever talks to this interface, so adding a
platform means writing one class here. A *client* is one company inside a platform: a Xero organisation, a
QuickBooks company (realm) or a Manager.io business.

Every provider returns ledger "items" in the same shape (see ledger_coding.build_items):
  id, tx_id, line_index, date, description, contact, reference, type, direction ("spend"|"receive"),
  amount (signed: spend < 0), current_code, proposed_code, source, confidence, reason, status, bank_account
"""
import datetime
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Tuple

Progress = Optional[Callable[[str], None]]


def stored_values(user_id, integration: str) -> Dict[str, Any]:
    """A FRESH read of the saved connection settings (own DB session), used to pick up a refresh token another
    thread has just rotated. Imported lazily so tests can point app.database.connection.engine elsewhere."""
    from types import SimpleNamespace

    from sqlmodel import Session

    from app.database import connection
    from app.services import connection_store as store

    with Session(connection.engine) as session:
        return store.resolve(session, SimpleNamespace(id=user_id), integration)


class ProviderError(Exception):
    """A problem the user can act on (not connected, unsupported, rejected by the platform...)."""


class LedgerProvider(ABC):
    key: str = ""
    label: str = ""
    #: False when this platform's ledger code has not been exercised against a real company yet.
    verified: bool = True
    #: Can recoded lines be written back? (Always behind the user's explicit Apply.)
    supports_apply: bool = True
    note: str = ""

    @abstractmethod
    def list_clients(self) -> List[Dict[str, str]]:
        """[{"id": ..., "name": ...}] the user may work on."""

    @abstractmethod
    def default_client_id(self) -> Optional[str]:
        """The client that was chosen when the connection was made (pre-selected in the UI)."""

    @abstractmethod
    def list_accounts(self, client_id: Optional[str]) -> List[Dict[str, Any]]:
        """Chart of accounts usable as a category: [{"code", "name", "type", "class"}]."""

    @abstractmethod
    def fetch_ledger(self, client_id: Optional[str], date_from: datetime.date, date_to: datetime.date,
                     progress: Progress = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Return (items, cache). `cache` keeps whatever apply_changes() needs to write back."""

    @abstractmethod
    def apply_changes(self, client_id: Optional[str], changes: List[Dict[str, Any]],
                      cache: Dict[str, Any]) -> Dict[str, Optional[str]]:
        """Write the changed lines. Returns {tx_id: None if ok else error text}."""

    def list_suppliers(self, client_id: Optional[str]) -> List[Dict[str, Any]]:
        """[{"id", "name", "abn"}] for the supplier checks. Optional: platforms that can't say so raise."""
        raise ProviderError(f"Supplier checks are not available for {self.label or 'this system'} yet.")
