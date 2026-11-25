"""Per-client settings: entity type, GST registration, BAS frequency."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from app.database.connection import get_db
from app.database.models import AuditLog, User
from app.security import auth
from app.services import client_settings as cs
from app.services import providers as prov
from app.services.providers.base import ProviderError

router = APIRouter()


class SettingsIn(BaseModel):
    provider: str
    client_id: str
    entity_type: Optional[str] = None
    gst_registration: Optional[str] = None
    bas_frequency: Optional[str] = None


def _client(db: Session, user: User, provider: str, client_id: str) -> dict:
    """The client must be one this login can access: never trust an id sent by the browser."""
    overview = {o["key"]: o for o in prov.provider_overview(db, user)}
    if provider not in overview or not overview[provider]["ready"]:
        raise HTTPException(status_code=400, detail="That accounting system is not connected.")
    try:
        client = next((c for c in prov.provider_for(provider, db, user).list_clients() if c["id"] == client_id), None)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if client is None:
        raise HTTPException(status_code=400, detail="Choose a client from the list for this accounting system.")
    return client


@router.get("")
def get_settings(provider: str, client_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    return {"settings": cs.get(db, user.id, provider, client_id), "choices": cs.CHOICES, "effects": cs.EFFECTS}


@router.put("")
def put_settings(payload: SettingsIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    client = _client(db, user, payload.provider, payload.client_id)
    values = {f: getattr(payload, f) for f in cs.FIELDS if getattr(payload, f) is not None}
    if not values:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    try:
        changed = cs.save(db, user.id, payload.provider, payload.client_id, values)
    except cs.InvalidSetting as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.add(AuditLog(user_id=user.id, action="client_settings",
                    details=f"{payload.provider}/{client['name']}: " + ", ".join(f"{k}={v}" for k, v in changed.items())))
    db.commit()
    return {"settings": cs.get(db, user.id, payload.provider, payload.client_id), "choices": cs.CHOICES, "effects": cs.EFFECTS}
