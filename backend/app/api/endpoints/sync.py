"""Auto-sync settings: which clients are kept in sync, run one now, and webhook status."""
import base64
import datetime
import hashlib
import hmac
import json
import uuid
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.config import settings
from app.database.connection import get_db
from app.database.models import AuditLog, LedgerRun, SyncTarget, User, WebhookEvent
from app.security import auth
from app.services import providers as prov
from app.services.autosync import sync_target, worker
from app.services.providers.base import ProviderError
from app.services.tunnel import detect_public_url

router = APIRouter()
WEBHOOK_PROVIDERS = {"xero": "XERO_WEBHOOK_KEY", "quickbooks": "QUICKBOOKS_WEBHOOK_VERIFIER_TOKEN"}
TEST_MARK = "TEST-"            # ids of events sent by the Send-test button, so the page can label them


class TargetIn(BaseModel):
    provider: str
    client_id: str
    enabled: bool = True
    lookback_days: int = Field(default=120, ge=7, le=400)
    ai_provider: Optional[str] = None      # None = rules only


def _view(db: Session, t: SyncTarget) -> Dict[str, Any]:
    run = (db.query(LedgerRun).filter(LedgerRun.user_id == t.user_id, LedgerRun.provider == t.provider,
                                      LedgerRun.client_id == t.client_id, LedgerRun.auto_sync == True)  # noqa: E712
           .first())
    iso = lambda d: (d.isoformat() + "Z") if d else None
    return {
        "id": str(t.id), "provider": t.provider, "client_id": t.client_id, "client_name": t.client_name,
        "enabled": t.enabled, "lookback_days": t.lookback_days, "ai_provider": t.ai_provider,
        "last_synced_at": iso(t.last_synced_at), "last_trigger": t.last_trigger, "last_status": t.last_status,
        "last_error": t.last_error, "last_summary": t.last_summary, "backoff_until": iso(t.backoff_until),
        "running": bool(t.lease_until and t.lease_until > datetime.datetime.utcnow()),
        "run_id": str(run.id) if run else None,
    }


@router.get("/targets")
def list_targets(provider: Optional[str] = None, client_id: Optional[str] = None,
                 db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    q = db.query(SyncTarget).filter(SyncTarget.user_id == user.id)
    if provider:
        q = q.filter(SyncTarget.provider == provider)
    if client_id:
        q = q.filter(SyncTarget.client_id == client_id)
    return [_view(db, t) for t in q.all()]


@router.put("/targets")
def upsert_target(payload: TargetIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Switch auto-sync on/off for one client. The client must be one this login can actually access."""
    overview = {o["key"]: o for o in prov.provider_overview(db, user)}
    if payload.provider not in overview:
        raise HTTPException(status_code=400, detail="Unknown accounting system.")
    if not overview[payload.provider]["ready"]:
        raise HTTPException(status_code=400, detail=f"{overview[payload.provider]['label']} is not connected.")

    system = prov.provider_for(payload.provider, db, user)
    if not system.verified and payload.provider.startswith("manager"):
        raise HTTPException(status_code=400, detail=system.note)
    try:
        client = next((c for c in system.list_clients() if c["id"] == payload.client_id), None)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if client is None:
        raise HTTPException(status_code=400, detail="Choose a client from the list for this accounting system.")

    t = (db.query(SyncTarget).filter(SyncTarget.user_id == user.id, SyncTarget.provider == payload.provider,
                                     SyncTarget.client_id == payload.client_id).first())
    was_enabled = bool(t and t.enabled)
    if t is None:
        t = SyncTarget(user_id=user.id, provider=payload.provider, client_id=payload.client_id)
    t.client_name, t.enabled = client["name"], payload.enabled
    t.lookback_days, t.ai_provider = payload.lookback_days, payload.ai_provider
    if payload.enabled and not was_enabled:
        t.backoff_until = None          # switching it on again clears an old back-off
    db.add(t)
    db.add(AuditLog(user_id=user.id, action="auto_sync_config",
                    details=f"{'Enabled' if payload.enabled else 'Disabled'} auto-sync for {payload.provider}/{client['name']}"))
    db.commit()
    db.refresh(t)

    if payload.enabled and not was_enabled:
        worker.enqueue(str(t.id), "enable")      # first sync now, so the ledger is there straight away
    return _view(db, t)


def _own_target(db: Session, user: User, target_id: str) -> SyncTarget:
    try:
        t = db.get(SyncTarget, uuid.UUID(target_id))
    except ValueError:
        t = None
    if not t or t.user_id != user.id:
        raise HTTPException(status_code=404, detail="Auto-sync target not found.")
    return t


@router.post("/targets/{target_id}/sync")
def sync_now(target_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    t = _own_target(db, user, target_id)
    if not t.enabled:
        raise HTTPException(status_code=409, detail="Auto-sync is switched off for this client.")
    outcome = sync_target(t.id, trigger="manual", engine=db.get_bind())
    db.refresh(t)
    return {"queued": True, "outcome": outcome, "target": _view(db, t)}


@router.delete("/targets/{target_id}")
def delete_target(target_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    t = _own_target(db, user, target_id)
    db.delete(t)
    db.commit()
    return {"deleted": target_id}


@router.get("/status")
def status(request: Request, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Is the background worker alive, are webhook secrets configured, which public address reaches us, and what
    arrived recently? (Never returns a secret.)"""
    events = db.query(WebhookEvent).order_by(WebhookEvent.id.desc()).limit(15).all()
    public = detect_public_url(request.url.port or 8000)
    return {
        "worker_running": worker.running,
        "sync_enabled": settings.SYNC_ENABLED,
        "nightly_hour": settings.SYNC_NIGHTLY_HOUR,
        "public_url": public,
        "webhooks": {p: {"configured": bool(getattr(settings, attr)), "path": f"{settings.API_V1_STR}/webhooks/{p}",
                         "register_url": f"{public}{settings.API_V1_STR}/webhooks/{p}" if public else None}
                     for p, attr in WEBHOOK_PROVIDERS.items()},
        "recent_events": [{"provider": e.provider, "detail": e.detail, "matched_targets": e.matched_targets,
                           "is_test": TEST_MARK in (e.dedupe_key or ""), "received_at": e.received_at.isoformat() + "Z"} for e in events],
    }


class TestHookIn(BaseModel):
    provider: str
    client_id: str


def _sign(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


@router.post("/test-webhook")
def test_webhook(payload: TestHookIn, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(auth.get_current_active_user)):
    """Send a signed TEST event for this client to our own webhook receiver, through the public tunnel when there is one.
    It proves the tunnel, the receiver, the signature check and the processing (a sync is queued if Auto-sync is on for the
    client). It cannot prove the portal's key matches .env: only a real event from the platform proves that."""
    if payload.provider not in WEBHOOK_PROVIDERS:
        raise HTTPException(status_code=400, detail="Only Xero and QuickBooks send webhooks.")
    attr = WEBHOOK_PROVIDERS[payload.provider]
    secret = getattr(settings, attr)
    if not secret:
        raise HTTPException(status_code=400, detail=f"{attr} is not set in backend/.env yet (and the backend restarted).")
    overview = {o["key"]: o for o in prov.provider_overview(db, user)}
    if payload.provider not in overview or not overview[payload.provider]["ready"]:
        raise HTTPException(status_code=400, detail="That accounting system is not connected.")
    try:
        client = next((c for c in prov.provider_for(payload.provider, db, user).list_clients() if c["id"] == payload.client_id), None)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if client is None:
        raise HTTPException(status_code=400, detail="Choose a client from the list for this accounting system.")

    now = datetime.datetime.utcnow()
    test_id = f"{TEST_MARK}{int(now.timestamp())}-{uuid.uuid4().hex[:6]}"   # unique per click, so de-duplication never swallows a test
    if payload.provider == "xero":
        body = json.dumps({"events": [{"resourceId": test_id, "eventDateUtc": now.isoformat(timespec="seconds"), "eventType": "UPDATE",
                                       "eventCategory": "CONTACT", "tenantId": payload.client_id, "tenantType": "ORGANISATION"}],
                           "firstEventSequence": 1, "lastEventSequence": 1}).encode()
        header = "x-xero-signature"
    else:
        body = json.dumps({"eventNotifications": [{"realmId": payload.client_id, "dataChangeEvent": {"entities": [
            {"name": "Purchase", "id": test_id, "operation": "Update", "lastUpdated": now.isoformat()}]}}]}).encode()
        header = "intuit-signature"

    port = request.url.port or 8000
    public = detect_public_url(port)
    base, via = (public, "tunnel") if public else (f"http://127.0.0.1:{port}", "local")
    url = f"{base}{settings.API_V1_STR}/webhooks/{payload.provider}"
    try:
        resp = httpx.post(url, content=body, timeout=20, headers={"content-type": "application/json", header: _sign(secret, body),
                                                                  "ngrok-skip-browser-warning": "true"})
    except httpx.HTTPError as exc:
        return {"via": via, "url": url, "ok": False, "status_code": None,
                "explanation": f"Could not reach {url}: {type(exc).__name__}. Is the tunnel still running?"}

    explain = {200: "Received and processed.",
               401: "The receiver rejected the signature. Something changed the request on the way (a proxy rewriting the body).",
               404: "Nothing answered at that path. The tunnel is not pointing at the backend (port 8000), or the path is wrong.",
               503: "The backend has no key loaded. Restart it after editing backend/.env."}
    ev = db.query(WebhookEvent).filter(WebhookEvent.dedupe_key.contains(test_id)).first() if resp.status_code == 200 else None
    return {"via": via, "url": url, "ok": resp.status_code == 200, "status_code": resp.status_code,
            "explanation": explain.get(resp.status_code, f"Unexpected response {resp.status_code}."),
            "recorded": ev is not None, "matched_targets": ev.matched_targets if ev else None,
            "note": ("Went through your public tunnel: tunnel, receiver, signature check and processing all work." if via == "tunnel" else
                     "No public tunnel was detected, so this tested the receiver locally only.") +
                    " Only a real event from the platform proves the portal's key matches .env."}
