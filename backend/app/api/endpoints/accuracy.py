"""Accuracy dashboard + the per-client auto-approval policy. Figures come only from people's own decisions."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from app.database.connection import get_db
from app.database.models import AuditLog, User
from app.security import auth
from app.services import accuracy as acc
from app.services import providers as prov
from app.services.providers.base import ProviderError

router = APIRouter()


class PolicyIn(BaseModel):
    provider: str
    client_id: str
    auto_override: bool


@router.get("")
def get_accuracy(provider: Optional[str] = None, client_id: Optional[str] = None, days: int = 180,
                 db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Accuracy over the last `days`. With provider+client_id it is that client; without, every client."""
    if days < 7 or days > 1100:
        raise HTTPException(status_code=400, detail="days must be between 7 and 1100.")
    out = acc.summary(db, user.id, provider, client_id, days)
    out["policy"] = acc.policy_status(db, user.id, provider, client_id) if provider and client_id else None
    return out


@router.put("/policy")
def set_policy(payload: PolicyIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Switch the shadow-period override on/off for one client. The client must be one this login can access."""
    overview = {o["key"]: o for o in prov.provider_overview(db, user)}
    if payload.provider not in overview or not overview[payload.provider]["ready"]:
        raise HTTPException(status_code=400, detail="That accounting system is not connected.")
    try:
        client = next((c for c in prov.provider_for(payload.provider, db, user).list_clients() if c["id"] == payload.client_id), None)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if client is None:
        raise HTTPException(status_code=400, detail="Choose a client from the list for this accounting system.")

    acc.set_override(db, user.id, payload.provider, payload.client_id, payload.auto_override)
    db.add(AuditLog(user_id=user.id, action="auto_approval_override",
                    details=f"{'Enabled' if payload.auto_override else 'Disabled'} the auto-approval override for {payload.provider}/{client['name']}"))
    db.commit()
    return acc.policy_status(db, user.id, payload.provider, payload.client_id)
