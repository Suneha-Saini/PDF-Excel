"""Automatic sync: keep chosen clients' ledgers up to date and apply the user's rules to what arrives.

  trigger (webhook | nightly | manual | enable)
      -> SyncWorker queue (coalesces bursts, one job at a time)
      -> sync_target(): fetch the ledger window from the platform, merge it into the client's managed run
         WITHOUT losing the user's manual edits / approvals, apply rules, refresh checks.

Nothing here writes to an accounting system. Changes arrive as proposals the user still reviews and applies.
Webhooks are only a *trigger*: the data is always re-fetched from the platform (payloads are thin, and the
nightly pass is the safety net for missed or unsupported events).
"""
import datetime
import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import update
from sqlmodel import Session

from app.ai.manager import get_provider
from app.config import settings
from app.database import connection
from app.database.models import AuditLog, CodingRule, LedgerRun, SyncTarget, User
from app.services import accuracy
from app.services import ledger_coding as lc
from app.services import payee_rules
from app.services import providers as prov
from app.services.coding.store import load_history

logger = logging.getLogger("app.services.autosync")

LEASE_MINUTES = 15
BUSY_RUN_STATES = ("fetching", "categorising", "applying")
BACKOFF = {"rate_limited": datetime.timedelta(minutes=15), "needs_reconnect": datetime.timedelta(hours=6),
           "error": datetime.timedelta(minutes=10)}
CAS_RETRIES = 3


# ------------------------------------------------------------------ merging
def merge_items(old: List[Dict[str, Any]], new: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Take the freshly fetched lines and carry over only what a human decided.

    Kept from the previous run: manual choices, 'keep current' and approved suggestions, plus earlier AI
    suggestions (so AI is not paid for twice). Rule results are NOT kept: they are recomputed, so edited
    rules take effect. Lines that vanished upstream are dropped; new lines arrive unassigned."""
    old_by_id = {i["id"]: i for i in old}
    out = []
    for it in new:
        prev = old_by_id.get(it["id"])
        if prev and it["status"] != "locked":
            human = prev.get("source") in ("manual", "keep") or prev.get("approved")
            if (human or prev.get("source") == "ai") and prev.get("proposed_code"):
                it.update(proposed_code=prev["proposed_code"], source=prev["source"], confidence=prev.get("confidence"),
                          reason=prev.get("reason", ""), approved=bool(prev.get("approved")))
        out.append(it)
    return out


def _summary(items: List[Dict[str, Any]], old_ids: set) -> Dict[str, Any]:
    return {
        "lines": len(items),
        "new_lines": sum(1 for i in items if i["id"] not in old_ids),
        "rule_matched": sum(1 for i in items if i.get("source") == "rule"),
        "unassigned": sum(1 for i in items if i["status"] == "unassigned"),
        "review": sum(1 for i in items if i["status"] == "review"),
        "pending_changes": len(lc.changes_to_apply(items)),
    }


def _rules_for(db: Session, user: User, provider: str, client_id: str) -> List[Dict[str, Any]]:
    rows = db.query(CodingRule).filter(CodingRule.user_id == user.id, CodingRule.provider == provider).all()
    return [{"id": r.id, "name": r.name, "keywords": r.keywords, "direction": r.direction,
             "account_code": r.account_code, "priority": r.priority, "active": r.active}
            for r in rows if not r.client_id or r.client_id == client_id]


# ------------------------------------------------------------------ one sync
def _classify(exc: Exception) -> str:
    text = str(exc).lower()
    if re.search(r"\b429\b", text) or "rate limit" in text or "too many" in text:
        return "rate_limited"
    if re.search(r"\b401\b", text) or any(s in text for s in ("reconnect", "not connected", "unauthor", "invalid_grant", "token refresh failed")):
        return "needs_reconnect"
    return "error"


def sync_target(target_id, trigger: str = "manual", engine=None, now: Optional[datetime.datetime] = None,
                today: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Run one sync for one target. Never raises: the outcome is recorded on the target and returned."""
    eng = engine or connection.engine
    now = now or datetime.datetime.utcnow()
    today = today or datetime.date.today()

    with Session(eng) as db:
        t = db.get(SyncTarget, target_id)
        if not t or not t.enabled:
            return {"status": "skipped", "reason": "disabled or removed"}
        if t.backoff_until and t.backoff_until > now and trigger != "manual":
            return {"status": "skipped", "reason": f"backing off until {t.backoff_until.isoformat()}Z"}
        if t.lease_until and t.lease_until > now:
            return {"status": "skipped", "reason": "a sync is already running"}
        t.lease_until = now + datetime.timedelta(minutes=LEASE_MINUTES)
        db.add(t)
        db.commit()

        user = db.get(User, t.user_id)
        try:
            system = prov.provider_for(t.provider, db, user)
            accounts = system.list_accounts(t.client_id)
            d_from = today - datetime.timedelta(days=t.lookback_days)
            items, cache = system.fetch_ledger(t.client_id, d_from, today, None)
            outcome = _store_into_run(db, t, user, system.label, items, cache, accounts, d_from, today, trigger)
            t.last_status = outcome["status"]
            t.last_error = None
            t.backoff_until = None
            if outcome["status"] == "ok":
                t.last_synced_at = now
                t.last_summary = outcome["summary"]
        except Exception as exc:  # noqa: BLE001 - recorded, classified, never allowed to kill the worker
            kind = _classify(exc)
            logger.warning("Auto-sync failed for %s/%s: %s", t.provider, t.client_id, exc)
            t.last_status, t.last_error = kind, str(exc)[:500]
            t.backoff_until = now + BACKOFF[kind]
            outcome = {"status": kind, "error": t.last_error}
        finally:
            t.lease_until = None
            t.last_trigger = trigger
            db.add(t)
            db.commit()
            db.refresh(t)

        db.add(AuditLog(user_id=t.user_id, action="auto_sync",
                        details=f"{trigger}: {t.provider}/{t.client_name or t.client_id} -> {outcome['status']}"
                                + (f" {outcome['summary']}" if outcome.get("summary") else "")))
        db.commit()
        return outcome


def _store_into_run(db: Session, t: SyncTarget, user: User, label: str, items, cache, accounts,
                    d_from: datetime.date, d_to: datetime.date, trigger: str) -> Dict[str, Any]:
    """Merge into the client's managed run (create it on first sync). Uses compare-and-swap on updated_at so a
    concurrent edit by the user is never overwritten: if the run changed under us, re-read and merge again."""
    rules = _rules_for(db, user, t.provider, t.client_id)

    for _ in range(CAS_RETRIES):
        run = (db.query(LedgerRun)
               .filter(LedgerRun.user_id == user.id, LedgerRun.provider == t.provider,
                       LedgerRun.client_id == t.client_id, LedgerRun.auto_sync == True)  # noqa: E712
               .first())
        if run is not None:
            db.refresh(run)
            if run.status in BUSY_RUN_STATES:
                return {"status": "deferred", "reason": f"run is {run.status}"}

        old = (run.items or []) if run else []
        merged = merge_items(old, [dict(i) for i in items])
        lc.apply_payee_rules(merged, payee_rules.rule_dicts(db, user.id, t.provider, t.client_id), accounts)
        lc.apply_rules(merged, rules)
        lc.apply_history(merged, load_history(db, user.id, t.provider, t.client_id, t.client_name,
                                              exclude_run_id=run.id if run else None), accounts,
                         auto_approve=accuracy.policy_status(db, user.id, t.provider, t.client_id)["active"])
        if t.ai_provider:
            try:
                lc.ai_categorise(merged, accounts, get_provider(t.ai_provider, db, user.id), None, only_unassigned=True)
            except Exception as exc:  # noqa: BLE001 - AI is optional; rules-only result is still valid
                logger.warning("Auto-sync AI step failed: %s", exc)
        lc.refresh(merged, accounts)
        summary = _summary(merged, {i["id"] for i in old})
        progress = f"Auto-synced {datetime.datetime.utcnow():%Y-%m-%d %H:%M} UTC ({trigger}): {summary['lines']} lines, {summary['new_lines']} new"

        if run is None:
            db.add(LedgerRun(user_id=user.id, provider=t.provider, client_id=t.client_id, organization=t.client_name,
                             auto_sync=True, date_from=d_from.isoformat(), date_to=d_to.isoformat(),
                             ai_provider=t.ai_provider, status="ready", progress=progress,
                             items=merged, accounts=accounts, txs=cache))
            db.commit()
            return {"status": "ok", "summary": summary, "created_run": True}

        base = run.updated_at
        res = db.execute(update(LedgerRun)
                         .where(LedgerRun.id == run.id, LedgerRun.updated_at == base)
                         .values(items=merged, accounts=accounts, txs=cache, date_from=d_from.isoformat(),
                                 date_to=d_to.isoformat(), status="ready", progress=progress, error=None,
                                 updated_at=datetime.datetime.utcnow()))
        if res.rowcount == 1:
            db.commit()
            return {"status": "ok", "summary": summary}
        db.rollback()   # the user edited the run while we were merging: go round again with their changes

    return {"status": "deferred", "reason": "run kept changing; will retry next time"}


# ------------------------------------------------------------------ what is due
def due_target_ids(db: Session, now_utc: datetime.datetime, now_local: datetime.datetime,
                   nightly_hour: int, max_age_hours: int) -> List[str]:
    """Targets that need a scheduled sync. Time-based so it also catches up after the app was off:
    stale + past the nightly hour, or very stale (2x max age) at any hour, or never synced."""
    out = []
    for t in db.query(SyncTarget).filter(SyncTarget.enabled == True).all():  # noqa: E712
        if t.lease_until and t.lease_until > now_utc:
            continue
        if t.backoff_until and t.backoff_until > now_utc:
            continue
        if t.last_synced_at is None:
            out.append(str(t.id))
            continue
        age = now_utc - t.last_synced_at
        if age >= datetime.timedelta(hours=max_age_hours) and now_local.hour >= nightly_hour:
            out.append(str(t.id))
        elif age >= datetime.timedelta(hours=2 * max_age_hours):
            out.append(str(t.id))
    return out


# ------------------------------------------------------------------ worker
class SyncWorker:
    """One background thread runs sync jobs one at a time (which also keeps token refreshes orderly);
    a second ticks every minute to queue whatever is due."""

    def __init__(self, runner: Callable[..., Dict[str, Any]] = sync_target, clock: Callable[[], float] = time.time,
                 min_gap_seconds: Optional[int] = None):
        self._runner, self._clock = runner, clock
        self._gap = settings.SYNC_WEBHOOK_MIN_GAP_SECONDS if min_gap_seconds is None else min_gap_seconds
        self._pending: Dict[str, Tuple[float, str]] = {}   # target id -> (not before, trigger)
        self._last_done: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    # -- queue
    def enqueue(self, target_id: str, trigger: str) -> None:
        """Queue a sync. Repeated requests for the same target collapse into one; webhook bursts are spaced
        out by the minimum gap so a flurry of events costs one fetch, not fifty."""
        now = self._clock()
        with self._lock:
            if target_id in self._pending:
                return
            due = now
            if trigger == "webhook":
                due = max(now + 1, self._last_done.get(target_id, 0) + self._gap)
            self._pending[target_id] = (due, trigger)

    def pending(self) -> Dict[str, Tuple[float, str]]:
        with self._lock:
            return dict(self._pending)

    def run_due(self) -> int:
        """Run every job whose time has come (called by the loop; also directly by tests)."""
        now = self._clock()
        with self._lock:
            ready = [(tid, trig) for tid, (due, trig) in self._pending.items() if due <= now]
            for tid, _ in ready:
                del self._pending[tid]
        for tid, trig in ready:
            try:
                self._runner(tid, trig)
            except Exception:  # noqa: BLE001 - the runner already records failures; never kill the thread
                logger.exception("Sync job crashed")
            self._last_done[tid] = self._clock()
        return len(ready)

    # -- scheduling
    def tick(self) -> int:
        """Queue targets that are due for their scheduled sync."""
        with Session(connection.engine) as db:
            ids = due_target_ids(db, datetime.datetime.utcnow(), datetime.datetime.now(),
                                 settings.SYNC_NIGHTLY_HOUR, settings.SYNC_MAX_AGE_HOURS)
        for tid in ids:
            self.enqueue(tid, "nightly")
        return len(ids)

    # -- threads
    def start(self) -> None:
        if self._threads or not settings.SYNC_ENABLED:
            return
        self._stop.clear()

        def work():
            while not self._stop.is_set():
                self.run_due()
                self._stop.wait(1.0)

        def schedule():
            self._stop.wait(20)    # let the app finish starting
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    logger.exception("Sync scheduler tick failed")
                self._stop.wait(60)

        self._threads = [threading.Thread(target=work, name="sync-worker", daemon=True),
                         threading.Thread(target=schedule, name="sync-scheduler", daemon=True)]
        for th in self._threads:
            th.start()
        logger.info("Auto-sync worker started (nightly hour %s, max age %sh)", settings.SYNC_NIGHTLY_HOUR, settings.SYNC_MAX_AGE_HOURS)

    def stop(self) -> None:
        self._stop.set()
        for th in self._threads:
            th.join(timeout=3)
        self._threads = []

    @property
    def running(self) -> bool:
        return bool(self._threads) and all(th.is_alive() for th in self._threads)


worker = SyncWorker()
