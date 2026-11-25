"""Public webhook receivers (no login: the platforms call these). Every request is authenticated by an HMAC
signature over the raw body; unsigned or wrongly signed requests are rejected before anything is read.

A webhook is only a *trigger*. We record it (idempotently), find which auto-synced clients it concerns, and queue a
sync for each; the sync re-fetches the real data. Return quickly: both platforms time out after a few seconds.

Platform facts (re-check against current docs when you configure them):
  Xero       header x-xero-signature = base64(HMAC-SHA256(webhook key, body)); events cover contacts/invoices
             (and a few more), NOT bank transactions, so bank lines still rely on the nightly sync.
  QuickBooks header intuit-signature = base64(HMAC-SHA256(verifier token, body)); entities incl. Purchase, Deposit.
  Manager.io no webhooks: it is covered by the nightly sync only.
"""
import base64
import hashlib
import hmac
import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.config import settings
from app.database.connection import get_db
from app.database.models import SyncTarget, WebhookEvent
from app.services.autosync import worker

logger = logging.getLogger("app.api.webhooks")
router = APIRouter()

# QuickBooks entities that change what the Ledger page shows.
QBO_RELEVANT = {"purchase", "deposit", "account"}


def signature_ok(secret: str, body: bytes, header: str) -> bool:
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, (header or "").strip())


def _record_and_trigger(db: Session, provider: str, events: Iterable[Tuple[str, str, str]]) -> Dict[str, int]:
    """events: (dedupe_key, client_id, detail). Stores each new one, queues a sync per affected target."""
    stats = {"received": 0, "duplicates": 0, "queued": 0}
    targets_hit: set = set()
    for key, client_id, detail in events:
        targets = (db.query(SyncTarget)
                   .filter(SyncTarget.provider == provider, SyncTarget.client_id == client_id,
                           SyncTarget.enabled == True).all())  # noqa: E712
        db.add(WebhookEvent(provider=provider, dedupe_key=key, client_id=client_id, detail=detail[:200],
                            matched_targets=len(targets)))
        try:
            db.commit()
        except IntegrityError:        # delivered before: platforms retry and batch
            db.rollback()
            stats["duplicates"] += 1
            continue
        stats["received"] += 1
        for t in targets:
            targets_hit.add(str(t.id))
    for tid in targets_hit:
        worker.enqueue(tid, "webhook")
    stats["queued"] = len(targets_hit)
    return stats


# ------------------------------------------------------------------ Xero
@router.post("/xero")
async def xero_webhook(request: Request, db: Session = Depends(get_db)):
    key = settings.XERO_WEBHOOK_KEY
    if not key:
        raise HTTPException(status_code=503, detail="XERO_WEBHOOK_KEY is not configured.")
    body = await request.body()
    if not signature_ok(key, body, request.headers.get("x-xero-signature", "")):
        # Xero's "intent to receive" check expects exactly this for a bad signature.
        return Response(status_code=401)

    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return Response(status_code=400)
    events = []
    for e in payload.get("events", []) or []:
        tenant = e.get("tenantId")
        if not tenant:
            continue
        key_ = "|".join(str(e.get(k, "")) for k in ("tenantId", "eventCategory", "eventType", "resourceId", "eventDateUtc"))
        events.append((f"xero|{key_}", tenant, f"{e.get('eventCategory')} {e.get('eventType')} {e.get('resourceId')}"))
    stats = _record_and_trigger(db, "xero", events)
    logger.info("Xero webhook: %s", stats)
    return Response(status_code=200)     # empty body, quickly


# ------------------------------------------------------------------ QuickBooks
def _qbo_events(payload: Any) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    if isinstance(payload, dict):                        # classic format
        for note in payload.get("eventNotifications", []) or []:
            realm = str(note.get("realmId") or "")
            for ent in ((note.get("dataChangeEvent") or {}).get("entities")) or []:
                name = str(ent.get("name") or "")
                if realm and name.lower() in QBO_RELEVANT:
                    key = "|".join(str(x) for x in (realm, name, ent.get("id"), ent.get("operation"), ent.get("lastUpdated")))
                    out.append((f"quickbooks|{key}", realm, f"{name} {ent.get('operation')} {ent.get('id')}"))
    elif isinstance(payload, list):                      # CloudEvents-style format (shape not verified here)
        for ev in payload:
            realm = str(ev.get("intuitaccountid") or "")
            parts = str(ev.get("type") or "").split(".")          # e.g. qbo.purchase.created.v1
            name = parts[1] if len(parts) > 1 else ""
            if realm and name.lower() in QBO_RELEVANT:
                out.append((f"quickbooks|{ev.get('id') or ev.get('time')}|{realm}", realm, f"{ev.get('type')} {ev.get('intuitentityid')}"))
    return out


@router.post("/quickbooks")
async def quickbooks_webhook(request: Request, db: Session = Depends(get_db)):
    token = settings.QUICKBOOKS_WEBHOOK_VERIFIER_TOKEN
    if not token:
        raise HTTPException(status_code=503, detail="QUICKBOOKS_WEBHOOK_VERIFIER_TOKEN is not configured.")
    body = await request.body()
    if not signature_ok(token, body, request.headers.get("intuit-signature", "")):
        return Response(status_code=401)
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return Response(status_code=400)
    stats = _record_and_trigger(db, "quickbooks", _qbo_events(payload))
    logger.info("QuickBooks webhook: %s", stats)
    return Response(status_code=200)
