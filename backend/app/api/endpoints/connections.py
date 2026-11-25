from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from urllib.parse import quote
import uuid

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from app.api import schemas
from app.config import settings
from app.database.connection import get_db
from app.database.models import User, AuditLog
from app.security import auth
from app.services import connection_store as store
from app.services.manager_service import ManagerService, ManagerError
from app.services.xero_service import XeroService, XeroError
from app.services.quickbooks_service import QuickBooksService, QuickBooksError

router = APIRouter()


def _check(integration: str) -> None:
    if integration not in store.SPECS:
        raise HTTPException(status_code=404, detail=f"Unknown integration '{integration}'.")


def xero_for(db: Session, user: User) -> XeroService:
    """Build a Xero client that persists refreshed tokens back to the database."""
    def persist(tokens: Dict[str, Any]) -> None:
        store.save(
            db, user, "xero",
            public={"tenant_id": tokens.get("tenant_id"), "tenant_name": tokens.get("tenant_name"),
                    "expires_at": tokens.get("expires_at")},
            secret={"access_token": tokens.get("access_token"), "refresh_token": tokens.get("refresh_token")},
        )
    return XeroService(store.resolve(db, user, "xero"), persist_tokens=persist)


def quickbooks_for(db: Session, user: User) -> QuickBooksService:
    """Build a QuickBooks client that persists refreshed tokens back to the database."""
    def persist(tokens: Dict[str, Any]) -> None:
        store.save(
            db, user, "quickbooks",
            public={"realm_id": tokens.get("realm_id"), "company_name": tokens.get("company_name"),
                    "environment": tokens.get("environment"), "expires_at": tokens.get("expires_at")},
            secret={"access_token": tokens.get("access_token"), "refresh_token": tokens.get("refresh_token")},
        )
    return QuickBooksService(store.resolve(db, user, "quickbooks"), persist_tokens=persist)


@router.get("/")
def list_connections(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_active_user),
) -> List[Dict[str, Any]]:
    return [store.public_view(db, current_user, key) for key in store.SPECS]


@router.put("/{integration}")
def save_connection(
    integration: str,
    payload: schemas.ConnectionSave,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_active_user),
):
    _check(integration)
    spec = store.SPECS[integration]
    # Only accept known keys; tokens/tenant for Xero & realm for QuickBooks are written by the OAuth flow, not the form.
    protected = set()
    if integration == "xero":
        protected = {"tenant_id", "tenant_name", "expires_at", "access_token", "refresh_token"}
    elif integration == "quickbooks":
        protected = {"realm_id", "company_name", "expires_at", "access_token", "refresh_token"}
    public = {k: v for k, v in payload.values.items() if k in spec["public"] and k not in protected}
    secret = {k: v for k, v in payload.secrets.items() if k in spec["secret"] and k not in protected}
    store.save(db, current_user, integration, public, secret)
    db.add(AuditLog(user_id=current_user.id, action="update_connection", details=f"Updated {spec['label']} settings"))
    db.commit()
    return store.public_view(db, current_user, integration)


@router.delete("/{integration}")
def delete_connection(
    integration: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_active_user),
):
    _check(integration)
    store.delete(db, current_user, integration)
    return store.public_view(db, current_user, integration)


@router.post("/{integration}/test")
def test_connection(
    integration: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_active_user),
):
    _check(integration)
    try:
        if integration == "xero":
            message = xero_for(db, current_user).test()
        elif integration == "quickbooks":
            message = quickbooks_for(db, current_user).test()
        else:
            values = store.resolve(db, current_user, integration)
            message = ManagerService(values).test()
        store.set_test_result(db, current_user, integration, True, message)
        return {"status": "ok", "message": message}
    except (ManagerError, XeroError, QuickBooksError) as exc:
        store.set_test_result(db, current_user, integration, False, str(exc))
        return {"status": "error", "message": str(exc)}
    except Exception as exc:  # network errors etc.
        store.set_test_result(db, current_user, integration, False, str(exc))
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}


@router.post("/xero/disconnect")
def xero_disconnect(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_active_user),
):
    """Forget the Xero tokens and organisation; saved Client ID/Secret stay so you can reconnect."""
    store.clear_keys(
        db, current_user, "xero",
        public_keys=["tenant_id", "tenant_name", "expires_at"],
        secret_keys=["access_token", "refresh_token"],
    )
    db.add(AuditLog(user_id=current_user.id, action="disconnect_xero", details="Disconnected Xero"))
    db.commit()
    return store.public_view(db, current_user, "xero")


# ------------------------------------------------------------------ Xero OAuth
@router.get("/xero/authorize-url")
def xero_authorize_url(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_active_user),
):
    state = jwt.encode(
        {"sub": str(current_user.id), "purpose": "xero", "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        settings.SECRET_KEY, algorithm=settings.ALGORITHM,
    )
    try:
        return {"url": xero_for(db, current_user).authorize_url(state)}
    except XeroError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/xero/callback", include_in_schema=False)
def xero_callback(code: str = "", state: str = "", error: str = "", db: Session = Depends(get_db)):
    """Xero redirects the browser here. No bearer token is available, so identity comes from the signed `state`."""
    def back(**params: str) -> RedirectResponse:
        query = "&".join(f"{k}={quote(v)}" for k, v in params.items())
        return RedirectResponse(f"{settings.FRONTEND_URL}/?{query}")

    if error:
        return back(xero="error", message=error)
    try:
        claims = jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if claims.get("purpose") != "xero":
            raise ValueError("bad state")
        user = db.get(User, uuid.UUID(claims["sub"]))
        if not user:
            raise ValueError("unknown user")
    except Exception:
        return back(xero="error", message="Invalid or expired sign-in state. Please try again.")

    try:
        svc = xero_for(db, user)
        svc.exchange_code(code)
        db.add(AuditLog(user_id=user.id, action="connect_xero", details=f"Connected Xero organisation {svc.tenant_name}"))
        db.commit()
        return back(xero="connected", org=svc.tenant_name or "")
    except Exception as exc:
        return back(xero="error", message=str(exc)[:200])


# ------------------------------------------------------------------ QuickBooks OAuth
@router.post("/quickbooks/disconnect")
def quickbooks_disconnect(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_active_user),
):
    """Forget the QuickBooks tokens and company; saved Client ID/Secret stay so you can reconnect."""
    store.clear_keys(
        db, current_user, "quickbooks",
        public_keys=["realm_id", "company_name", "expires_at"],
        secret_keys=["access_token", "refresh_token"],
    )
    db.add(AuditLog(user_id=current_user.id, action="disconnect_quickbooks", details="Disconnected QuickBooks Online"))
    db.commit()
    return store.public_view(db, current_user, "quickbooks")


@router.get("/quickbooks/authorize-url")
def quickbooks_authorize_url(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_active_user),
):
    state = jwt.encode(
        {"sub": str(current_user.id), "purpose": "quickbooks", "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        settings.SECRET_KEY, algorithm=settings.ALGORITHM,
    )
    try:
        return {"url": quickbooks_for(db, current_user).authorize_url(state)}
    except QuickBooksError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/quickbooks/callback", include_in_schema=False)
def quickbooks_callback(
    code: str = "",
    state: str = "",
    realmId: str = "",
    error: str = "",
    error_description: str = "",
    db: Session = Depends(get_db),
):
    """QuickBooks redirects the browser here. No bearer token is available, so identity comes from the signed `state`."""
    def back(**params: str) -> RedirectResponse:
        query = "&".join(f"{k}={quote(v)}" for k, v in params.items())
        return RedirectResponse(f"{settings.FRONTEND_URL}/?{query}")

    if error:
        return back(quickbooks="error", message=error_description or error)
    try:
        claims = jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if claims.get("purpose") != "quickbooks":
            raise ValueError("bad state")
        user = db.get(User, uuid.UUID(claims["sub"]))
        if not user:
            raise ValueError("unknown user")
    except Exception:
        return back(quickbooks="error", message="Invalid or expired sign-in state. Please try again.")

    try:
        svc = quickbooks_for(db, user)
        svc.exchange_code(code, realm_id=realmId)
        db.add(AuditLog(user_id=user.id, action="connect_quickbooks", details=f"Connected QuickBooks company {svc.company_name} ({svc.realm_id})"))
        db.commit()
        return back(quickbooks="connected", org=svc.company_name or f"Company {svc.realm_id}")
    except Exception as exc:
        return back(quickbooks="error", message=str(exc)[:200])
