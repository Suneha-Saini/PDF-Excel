"""Client workflow API: one review of one client for one BAS period."""
import datetime
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.database.connection import get_db
from app.database.models import AuditLog, ClientWorkflow, User, WorkflowFinding
from app.security import auth
from app.services import client_settings as client_cfg
from app.services import providers as prov
from app.services import workflow as wfs
from app.services.providers.base import ProviderError

router = APIRouter()


class CreateIn(BaseModel):
    provider: str
    client_id: str
    period_start: Optional[str] = None          # first day of the period; default = the most recent period that has ended


class PatchIn(BaseModel):
    status: Optional[str] = None                # not_started | in_review (sign-off has its own action)
    assigned_to: Optional[str] = None
    notes: Optional[str] = None
    due_date: Optional[str] = None
    checklist: Optional[Dict[str, bool]] = None
    bas_figures: Optional[Dict[str, Optional[float]]] = None


class RunIn(BaseModel):
    stage: str = "all"                          # gl | bas | all


class FindingIn(BaseModel):
    status: str
    note: Optional[str] = Field(default=None, max_length=1000)


class SignOffIn(BaseModel):
    by: Optional[str] = Field(default=None, max_length=120)


def _client(db: Session, user: User, provider: str, client_id: str):
    """System, validated client (one this login can access) - never trust an id sent by the browser."""
    overview = {o["key"]: o for o in prov.provider_overview(db, user)}
    if provider not in overview or not overview[provider]["ready"]:
        raise HTTPException(status_code=400, detail="That accounting system is not connected.")
    system = prov.provider_for(provider, db, user)
    try:
        client = next((c for c in system.list_clients() if c["id"] == client_id), None)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if client is None:
        raise HTTPException(status_code=400, detail="Choose a client from the list for this accounting system.")
    return system, client


def _own(db: Session, user: User, workflow_id: str) -> ClientWorkflow:
    try:
        wf = db.get(ClientWorkflow, uuid.UUID(workflow_id))
    except ValueError:
        wf = None
    if not wf or wf.user_id != user.id:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    return wf


def _editable(wf: ClientWorkflow) -> None:
    if wf.status == "signed_off":
        raise HTTPException(status_code=409, detail="This review is signed off. Reopen it to change anything.")


def _date(value: str, what: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{what} must be a date like 2026-10-28.")


def _view(db: Session, wf: ClientWorkflow) -> Dict[str, Any]:
    settings = client_cfg.get(db, wf.user_id, wf.provider, wf.client_id)
    stage_list = wfs.stages(db, wf, settings)
    findings = wfs.findings_of(db, wf)
    today = datetime.date.today()
    days_left = (datetime.date.fromisoformat(wf.due_date) - today).days if wf.due_date else None
    est = ((wf.checks or {}).get("bas") or {}).get("estimate")
    return {
        "id": str(wf.id), "provider": wf.provider, "client_id": wf.client_id, "client_name": wf.client_name, "kind": wf.kind.upper(),
        "period": {"start": wf.period_start, "end": wf.period_end, "label": wf.label},
        "due_date": wf.due_date, "days_left": days_left, "status": wf.status, "assigned_to": wf.assigned_to, "notes": wf.notes,
        "checklist": {k: bool((wf.checklist or {}).get(k)) for k in wfs.CHECKLIST}, "checklist_labels": wfs.CHECKLIST,
        "bas_figures": {k: (wf.bas_figures or {}).get(k) for k in wfs.BAS_FIGURES}, "bas_estimate": est,
        "settings": {k: settings[k] for k in ("entity_type", "gst_registration", "bas_frequency")},
        "stages": stage_list,
        "findings": [{"id": f.id, "stage": f.stage, "severity": f.severity, "kind": f.kind, "text": f.text, "status": f.status,
                      "note": f.note, "detail": f.detail} for f in findings],
        "findings_summary": {"open": len(wfs._open(findings)), "high_open": len(wfs._open(findings, severity="High")), "total": len(findings)},
        "signed_off": {"by": wf.signed_off_by, "at": wf.signed_off_at.isoformat() + "Z" if wf.signed_off_at else None} if wf.status == "signed_off" else None,
        "can_sign_off": wf.status != "signed_off" and not wfs.signoff_blockers(stage_list, findings),
        "blockers": [] if wf.status == "signed_off" else wfs.signoff_blockers(stage_list, findings),
    }


# ------------------------------------------------------------------ list / create / read
@router.get("")
def list_workflows(provider: str, client_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    rows = (db.query(ClientWorkflow).filter(ClientWorkflow.user_id == user.id, ClientWorkflow.provider == provider,
                                            ClientWorkflow.client_id == client_id).order_by(ClientWorkflow.period_end.desc()).all())
    out = []
    for wf in rows:
        v = _view(db, wf)
        out.append({k: v[k] for k in ("id", "kind", "period", "due_date", "days_left", "status", "assigned_to", "findings_summary")} |
                   {"stages": [{"key": s["key"], "status": s["status"]} for s in v["stages"]]})
    cfg = client_cfg.get(db, user.id, provider, client_id)
    return {"workflows": out, "bas_frequency": cfg["bas_frequency"], "gst_registration": cfg["gst_registration"]}


@router.post("")
def create(payload: CreateIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    _, client = _client(db, user, payload.provider, payload.client_id)
    freq = client_cfg.get(db, user.id, payload.provider, payload.client_id)["bas_frequency"]
    if freq not in wfs.FREQUENCIES:
        raise HTTPException(status_code=400, detail="Set BAS frequency in Client details (Setup tab) first: the period depends on it.")

    if payload.period_start:
        start, end = wfs.bounds(freq, _date(payload.period_start, "period_start"))
        if start.isoformat() != payload.period_start:
            raise HTTPException(status_code=400, detail=f"{payload.period_start} is not the first day of a {freq} BAS period (try {start.isoformat()}).")
    else:
        start, end = wfs.default_period(freq, datetime.date.today())

    dup = (db.query(ClientWorkflow).filter(ClientWorkflow.user_id == user.id, ClientWorkflow.provider == payload.provider,
                                           ClientWorkflow.client_id == payload.client_id, ClientWorkflow.period_start == start.isoformat()).first())
    if dup:
        raise HTTPException(status_code=409, detail=f"A review for {dup.label} already exists.")
    wf = ClientWorkflow(user_id=user.id, provider=payload.provider, client_id=payload.client_id, client_name=client["name"],
                        period_start=start.isoformat(), period_end=end.isoformat(), label=wfs.label(freq, start, end),
                        due_date=wfs.due_date(freq, end).isoformat(), checklist={}, bas_figures={}, checks={})
    db.add(wf)
    db.add(AuditLog(user_id=user.id, action="workflow_create", details=f"Started the {wf.label} BAS review for {client['name']}"))
    db.commit()
    db.refresh(wf)
    return _view(db, wf)


@router.get("/{workflow_id}")
def read(workflow_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    return _view(db, _own(db, user, workflow_id))


# ------------------------------------------------------------------ edit
@router.patch("/{workflow_id}")
def patch(workflow_id: str, payload: PatchIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    wf = _own(db, user, workflow_id)
    _editable(wf)
    if payload.status is not None:
        if payload.status not in ("not_started", "in_review"):
            raise HTTPException(status_code=400, detail="status must be not_started or in_review (use Sign off to finish).")
        wf.status = payload.status
    if payload.assigned_to is not None:
        wf.assigned_to = payload.assigned_to.strip()[:120] or None
    if payload.notes is not None:
        wf.notes = payload.notes[:4000] or None
    if payload.due_date is not None:
        wf.due_date = _date(payload.due_date, "due_date").isoformat()
    if payload.checklist is not None:
        bad = set(payload.checklist) - set(wfs.CHECKLIST)
        if bad:
            raise HTTPException(status_code=400, detail=f"Unknown checklist item(s): {', '.join(sorted(bad))}.")
        wf.checklist = {**(wf.checklist or {}), **payload.checklist}
    if payload.bas_figures is not None:
        bad = set(payload.bas_figures) - set(wfs.BAS_FIGURES)
        if bad:
            raise HTTPException(status_code=400, detail=f"Unknown BAS figure(s): {', '.join(sorted(bad))}. Use {', '.join(wfs.BAS_FIGURES)}.")
        merged = {**(wf.bas_figures or {}), **payload.bas_figures}
        wf.bas_figures = {k: v for k, v in merged.items() if v is not None}
        settings = client_cfg.get(db, user.id, wf.provider, wf.client_id)
        if (wf.checks or {}).get("bas") and settings["gst_registration"] == "cash":
            wfs.run_bas_review(db, wf, settings)       # the reconciliation depends on these figures: redo it now
    wf.updated_at = datetime.datetime.utcnow()
    db.add(wf)
    db.commit()
    return _view(db, wf)


# ------------------------------------------------------------------ run the reviews
@router.post("/{workflow_id}/run")
def run(workflow_id: str, payload: RunIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    wf = _own(db, user, workflow_id)
    _editable(wf)
    if payload.stage not in ("gl", "bas", "all"):
        raise HTTPException(status_code=400, detail="stage must be gl, bas or all.")
    system, _ = _client(db, user, wf.provider, wf.client_id)
    settings = client_cfg.get(db, user.id, wf.provider, wf.client_id)
    results: Dict[str, Any] = {}
    if payload.stage in ("gl", "all"):
        try:
            accounts = system.list_accounts(wf.client_id)
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        results["gl"] = wfs.run_gl_review(db, wf, accounts, settings)
    if payload.stage in ("bas", "all") and settings["gst_registration"] == "cash":
        results["bas"] = wfs.run_bas_review(db, wf, settings)
    if wf.status == "not_started":
        wf.status = "in_review"
    wf.updated_at = datetime.datetime.utcnow()
    db.add(wf)
    db.add(AuditLog(user_id=user.id, action="workflow_run", details=f"Ran {payload.stage} review for {wf.client_name} {wf.label}"))
    db.commit()
    return _view(db, wf)


# ------------------------------------------------------------------ findings, sign-off
@router.post("/{workflow_id}/findings/{finding_id}")
def set_finding(workflow_id: str, finding_id: int, payload: FindingIn, db: Session = Depends(get_db),
                user: User = Depends(auth.get_current_active_user)):
    wf = _own(db, user, workflow_id)
    _editable(wf)
    f = db.get(WorkflowFinding, finding_id)
    if not f or f.workflow_id != wf.id:
        raise HTTPException(status_code=404, detail="Finding not found.")
    if payload.status not in ("open", "resolved", "accepted"):
        raise HTTPException(status_code=400, detail="status must be open, resolved or accepted.")
    if payload.status == "accepted" and not (payload.note or "").strip():
        raise HTTPException(status_code=400, detail="Accepting a finding needs a note saying why it is fine.")
    f.status, f.note = payload.status, (payload.note or "").strip() or f.note
    f.resolved_at = None if payload.status == "open" else datetime.datetime.utcnow()
    f.updated_at = datetime.datetime.utcnow()
    db.add(f)
    db.add(AuditLog(user_id=user.id, action="workflow_finding", details=f"{payload.status}: {f.text[:120]}"))
    db.commit()
    return _view(db, wf)


@router.post("/{workflow_id}/sign-off")
def sign_off(workflow_id: str, payload: SignOffIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    wf = _own(db, user, workflow_id)
    _editable(wf)
    v = _view(db, wf)
    if v["blockers"]:
        raise HTTPException(status_code=409, detail="Cannot sign off yet: " + "; ".join(v["blockers"]) + ".")
    wf.status, wf.signed_off_at = "signed_off", datetime.datetime.utcnow()
    wf.signed_off_by = (payload.by or wf.assigned_to or "local user").strip()[:120]
    db.add(wf)
    db.add(AuditLog(user_id=user.id, action="workflow_signoff", details=f"{wf.client_name} {wf.label} signed off by {wf.signed_off_by}"))
    db.commit()
    return _view(db, wf)


@router.post("/{workflow_id}/reopen")
def reopen(workflow_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    wf = _own(db, user, workflow_id)
    if wf.status != "signed_off":
        raise HTTPException(status_code=409, detail="This review is not signed off.")
    wf.status, wf.signed_off_at, wf.signed_off_by = "in_review", None, None
    db.add(wf)
    db.add(AuditLog(user_id=user.id, action="workflow_reopen", details=f"Reopened {wf.client_name} {wf.label}"))
    db.commit()
    return _view(db, wf)


@router.delete("/{workflow_id}")
def delete(workflow_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    wf = _own(db, user, workflow_id)
    _editable(wf)
    for f in db.query(WorkflowFinding).filter(WorkflowFinding.workflow_id == wf.id).all():
        db.delete(f)
    db.add(AuditLog(user_id=user.id, action="workflow_delete", details=f"Deleted the {wf.label} review for {wf.client_name}"))
    db.delete(wf)
    db.commit()
    return {"deleted": workflow_id}
