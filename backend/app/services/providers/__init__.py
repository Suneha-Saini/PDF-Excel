"""Registry of ledger providers. Keys match the connection ids used by Settings > Deployment."""
from typing import Any, Dict, List

from sqlmodel import Session

from app.database.models import User
from app.services import connection_store as store
from app.services.providers.base import LedgerProvider, ProviderError

ORDER = ["xero", "quickbooks", "manager_cloud", "manager_local"]
LABELS = {"xero": "Xero", "quickbooks": "QuickBooks Online", "manager_cloud": "Manager.io (Cloud)",
          "manager_local": "Manager.io (Local)"}


def provider_for(key: str, db: Session, user: User) -> LedgerProvider:
    # Imported lazily so importing this package never pulls in every platform's dependencies.
    if key == "xero":
        from app.services.providers.xero_provider import XeroLedgerProvider
        return XeroLedgerProvider(db, user)
    if key == "quickbooks":
        from app.services.providers.quickbooks_provider import QuickBooksLedgerProvider
        return QuickBooksLedgerProvider(db, user)
    if key in ("manager_cloud", "manager_local"):
        from app.services.providers.manager_provider import ManagerLedgerProvider
        return ManagerLedgerProvider(db, user, key, LABELS[key])
    raise ProviderError(f"Unknown accounting system '{key}'.")


def provider_overview(db: Session, user: User) -> List[Dict[str, Any]]:
    """What the Ledger page's first dropdown shows: every platform, whether it is connected, and caveats."""
    out = []
    for key in ORDER:
        view = store.public_view(db, user, key)
        p = provider_for(key, db, user)
        ready = view["status"] == "connected" if key in ("xero", "quickbooks") else view["status"] == "configured"
        out.append({"key": key, "label": LABELS[key], "status": view["status"], "ready": ready,
                    "verified": p.verified, "supports_apply": p.supports_apply, "note": p.note})
    return out
