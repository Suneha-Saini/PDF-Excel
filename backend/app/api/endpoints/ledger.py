"""Ledger coding API: pick an accounting system and a client, fetch its ledger, categorise it (rules first,
then AI), check that it balances, let the user review, then write the new account codes back.

Works for every provider in app.services.providers (Xero, QuickBooks, Manager.io); a run is tied to one provider
and one client of it for its whole life."""
import datetime
import io
import logging
import threading
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session

from app.ai.manager import get_provider
from app.database.connection import engine, get_db
from app.database.models import AuditLog, CodingRule, LedgerRun, User
from app.security import auth
from app.services import accuracy as acc
from app.services import client_settings as client_cfg
from app.services import ledger_coding as lc
from app.services import payee_rules as payee_svc
from app.services import tax_backfill
from app.services import providers as prov
from app.services.coding import evaluate, setup_issues as setup_review, suppliers as supplier_checks, tax as tax_checks
from app.services.coding.store import client_name_for, load_history
from app.services.providers.base import ProviderError

logger = logging.getLogger("app.api.ledger")
router = APIRouter()
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ------------------------------------------------------------------ schemas
class RuleIn(BaseModel):
    name: str = Field(min_length=1)
    keywords: str = Field(min_length=1)          # comma-separated
    direction: str = "any"                       # any | spend | receive
    account_code: str = Field(min_length=1)
    priority: int = 100
    active: bool = True
    provider: str = "xero"
    client_id: Optional[str] = None              # None = every client of this provider


class RunCreate(BaseModel):
    date_from: str
    date_to: str
    ai_provider: Optional[str] = None            # None = rules only
    provider: str = "xero"                       # xero | quickbooks | manager_cloud | manager_local
    client_id: Optional[str] = None              # None = the client chosen when the connection was made


class ItemChanges(BaseModel):
    changes: Dict[str, Optional[str]]            # item id -> account code (null clears)


# ------------------------------------------------------------------ helpers
def _rule_dicts(db: Session, user: User, provider: str, client_id: Optional[str]) -> List[Dict[str, Any]]:
    """Rules for this provider that apply to this client (client-specific ones plus the provider-wide ones)."""
    rows = db.query(CodingRule).filter(CodingRule.user_id == user.id, CodingRule.provider == provider).all()
    return [{"id": r.id, "name": r.name, "keywords": r.keywords, "direction": r.direction,
             "account_code": r.account_code, "priority": r.priority, "active": r.active,
             "provider": r.provider, "client_id": r.client_id}
            for r in rows if not r.client_id or not client_id or r.client_id == client_id]


def _provider(key: str, db: Session, user: User):
    try:
        return prov.provider_for(key, db, user)
    except ProviderError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


def _get_run(db: Session, user: User, run_id: str) -> LedgerRun:
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Run not found.")
    run = db.get(LedgerRun, rid)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


def _save(db: Session, run: LedgerRun, **fields) -> None:
    for k, v in fields.items():
        setattr(run, k, v)
        if k in ("items", "accounts", "txs", "apply_results"):
            flag_modified(run, k)
    run.updated_at = datetime.datetime.utcnow()
    db.add(run)
    db.commit()


def _view(run: LedgerRun, with_items: bool = True) -> Dict[str, Any]:
    label = prov.LABELS.get(run.provider or "xero", run.provider or "Xero")
    out: Dict[str, Any] = {
        "id": str(run.id), "organization": run.organization, "date_from": run.date_from, "date_to": run.date_to,
        "provider": run.provider or "xero", "provider_label": label, "client_id": run.client_id, "auto_sync": bool(run.auto_sync),
        "ai_provider": run.ai_provider, "status": run.status, "progress": run.progress, "error": run.error,
        "apply_results": run.apply_results, "updated_at": run.updated_at.isoformat(),
    }
    if with_items and run.items is not None:
        accounts = run.accounts or []
        out["accounts"] = accounts
        out["items"] = run.items
        out["report"] = lc.run_checks(run.items, accounts, system=label)
        out["pending_changes"] = len(lc.changes_to_apply(run.items))
    return out


# ------------------------------------------------------------------ providers + clients
@router.get("/providers")
def list_providers(db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Every accounting system, whether it is connected, and any caveat. Drives the first dropdown."""
    return prov.provider_overview(db, user)


@router.get("/providers/{key}/clients")
def list_clients(key: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """The companies the user can work on in this system (Xero organisations, QuickBooks company, ...)."""
    provider = _provider(key, db, user)
    try:
        return {"clients": provider.list_clients(), "default": provider.default_client_id()}
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/providers/{key}/diagnose")
def diagnose_provider(key: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Manager.io only: report which API routes the instance answers (used to finish the ledger mapping)."""
    provider = _provider(key, db, user)
    if not hasattr(provider, "diagnose"):
        raise HTTPException(status_code=404, detail="No diagnostic for this system.")
    try:
        return {"routes": provider.diagnose()}
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ------------------------------------------------------------------ rules
@router.get("/rules")
def list_rules(provider: str = "xero", client_id: Optional[str] = None,
               db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    return sorted(_rule_dicts(db, user, provider, client_id), key=lambda r: (r["priority"], r["id"]))


@router.post("/rules")
def create_rule(payload: RuleIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    if payload.direction not in ("any", "spend", "receive"):
        raise HTTPException(status_code=400, detail="direction must be any, spend or receive")
    if payload.provider not in prov.ORDER:
        raise HTTPException(status_code=400, detail="Unknown accounting system.")
    rule = CodingRule(user_id=user.id, **payload.model_dump())
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return {"id": rule.id, **payload.model_dump()}


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, payload: RuleIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    rule = db.get(CodingRule, rule_id)
    if not rule or rule.user_id != user.id:
        raise HTTPException(status_code=404, detail="Rule not found.")
    if payload.direction not in ("any", "spend", "receive"):
        raise HTTPException(status_code=400, detail="direction must be any, spend or receive")
    for k, v in payload.model_dump().items():
        setattr(rule, k, v)
    db.add(rule)
    db.commit()
    return {"id": rule.id, **payload.model_dump()}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    rule = db.get(CodingRule, rule_id)
    if not rule or rule.user_id != user.id:
        raise HTTPException(status_code=404, detail="Rule not found.")
    db.delete(rule)
    db.commit()
    return {"deleted": rule_id}


# ------------------------------------------------------------------ chart of accounts
@router.get("/accounts")
def chart_of_accounts(provider: str = "xero", client_id: Optional[str] = None,
                      db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    try:
        return _provider(provider, db, user).list_accounts(client_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ------------------------------------------------------------------ runs
def _run_job(run_id: uuid.UUID) -> None:
    with Session(engine) as db:
        run = db.get(LedgerRun, run_id)
        user = db.get(User, run.user_id)

        def progress(text: str) -> None:
            _save(db, run, progress=text)

        try:
            system = prov.provider_for(run.provider or "xero", db, user)   # the accounting system (Xero/QBO/...)
            progress(f"Reading chart of accounts from {system.label}...")
            accounts = system.list_accounts(run.client_id)
            items, cache = system.fetch_ledger(
                run.client_id, datetime.date.fromisoformat(run.date_from), datetime.date.fromisoformat(run.date_to), progress)

            progress(f"Applying your rules to {len(items)} ledger lines...")
            lc.apply_payee_rules(items, payee_svc.rule_dicts(db, user.id, run.provider or "xero", run.client_id), accounts)
            lc.apply_rules(items, _rule_dicts(db, user, run.provider or "xero", run.client_id))
            progress("Learning from how this client's earlier lines were coded...")
            lc.apply_history(items, load_history(db, user.id, run.provider or "xero", run.client_id, run.organization,
                                                 exclude_run_id=run.id), accounts,
                             auto_approve=acc.policy_status(db, user.id, run.provider or "xero", run.client_id)["active"])
            _save(db, run, status="categorising", accounts=accounts)

            if run.ai_provider:
                ai = get_provider(run.ai_provider, db, user.id)             # the AI model: a different thing
                lc.ai_categorise(items, accounts, ai, progress)
            lc.refresh(items, accounts)

            _save(db, run, items=items, txs=cache, status="ready", progress=f"{len(items)} lines ready for review")
            db.add(AuditLog(user_id=user.id, action="ledger_run",
                            details=f"Fetched {len(items)} ledger lines from {system.label} / {run.organization} ({run.date_from}..{run.date_to})"))
            db.commit()
        except Exception as exc:
            logger.exception("Ledger run failed")
            _save(db, run, status="failed", error=str(exc)[:500])


@router.post("/runs")
def create_run(payload: RunCreate, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    try:
        d_from, d_to = datetime.date.fromisoformat(payload.date_from), datetime.date.fromisoformat(payload.date_to)
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD.")
    if d_from > d_to:
        raise HTTPException(status_code=400, detail="'From' date must be before 'To' date.")

    overview = {o["key"]: o for o in prov.provider_overview(db, user)}
    if payload.provider not in overview:
        raise HTTPException(status_code=400, detail="Unknown accounting system.")
    if not overview[payload.provider]["ready"]:
        raise HTTPException(status_code=400, detail=f"{overview[payload.provider]['label']} is not connected. Open Settings > Deployment first.")

    provider = _provider(payload.provider, db, user)
    try:
        clients = provider.list_clients()
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    client_id = payload.client_id or provider.default_client_id()
    # The client must be one this login actually has access to: never trust an id sent by the browser.
    client = next((c for c in clients if c["id"] == client_id), None)
    if client is None:
        raise HTTPException(status_code=400, detail="Choose a client from the list for this accounting system.")

    run = LedgerRun(user_id=user.id, provider=payload.provider, client_id=client["id"], organization=client["name"],
                    date_from=payload.date_from, date_to=payload.date_to, ai_provider=payload.ai_provider)
    db.add(run)
    db.commit()
    db.refresh(run)
    threading.Thread(target=_run_job, args=(run.id,), daemon=True).start()
    return {"id": str(run.id)}


@router.get("/runs")
def list_runs(provider: Optional[str] = None, client_id: Optional[str] = None,
              db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Recent runs. With provider/client_id: that client's runs, most recently UPDATED first, so the page can show
    the ledger of whichever client was just selected (including the one auto-sync keeps current)."""
    q = db.query(LedgerRun).filter(LedgerRun.user_id == user.id)
    if provider:
        q = q.filter(LedgerRun.provider == provider)
    if client_id:
        q = q.filter(LedgerRun.client_id == client_id)
    order = LedgerRun.updated_at.desc() if (provider or client_id) else LedgerRun.created_at.desc()
    return [_view(r, with_items=False) for r in q.order_by(order).limit(20).all()]


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    return _view(_get_run(db, user, run_id))


def _require_ready(run: LedgerRun) -> None:
    if run.status != "ready" or run.items is None:
        raise HTTPException(status_code=409, detail=f"Run is '{run.status}', not ready for this action.")


@router.patch("/runs/{run_id}/items")
def edit_items(run_id: str, payload: ItemChanges, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    run = _get_run(db, user, run_id)
    _require_ready(run)
    items = run.items
    by_id = {i["id"]: i for i in items}
    for item_id, code in payload.changes.items():
        it = by_id.get(item_id)
        if not it or it["status"] == "locked":
            continue
        before = dict(it)                      # what the suggestion was, before the person changed it
        if code:
            it.update(proposed_code=code, source="manual", confidence=1.0, reason="Set by you", auto_approved=False)
        else:
            it.update(proposed_code=None, source=None, confidence=None, reason="", approved=False, auto_approved=False)
        acc.record_decision(db, run, before, code or None, "edit")     # a person's own decision on a suggestion
        if before.get("source") == "payee" and (code or None) != before.get("proposed_code"):
            payee_svc.note_override(db, before.get("payee_rule_id"))    # the rule may be stale
    lc.refresh(items, run.accounts or [])
    _save(db, run, items=items)            # commits the decisions too
    return _view(run)


@router.post("/runs/{run_id}/keep-current")
def keep_current(run_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Accept the existing Xero code for every line that still has no proposal."""
    run = _get_run(db, user, run_id)
    _require_ready(run)
    items = run.items
    for it in items:
        if it["status"] == "unassigned" and it["current_code"]:
            it.update(proposed_code=it["current_code"], source="keep", confidence=1.0, reason="Kept current code")
    lc.refresh(items, run.accounts or [])
    _save(db, run, items=items)
    return _view(run)


@router.post("/runs/{run_id}/items/{item_id}/confirm")
def confirm_item(run_id: str, item_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """A person checks ONE suggestion and keeps it. This is the decision that counts towards accuracy."""
    run = _get_run(db, user, run_id)
    _require_ready(run)
    items = run.items
    it = next((i for i in items if i["id"] == item_id), None)
    if it is None:
        raise HTTPException(status_code=404, detail="Line not found.")
    if it["status"] != "review" or not it.get("proposed_code"):
        raise HTTPException(status_code=409, detail="This line is not waiting for review.")
    if it["proposed_code"] not in {a["code"] for a in (run.accounts or [])}:
        raise HTTPException(status_code=409, detail="The suggested account is not in the chart of accounts.")
    acc.record_decision(db, run, dict(it), it["proposed_code"], "line")
    it["approved"] = True
    lc.refresh(items, run.accounts or [])
    _save(db, run, items=items)
    return _view(run)


@router.post("/runs/{run_id}/accept-review")
def accept_review(run_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Approve every low-confidence suggestion as-is (lines with an unknown code stay in review)."""
    run = _get_run(db, user, run_id)
    _require_ready(run)
    items = run.items
    known = {a["code"] for a in (run.accounts or [])}
    for it in items:
        if it["status"] == "review" and it["proposed_code"] in known:
            acc.record_decision(db, run, dict(it), it["proposed_code"], "bulk")   # logged, but NOT counted as a check
            it["approved"] = True
    lc.refresh(items, run.accounts or [])
    _save(db, run, items=items)
    return _view(run)


class RetryAI(BaseModel):
    ai_provider: str
    all_lines: bool = False   # True: re-categorise everything except rule matches and your manual edits


def _ai_job(run_id: uuid.UUID, provider_name: str, all_lines: bool = False) -> None:
    with Session(engine) as db:
        run = db.get(LedgerRun, run_id)
        user = db.get(User, run.user_id)
        try:
            items = run.items
            provider = get_provider(provider_name, db, user.id)
            lc.ai_categorise(items, run.accounts or [], provider,
                             lambda t: _save(db, run, progress=t), only_unassigned=not all_lines)
            lc.refresh(items, run.accounts or [])
            _save(db, run, items=items, ai_provider=provider_name, status="ready", progress="AI categorisation finished")
        except Exception as exc:
            logger.exception("AI retry failed")
            _save(db, run, status="ready", progress=f"AI failed: {str(exc)[:200]}")


@router.post("/runs/{run_id}/retry-ai")
def retry_ai(run_id: str, payload: RetryAI, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Run the AI again on lines that still have no category (e.g. after fixing the model/API key)."""
    run = _get_run(db, user, run_id)
    _require_ready(run)
    _save(db, run, status="categorising", progress="Starting AI categorisation...")
    threading.Thread(target=_ai_job, args=(run.id, payload.ai_provider, payload.all_lines), daemon=True).start()
    return _view(run, with_items=False)


@router.post("/runs/{run_id}/reapply-rules")
def reapply_rules(run_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    run = _get_run(db, user, run_id)
    _require_ready(run)
    items = run.items
    lc.apply_payee_rules(items, payee_svc.rule_dicts(db, user.id, run.provider or "xero", run.client_id), run.accounts or [])
    lc.apply_rules(items, _rule_dicts(db, user, run.provider or "xero", run.client_id), overwrite=True)
    lc.apply_history(items, load_history(db, user.id, run.provider or "xero", run.client_id, run.organization,
                                         exclude_run_id=run.id), run.accounts or [],
                     auto_approve=acc.policy_status(db, user.id, run.provider or "xero", run.client_id)["active"])
    lc.refresh(items, run.accounts or [])
    _save(db, run, items=items)
    return _view(run)


def _apply_job(run_id: uuid.UUID) -> None:
    with Session(engine) as db:
        run = db.get(LedgerRun, run_id)
        user = db.get(User, run.user_id)
        try:
            items = run.items
            changes = lc.changes_to_apply(items)
            provider = prov.provider_for(run.provider or "xero", db, user)
            _save(db, run, progress=f"Updating {len({c['tx_id'] for c in changes})} transactions in {provider.label}...")
            results = provider.apply_changes(run.client_id, changes, run.txs or {})

            ok_tx = {t for t, err in results.items() if err is None}
            errors = [f"{run.txs[t]['Date']} {t[:8]}: {err}" for t, err in results.items() if err]
            done = 0
            acc.record_apply(db, run, [it for it in changes if it["tx_id"] in ok_tx])   # unreviewed lines: logged, not counted
            for it in changes:
                if it["tx_id"] in ok_tx:
                    it["current_code"] = it["proposed_code"]
                    it["status"] = "ok"
                    done += 1
                else:
                    it["reason"] = f"{provider.label} rejected: {results.get(it['tx_id'])}"
                    it["status"] = "review"
            remaining = len(lc.changes_to_apply(items))
            summary = {"lines_changed": done, "transactions_updated": len(ok_tx),
                       "transactions_failed": len(results) - len(ok_tx), "errors": errors[:20]}
            _save(db, run, items=items, apply_results=summary,
                  status="applied" if remaining == 0 else "ready", progress=f"Applied to {provider.label}")
            db.add(AuditLog(user_id=user.id, action="ledger_apply",
                            details=f"Recoded {done} ledger lines in {provider.label} / {run.organization}"))
            db.commit()
        except Exception as exc:
            logger.exception("Apply failed")
            _save(db, run, status="ready", error=str(exc)[:500], progress="Apply failed")


@router.post("/runs/{run_id}/apply")
def apply_run(run_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    run = _get_run(db, user, run_id)
    _require_ready(run)
    provider = _provider(run.provider or "xero", db, user)
    if not provider.supports_apply:
        raise HTTPException(status_code=409, detail=f"Writing changes back to {provider.label} is not supported yet. Download the Excel instead.")
    report = lc.run_checks(run.items, run.accounts or [], system=provider.label)
    if not report["balanced"]:
        raise HTTPException(status_code=409, detail="The ledger does not balance yet. Fix the failed checks before applying.")
    if not lc.changes_to_apply(run.items):
        raise HTTPException(status_code=409, detail="Nothing to change: every line already has its proposed code.")
    _save(db, run, status="applying", error=None, apply_results=None, progress="Starting...")
    threading.Thread(target=_apply_job, args=(run.id,), daemon=True).start()
    return _view(run, with_items=False)


# ------------------------------------------------------------------ tax backfill
class RefreshTaxIn(BaseModel):
    provider: str
    client_id: str


@router.get("/refresh-tax")
def refresh_tax_plan(provider: str, client_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """How many stored lines lack tax information (read-only: nothing is fetched)."""
    runs = tax_backfill._runs(db, user.id, provider, client_id, client_name_for(db, user.id, provider, client_id))
    return tax_backfill.plan(runs)


@router.post("/refresh-tax")
def refresh_tax(payload: RefreshTaxIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Re-read this client's transactions from the platform (READ-ONLY) and add the tax code to stored lines that lack
    one. Never changes suggestions, statuses or decisions."""
    system = _provider(payload.provider, db, user)
    try:
        client = next((c for c in system.list_clients() if c["id"] == payload.client_id), None)
        if client is None:
            raise HTTPException(status_code=400, detail="Choose a client from the list for this accounting system.")
        result = tax_backfill.backfill(db, user.id, system, payload.provider, payload.client_id, client["name"])
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    db.add(AuditLog(user_id=user.id, action="tax_backfill",
                    details=f"Added tax codes to {result['lines_updated']} stored lines for {payload.provider}/{client['name']}"))
    db.commit()
    return result


# ------------------------------------------------------------------ ledger setup review (client level)
@router.get("/setup-issues")
def setup_issues(provider: str, client_id: str, db: Session = Depends(get_db),
                 user: User = Depends(auth.get_current_active_user)):
    """Potential problems in how this client's ledger has been coded, ranked High / Medium / Info. Learns from the
    client's whole history (live runs + imported exports) and reads the live chart of accounts for account types."""
    system = _provider(provider, db, user)
    try:
        client = next((c for c in system.list_clients() if c["id"] == client_id), None)
        if client is None:
            raise HTTPException(status_code=400, detail="Choose a client from the list for this accounting system.")
        accounts = system.list_accounts(client_id)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    history = load_history(db, user.id, provider, client_id, client["name"])
    settings_ = client_cfg.get(db, user.id, provider, client_id)         # entity type / GST registration set by the accountant
    return {**setup_review.analyse(history, accounts, settings_), "client": client["name"], "history_lines": len(history)}


# ------------------------------------------------------------------ insights
@router.get("/runs/{run_id}/insights")
def run_insights(run_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Evidence behind the suggestions: tax-code findings, rule-vs-history conflicts, and a backtest of how well the
    history signal agrees with the books (measured, not asserted)."""
    run = _get_run(db, user, run_id)
    if run.items is None:
        raise HTTPException(status_code=409, detail="Run has no data yet.")
    names = {a["code"]: a["name"] for a in (run.accounts or [])}
    history = load_history(db, user.id, run.provider or "xero", run.client_id, run.organization, exclude_run_id=run.id)
    pool = {i["id"]: i for i in history}
    pool.update({i["id"]: i for i in run.items})
    pool = list(pool.values())

    return {
        "history_lines": len(pool),
        "tax_findings": tax_checks.findings(pool, names),
        "conflicts": [{"id": i["id"], "date": i["date"], "description": i["description"], "reason": i["history_conflict"]}
                      for i in run.items if i.get("history_conflict")],
        "evaluation": evaluate.backtest(pool, names),
    }


@router.get("/runs/{run_id}/suppliers")
def run_suppliers(run_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    """Supplier checks (missing/malformed/duplicate ABNs, near-duplicate names). Reads the platform live."""
    run = _get_run(db, user, run_id)
    system = _provider(run.provider or "xero", db, user)
    try:
        suppliers = system.list_suppliers(run.client_id)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    by_id = {s["id"]: s["name"] for s in suppliers}
    findings = supplier_checks.supplier_findings(suppliers)
    return {"suppliers": len(suppliers), "findings": findings, "names": by_id,
            "note": "ABN checks only prove an ABN is well-formed. They do not confirm it exists or belongs to the supplier (that needs the ABR register)."}


# ------------------------------------------------------------------ Excel export
@router.get("/runs/{run_id}/export")
def export_run(run_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_active_user)):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    run = _get_run(db, user, run_id)
    if run.items is None:
        raise HTTPException(status_code=409, detail="Run has no data yet.")
    accounts = run.accounts or []
    names = {a["code"]: a["name"] for a in accounts}
    report = lc.run_checks(run.items, accounts, system=prov.LABELS.get(run.provider or "xero", "Xero"))

    wb = Workbook()
    head_fill, head_font = PatternFill("solid", start_color="1E293B"), Font(bold=True, color="FFFFFF")

    def sheet(ws, headers, rows, widths):
        ws.append(headers)
        for c in ws[1]:
            c.fill, c.font = head_fill, head_font
        for r in rows:
            ws.append(r)
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"

    ws = wb.active
    ws.title = "General Ledger"
    sheet(ws, ["Date", "Contact", "Description", "Bank account", "Type", "Amount", "Previous code", "Category code",
               "Category", "Source", "Confidence", "Status"],
          [[i["date"], i["contact"], i["description"], i["bank_account"], i["type"], i["amount"], i["current_code"],
            i["proposed_code"], names.get(i["proposed_code"], ""), i["source"], i["confidence"], i["status"]]
           for i in sorted(run.items, key=lambda x: (x["proposed_code"] or "~", x["date"]))],
          [12, 26, 48, 22, 14, 14, 14, 14, 30, 10, 11, 12])
    for row in ws.iter_rows(min_row=2, min_col=6, max_col=6):
        row[0].number_format = "#,##0.00;[Red]-#,##0.00"

    ws2 = wb.create_sheet("Category Summary")
    cats = report["categories"]
    sheet(ws2, ["Code", "Category", "Lines", "Received", "Spent", "Net"],
          [[c["code"], c["name"], c["count"], c["received"], c["spent"], c["net"]] for c in cats],
          [12, 34, 8, 16, 16, 16])
    n = len(cats) + 2
    ws2.append(["", "TOTAL", f"=SUM(C2:C{n - 1})", f"=SUM(D2:D{n - 1})", f"=SUM(E2:E{n - 1})", f"=SUM(F2:F{n - 1})"])
    for c in ws2[n]:
        c.font = Font(bold=True)

    ws3 = wb.create_sheet("Checks")
    sheet(ws3, ["Check", "Result", "Detail"],
          [[c["name"], "PASS" if c["ok"] else ("WARN" if c["level"] == "warn" else "FAIL"), c["detail"]] for c in report["checks"]],
          [38, 10, 90])
    ws3.append([])
    ws3.append([f"Organisation: {run.organization}   Period: {run.date_from} to {run.date_to}   Balanced: {report['balanced']}"])

    buf = io.BytesIO()
    wb.save(buf)
    fname = f"general-ledger_{run.date_from}_{run.date_to}.xlsx"
    return Response(content=buf.getvalue(), media_type=XLSX_MIME,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})
