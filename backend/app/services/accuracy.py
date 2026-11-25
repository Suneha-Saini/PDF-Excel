"""Measured accuracy of the suggestions, from people's own decisions, plus the shadow-period gate for auto-approval.

Principles (taken from how the client's own review tool works):
  * The system never marks its own work. A suggestion is "checked" only when a person decided it individually
    (per-line confirm, or editing the line). Bulk 'Accept suggestions' and lines approved automatically are tracked
    but NOT counted as checks.
  * "Right" means the person kept the suggested account. It is agreement with a reviewer, not proof of correctness.
  * Auto-approval is earned per client: it only switches on after MIN_CHECKED individually checked HIGH history
    suggestions with at least MIN_ACCURACY right, and it switches itself off if auto-approved lines keep getting
    corrected (circuit breaker). Applying to the accounting system is still always a separate, explicit step.
"""
import datetime
from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlmodel import Session

from app.database.models import CodingPolicy, LedgerRun, SuggestionDecision

SUGGESTION_SOURCES = ("history", "ai", "rule", "payee")
COUNTED_HOW = ("line", "edit")          # individual decisions by a person
MIN_CHECKED = 15
MIN_ACCURACY = 0.95
# circuit breaker: look at the most recent auto-approved lines; if too many were corrected, pause auto-approval
BREAKER_WINDOW = 50
BREAKER_MIN_LINES = 10
BREAKER_MAX_CORRECTED = 0.05


def _name(accounts: Optional[List[Dict[str, Any]]], code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    return next((a["name"] for a in (accounts or []) if a["code"] == code), None)


def record_decision(db: Session, run: LedgerRun, item_before: Dict[str, Any], final_code: Optional[str], how: str) -> bool:
    """Record what a person did with the suggestion on `item_before` (a snapshot taken BEFORE the change).
    Only suggestions are recorded (history / AI / rule), never a person's own manual codes. One row per line per run:
    deciding again replaces the earlier decision. Caller commits. Returns True if a row was written."""
    source, suggested = item_before.get("source"), item_before.get("proposed_code")
    if source not in SUGGESTION_SOURCES or not suggested:
        return False

    same = final_code == suggested
    if item_before.get("auto_approved"):
        outcome = "auto" if same else "auto_corrected"
    else:
        outcome = "accepted" if same else "corrected"

    row = (db.query(SuggestionDecision)
           .filter(SuggestionDecision.run_id == run.id, SuggestionDecision.item_id == item_before["id"]).first())
    if row is None:
        row = SuggestionDecision(user_id=run.user_id, run_id=run.id, item_id=item_before["id"], provider=run.provider or "xero",
                                 client_id=run.client_id, client_name=run.organization, source=source,
                                 suggested_code=suggested, outcome=outcome, how=how)
    row.level = item_before.get("level") if source == "history" else None
    row.confidence = item_before.get("confidence") if source == "ai" else None
    row.suggested_code, row.suggested_name = suggested, _name(run.accounts, suggested)
    row.final_code, row.final_name = final_code, _name(run.accounts, final_code)
    row.outcome, row.how = outcome, how
    row.item_date, row.item_description = item_before.get("date"), (item_before.get("description") or "")[:200]
    row.item_contact, row.item_amount = item_before.get("contact"), item_before.get("amount")
    row.decided_at = datetime.datetime.utcnow()
    db.add(row)
    return True


def record_apply(db: Session, run: LedgerRun, applied_items: List[Dict[str, Any]]) -> int:
    """Lines written to the accounting system WITHOUT an individual decision (high-confidence history that was
    auto-approved, or AI above its threshold) are logged as 'auto': visible, but not counted as checked."""
    n = 0
    for it in applied_items:
        if it.get("source") not in ("history", "ai") or not it.get("proposed_code"):
            continue
        exists = (db.query(SuggestionDecision.id)
                  .filter(SuggestionDecision.run_id == run.id, SuggestionDecision.item_id == it["id"]).first())
        if exists:
            continue
        # No row yet means no person decided it, so it went through automatically.
        if record_decision(db, run, {**it, "auto_approved": True}, it["proposed_code"], "apply"):
            n += 1
    return n


# ------------------------------------------------------------------ policy
def _rows(db: Session, user_id, provider: Optional[str], client_id: Optional[str], since: Optional[datetime.datetime] = None):
    q = db.query(SuggestionDecision).filter(SuggestionDecision.user_id == user_id)
    if provider:
        q = q.filter(SuggestionDecision.provider == provider)
    if client_id:
        q = q.filter(SuggestionDecision.client_id == client_id)
    if since:
        q = q.filter(SuggestionDecision.decided_at >= since)
    return q.all()


def policy_status(db: Session, user_id, provider: str, client_id: Optional[str]) -> Dict[str, Any]:
    """Is auto-approval allowed for this client right now, and why / why not."""
    rows = _rows(db, user_id, provider, client_id) if client_id else []
    # Only a person's individual decision on a suggestion is a check: auto-approved lines (even ones later edited)
    # are tracked separately below and never count towards earning auto-approval.
    high = [r for r in rows if r.source == "history" and r.level == "high" and r.how in COUNTED_HOW
            and r.outcome in ("accepted", "corrected")]
    right = sum(1 for r in high if r.outcome == "accepted")
    accuracy = right / len(high) if high else None
    eligible = len(high) >= MIN_CHECKED and accuracy is not None and accuracy >= MIN_ACCURACY

    pol = (db.query(CodingPolicy).filter(CodingPolicy.user_id == user_id, CodingPolicy.provider == provider,
                                         CodingPolicy.client_id == client_id).first()) if client_id else None
    override = bool(pol and pol.auto_override)

    autos = sorted((r for r in rows if r.outcome in ("auto", "auto_corrected")), key=lambda r: r.decided_at, reverse=True)[:BREAKER_WINDOW]
    corrected = sum(1 for r in autos if r.outcome == "auto_corrected")
    suspended = len(autos) >= BREAKER_MIN_LINES and corrected / len(autos) > BREAKER_MAX_CORRECTED
    active = (eligible or override) and not suspended

    if suspended:
        why = f"Paused: {corrected} of the last {len(autos)} auto-approved lines were corrected by a person (limit {int(BREAKER_MAX_CORRECTED * 100)}%)."
    elif eligible:
        why = f"Earned: {right} of {len(high)} checked High suggestions were right ({round(100 * accuracy, 1)}%)."
    elif override:
        why = "Switched on manually, skipping the shadow period."
    else:
        why = (f"Shadow period: {len(high)} of {MIN_CHECKED} High suggestions checked"
               + (f", {round(100 * accuracy, 1)}% right (need {int(MIN_ACCURACY * 100)}%)." if accuracy is not None else "."))
    return {"active": active, "eligible": eligible, "override": override, "suspended": suspended, "why": why,
            "min_checked": MIN_CHECKED, "min_accuracy_pct": int(MIN_ACCURACY * 100),
            "high_checked": len(high), "high_right": right,
            "high_accuracy_pct": round(100 * accuracy, 1) if accuracy is not None else None,
            "auto_lines": len(autos), "auto_corrected": corrected}


def set_override(db: Session, user_id, provider: str, client_id: str, value: bool) -> None:
    pol = (db.query(CodingPolicy).filter(CodingPolicy.user_id == user_id, CodingPolicy.provider == provider,
                                         CodingPolicy.client_id == client_id).first())
    if pol is None:
        pol = CodingPolicy(user_id=user_id, provider=provider, client_id=client_id)
    pol.auto_override, pol.updated_at = value, datetime.datetime.utcnow()
    db.add(pol)


# ------------------------------------------------------------------ the dashboard
def _bucket(r: SuggestionDecision) -> str:
    if r.source == "history":
        return f"history · {r.level or 'unknown'}"
    if r.source == "ai":
        c = r.confidence or 0
        return "ai · high (80%+)" if c >= 0.8 else "ai · medium (60-80%)" if c >= 0.6 else "ai · low (<60%)"
    return "payee rule" if r.source == "payee" else "rule"


def _tally(rows: List[SuggestionDecision]) -> Dict[str, Any]:
    right = sum(1 for r in rows if r.outcome == "accepted")
    return {"checked": len(rows), "right": right, "accuracy_pct": round(100 * right / len(rows), 1) if rows else None}


def summary(db: Session, user_id, provider: Optional[str], client_id: Optional[str], days: int = 180) -> Dict[str, Any]:
    since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    rows = _rows(db, user_id, provider, client_id, since)
    counted = [r for r in rows if r.how in COUNTED_HOW and r.outcome in ("accepted", "corrected")]

    by_bucket: Dict[str, List[SuggestionDecision]] = defaultdict(list)
    by_source: Dict[str, List[SuggestionDecision]] = defaultdict(list)
    by_client: Dict[tuple, List[SuggestionDecision]] = defaultdict(list)
    by_month: Dict[str, List[SuggestionDecision]] = defaultdict(list)
    for r in counted:
        by_bucket[_bucket(r)].append(r)
        by_source[r.source].append(r)
        by_client[(r.provider, r.client_id, r.client_name)].append(r)
        by_month[r.decided_at.strftime("%Y-%m")].append(r)

    order = ["history · high", "history · medium", "history · low", "ai · high (80%+)", "ai · medium (60-80%)", "ai · low (<60%)", "payee rule", "rule"]
    wrong = sorted((r for r in counted if r.outcome == "corrected"), key=lambda r: r.decided_at, reverse=True)[:10]
    auto = [r for r in rows if r.outcome in ("auto", "auto_corrected")]
    return {
        "days": days,
        "totals": {**_tally(counted),
                   "bulk_accepted": sum(1 for r in rows if r.how == "bulk"),
                   "auto_approved": len(auto), "auto_corrected": sum(1 for r in auto if r.outcome == "auto_corrected")},
        "by_confidence": [{"bucket": b, **_tally(by_bucket[b])} for b in order if b in by_bucket],
        "by_source": {s: _tally(v) for s, v in by_source.items()},
        "by_client": sorted(({"provider": p, "client_id": c, "client_name": n, **_tally(v)} for (p, c, n), v in by_client.items()),
                            key=lambda x: -x["checked"]),
        "by_month": [{"month": m, **_tally(v)} for m, v in sorted(by_month.items())],
        "recent_corrections": [{"date": r.item_date, "description": r.item_description, "contact": r.item_contact,
                                "amount": r.item_amount, "source": r.source, "level": r.level,
                                "suggested": f"{r.suggested_code} {r.suggested_name or ''}".strip(),
                                "final": f"{r.final_code} {r.final_name or ''}".strip() if r.final_code else "(cleared)",
                                "client_name": r.client_name} for r in wrong],
        "note": "Counts only lines a person decided individually. Bulk accepts and automatically approved lines are not counted. "
                "'Right' means the reviewer kept the suggestion, which is agreement, not proof of correctness.",
    }
