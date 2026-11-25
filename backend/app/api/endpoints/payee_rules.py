"""Remembered payee rules: list, propose from history, create from a reviewer's work, edit, delete, undo a batch."""
import datetime
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.database.connection import get_db
from app.database.models import AuditLog, LedgerRun, PayeeRule, User
from app.security import auth
from app.services import payee_rules as svc
from app.services import providers as prov
from app.services.coding.store import client_name_for, load_history
from app.services.coding.text import payee_key
from app.services.providers.base import ProviderError

router = APIRouter()


class Entry(BaseModel):
    payee_key: str = Field(min_length=3)
    payee_display: str = ""
    direction: str
    account_code: str
    tax_label: Optional[str] = None


class CreateIn(BaseModel):
    provider: str
    client_id: str
    entries: List[Entry] = Field(min_length=1, max_length=500)


class FromRunIn(BaseModel):
    item_ids: Optional[List[str]] = None       # None = every line a person decided in the run


class PatchIn(BaseModel):
    active: Optional[bool] = None
    account_code: Optional[str] = None


class UndoIn(BaseModel):
    provider: str
    client_id: str


def _context(db: Session, user: User, provider: str, client_id: str):
    """System, validated client (must be one this login can access), and its chart of accounts."""
    overview = {o["key"]: o for o in prov.provider_overview(db, user)}
    if provider not in overview or not overview[provider]["ready"]:
        raise HTTPException(status_code=400, detail="That accounting system is not connected.")
    system = prov.provider_for(provider, db, user)
    try:
        client = next((c for c in system.list_clients() if c["id"] == client_id), None)
        if client is None:
            raise HTTPException(status_code=400, detail="Choose a client from the list for this accounting system.")
        return system, client, system.list_accounts(client_id)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


def _view(r: PayeeRule, use: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": r.id, "payee": r.payee_display, "payee_key": r.payee_key, "direction": r.direction,
            "account_code": r.account_code, "account_name": r.account_name, "tax_label": r.tax_label, "abn": r.abn,
            "source": r.source, "active": r.active, "overrides": r.overrides, "batch_id": r.batch_id,
            "times": use["times"], "agree": use["agree"], "last_seen": use["last_seen"],
            "created_at": r.created_at.isoformat() + "Z"}


def _own(db: Session, user: User, rule_id: int) -> PayeeRule:
    r = db.get(PayeeRule, rule_id)
    if not r or r.user_id != user.id:
        raise HTTPException(status_code=404, detail="Rule not found.")
    return r


def _names(chart: List[Dict[str, Any]]) -> Dict[str, str]:
    return {str(a["code"]): a["name"] for a in chart}


# ------------------------------------------------------------------ list
@router.get("")
def list_rules(provider: str, client_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    rows = (db.query(PayeeRule).filter(PayeeRule.user_id == user.id, PayeeRule.provider == provider,
                                       PayeeRule.client_id == client_id).order_by(PayeeRule.created_at.desc()).all())
    history = load_history(db, user.id, provider, client_id, client_name_for(db, user.id, provider, client_id))
    use = svc.usage(history, rows)
    last = next((r for r in rows), None)
    batches: Dict[str, int] = {}
    for r in rows:
        batches[r.batch_id] = batches.get(r.batch_id, 0) + 1
    last_batch = {"id": last.batch_id, "rules": batches[last.batch_id], "created_at": last.created_at.isoformat() + "Z"} if last else None
    known = {payee_key(i)[0] for i in history if i.get("current_code")} - {None}
    return {"rules": [_view(r, use[r.id]) for r in rows], "payees_known": len(known), "history_lines": len(history),
            "last_batch": last_batch}


# ------------------------------------------------------------------ propose from history
@router.get("/candidates")
def get_candidates(provider: str, client_id: str, min_lines: int = 3, min_share: float = 0.9,
                   db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Payees that history codes consistently and that have no rule yet. Nothing is created here."""
    if min_lines < 2 or not (0.5 <= min_share <= 1):
        raise HTTPException(status_code=400, detail="min_lines must be at least 2 and min_share between 0.5 and 1.")
    _, client, chart = _context(db, user, provider, client_id)
    existing = {(r.payee_key, r.direction) for r in db.query(PayeeRule).filter(
        PayeeRule.user_id == user.id, PayeeRule.provider == provider, PayeeRule.client_id == client_id).all()}
    history = load_history(db, user.id, provider, client_id, client["name"])
    found = svc.candidates(history, _names(chart), existing, min_lines, min_share)
    return {"candidates": found, "history_lines": len(history)}


# ------------------------------------------------------------------ create
@router.post("")
def create(payload: CreateIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Create rules the user chose (typically ticked from /candidates) as one undoable batch."""
    system, client, chart = _context(db, user, payload.provider, payload.client_id)
    names = _names(chart)
    entries: List[Dict[str, Any]] = []
    for e in payload.entries:
        if e.direction not in ("spend", "receive"):
            raise HTTPException(status_code=400, detail="direction must be spend or receive.")
        if not e.payee_key.startswith(("c:", "d:")):
            raise HTTPException(status_code=400, detail="Unrecognised payee key.")
        if e.account_code not in names:
            raise HTTPException(status_code=400, detail=f"Account {e.account_code} is not in this client's chart of accounts.")
        entries.append({**e.model_dump(), "account_name": names[e.account_code]})
    result = svc.create_rules(db, user.id, payload.provider, payload.client_id, entries, "history", svc.abn_lookup(system, payload.client_id))
    db.add(AuditLog(user_id=user.id, action="payee_rules_create", details=f"Created {result['created']} payee rules for {payload.provider}/{client['name']}"))
    db.commit()
    return result


@router.post("/from-run/{run_id}")
def from_run(run_id: str, payload: FromRunIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """'Remember': turn the lines a PERSON decided in this run into payee rules. A reviewer's correction replaces an
    existing rule for the same payee. Lines coded by the system (rules, history, AI) are never remembered."""
    try:
        run = db.get(LedgerRun, uuid.UUID(run_id))
    except ValueError:
        run = None
    if not run or run.user_id != user.id or run.items is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if not run.client_id:
        raise HTTPException(status_code=409, detail="This run predates client tracking; fetch the ledger again to use payee rules.")

    names = {a["code"]: a["name"] for a in (run.accounts or [])}
    found = svc.remembered_entries(run.items, names, set(payload.item_ids) if payload.item_ids is not None else None)
    if not found["entries"]:
        raise HTTPException(status_code=409, detail="Nothing to remember: no line you coded or approved has a payee. " +
                            (f"Skipped: {found['skipped']}" if found["skipped"] else ""))
    system = prov.provider_for(run.provider or "xero", db, user)
    result = svc.create_rules(db, user.id, run.provider or "xero", run.client_id, found["entries"], "remembered",
                              svc.abn_lookup(system, run.client_id), update_existing=True)
    db.add(AuditLog(user_id=user.id, action="payee_rules_remember",
                    details=f"Remembered {result['created']} new / {result['updated']} updated payee rules for {run.organization}"))
    db.commit()
    return {**result, "skipped_lines": found["skipped"]}


# ------------------------------------------------------------------ edit / delete / undo
@router.patch("/{rule_id}")
def patch(rule_id: int, payload: PatchIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    r = _own(db, user, rule_id)
    if payload.account_code is not None:
        _, _, chart = _context(db, user, r.provider, r.client_id)
        names = _names(chart)
        if payload.account_code not in names:
            raise HTTPException(status_code=400, detail="That account is not in this client's chart of accounts.")
        r.account_code, r.account_name, r.overrides = payload.account_code, names[payload.account_code], 0
    if payload.active is not None:
        r.active = payload.active
    r.updated_at = datetime.datetime.utcnow()
    db.add(r)
    db.commit()
    return {"id": r.id, "account_code": r.account_code, "active": r.active}


@router.delete("/{rule_id}")
def delete(rule_id: int, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    r = _own(db, user, rule_id)
    db.add(AuditLog(user_id=user.id, action="payee_rule_delete", details=f"Deleted payee rule for {r.payee_display}"))
    db.delete(r)
    db.commit()
    return {"deleted": rule_id}


@router.post("/undo-last-batch")
def undo_last_batch(payload: UndoIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Remove the rules created by the most recent batch. (Rules a Remember merely re-pointed are not undone.)"""
    rows = (db.query(PayeeRule).filter(PayeeRule.user_id == user.id, PayeeRule.provider == payload.provider,
                                       PayeeRule.client_id == payload.client_id).order_by(PayeeRule.created_at.desc()).all())
    if not rows:
        raise HTTPException(status_code=404, detail="No payee rules to undo.")
    batch = rows[0].batch_id
    doomed = [r for r in rows if r.batch_id == batch]
    for r in doomed:
        db.delete(r)
    db.add(AuditLog(user_id=user.id, action="payee_rules_undo", details=f"Undid a batch of {len(doomed)} payee rules"))
    db.commit()
    return {"removed": len(doomed), "batch_id": batch}
