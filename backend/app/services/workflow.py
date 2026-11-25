"""Client workflow: one review of one client for one BAS period.

    Files -> Coding check -> GL review -> BAS review -> Sign-off

Stage results are COMPUTED from the data each time; only what a person decides is stored (checklist ticks, BAS figures
typed from the activity statement, finding resolutions, assignment, sign-off). Nothing here lodges or changes anything.

Scope notes, deliberately explicit:
  * Periods and due dates follow the standard Australian BAS calendar. Tax-agent lodgment dates are later, so the due
    date is editable.
  * The BAS estimate is built from BANK transactions, so it is only meaningful for CASH-basis GST clients. An accruals
    client's BAS needs invoices and bills, which this ledger does not contain; that stage says so instead of guessing.
"""
import calendar
import datetime
import hashlib
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_
from sqlmodel import Session

from app.database.models import ClientWorkflow, HistoryImport, LedgerRun, WorkflowFinding
from app.services.coding import setup_issues as si
from app.services.coding.store import load_history

FREQUENCIES = ("monthly", "quarterly", "annual")
CHECKLIST = {
    "bas_summary": "BAS figures (Activity Statement / BAS summary)",
    "profit_and_loss": "Profit and Loss",
    "balance_sheet": "Balance Sheet",
}
BAS_FIGURES = ("G1", "1A", "1B")
TOLERANCE_MEDIUM = 0.02        # estimate differs from the BAS figure by more than 2%  -> Medium
TOLERANCE_HIGH = 0.10          # ... by more than 10%                                    -> High
_SEV_ORDER = {"High": 0, "Medium": 1, "Info": 2}


# ------------------------------------------------------------------ periods and due dates
def _last_day(y: int, m: int) -> int:
    return calendar.monthrange(y, m)[1]


def bounds(freq: str, anchor: datetime.date) -> Tuple[datetime.date, datetime.date]:
    """The BAS period containing `anchor`. Quarters are the Australian financial quarters (Jul-Sep, Oct-Dec, Jan-Mar,
    Apr-Jun); annual is the financial year (1 Jul - 30 Jun)."""
    if freq == "monthly":
        return datetime.date(anchor.year, anchor.month, 1), datetime.date(anchor.year, anchor.month, _last_day(anchor.year, anchor.month))
    if freq == "quarterly":
        first = anchor.month - ((anchor.month - 1) % 3)               # 1,4,7,10
        last = first + 2
        return datetime.date(anchor.year, first, 1), datetime.date(anchor.year, last, _last_day(anchor.year, last))
    if freq == "annual":
        y = anchor.year if anchor.month >= 7 else anchor.year - 1
        return datetime.date(y, 7, 1), datetime.date(y + 1, 6, 30)
    raise ValueError("BAS frequency must be monthly, quarterly or annual.")


def default_period(freq: str, today: datetime.date) -> Tuple[datetime.date, datetime.date]:
    """The most recent period that has ended (what is being reviewed now)."""
    start, end = bounds(freq, today)
    return bounds(freq, start - datetime.timedelta(days=1)) if end >= today else (start, end)


def due_date(freq: str, end: datetime.date) -> datetime.date:
    """Standard ATO due dates: monthly 21st of the next month; quarterly 28th of the next month (the Oct-Dec quarter is
    due 28 Feb); annual 31 October. A tax agent's lodgment program is later: edit the date."""
    nxt_y, nxt_m = (end.year + 1, 1) if end.month == 12 else (end.year, end.month + 1)
    if freq == "monthly":
        return datetime.date(nxt_y, nxt_m, 21)
    if freq == "quarterly":
        return datetime.date(end.year + 1, 2, 28) if end.month == 12 else datetime.date(nxt_y, nxt_m, 28)
    return datetime.date(end.year, 10, 31)


def label(freq: str, start: datetime.date, end: datetime.date) -> str:
    if freq == "monthly":
        return start.strftime("%b %Y")
    if freq == "quarterly":
        return f"{start.strftime('%b')}-{end.strftime('%b')} {end.year}"
    return f"FY {start.year}-{str(end.year)[2:]}"


# ------------------------------------------------------------------ data in the period
def _in_period(lines: List[Dict[str, Any]], start: str, end: str) -> List[Dict[str, Any]]:
    return [i for i in lines if start <= (i.get("date") or "") <= end]


def _client_runs(db: Session, wf: ClientWorkflow):
    same = LedgerRun.client_id == wf.client_id
    if wf.client_name:
        same = or_(same, (LedgerRun.client_id.is_(None)) & (LedgerRun.organization == wf.client_name))
    return db.query(LedgerRun).filter(LedgerRun.user_id == wf.user_id, LedgerRun.provider == wf.provider, same,
                                      LedgerRun.items.isnot(None))


def ledger_coverage(db: Session, wf: ClientWorkflow) -> Dict[str, Any]:
    """How much of the period has been fetched (live runs) or imported. A run covers its whole date window."""
    start, end = datetime.date.fromisoformat(wf.period_start), datetime.date.fromisoformat(wf.period_end)
    windows = [(r.date_from, r.date_to) for r in _client_runs(db, wf).all() if r.items]
    windows += [(i.date_from, i.date_to) for i in db.query(HistoryImport).filter(
        HistoryImport.user_id == wf.user_id, HistoryImport.provider == wf.provider, HistoryImport.client_id == wf.client_id).all()
        if i.date_from and i.date_to]
    total = (end - start).days + 1
    gaps: List[Tuple[str, str]] = []
    covered = 0
    run_start: Optional[datetime.date] = None
    for k in range(total):
        d = (start + datetime.timedelta(days=k)).isoformat()
        hit = any(a <= d <= b for a, b in windows)
        covered += hit
        if not hit and run_start is None:
            run_start = start + datetime.timedelta(days=k)
        if hit and run_start is not None:
            gaps.append((run_start.isoformat(), (start + datetime.timedelta(days=k - 1)).isoformat()))
            run_start = None
    if run_start is not None:
        gaps.append((run_start.isoformat(), end.isoformat()))
    return {"covered_days": covered, "total_days": total, "complete": covered == total, "gaps": gaps}


def best_run(db: Session, wf: ClientWorkflow) -> Optional[LedgerRun]:
    """The run that overlaps the period most (ties: the most recently updated)."""
    best, best_key = None, (-1, "")
    for r in _client_runs(db, wf).all():
        lo, hi = max(r.date_from, wf.period_start), min(r.date_to, wf.period_end)
        overlap = (datetime.date.fromisoformat(hi) - datetime.date.fromisoformat(lo)).days + 1 if lo <= hi else 0
        key = (overlap, r.updated_at.isoformat())
        if overlap > 0 and key > best_key:
            best, best_key = r, key
    return best


# ------------------------------------------------------------------ BAS estimate
def bas_estimate(lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """GST figures built from the period's bank transactions at 10% (GST = gross / 11). An ESTIMATE: it ignores anything
    that never touches the bank (journals, invoices not yet paid), so a cash-basis BAS should be close, not identical."""
    g1 = g3 = g10 = g11 = sales_gst_base = purch_gst_base = 0.0
    with_tax = with_gst_codes = 0
    for it in lines:
        tax = si.tax_of(it)
        if tax is None:
            continue
        with_tax += 1
        low = tax.lower()
        side = si.tax_side(tax)
        if side is not None:
            with_gst_codes += 1        # the line carries a sales- or purchases-side GST code (even a GST-free one)
        if side == "income" and "excluded" not in low:
            g1 += it["amount"]                                       # money in is positive, a refund reduces it
            if si.gst_bearing(tax):
                sales_gst_base += it["amount"]
            elif "input taxed" not in low and "exempt" not in low:
                g3 += it["amount"]                                   # GST-free sales
        elif side == "expense" and "excluded" not in low:
            out = -it["amount"]                                      # money out is negative
            if "capital" in low:
                g10 += out
            else:
                g11 += out
            if si.gst_bearing(tax):
                purch_gst_base += out
    r2 = lambda v: round(v, 2)
    return {"G1": r2(g1), "G3": r2(g3), "G10": r2(g10), "G11": r2(g11), "1A": r2(sales_gst_base / 11), "1B": r2(purch_gst_base / 11),
            "lines": len(lines), "lines_with_tax": with_tax, "lines_with_gst_codes": with_gst_codes,
            "basis": "bank transactions only, GST taken as 1/11 of the gross amount"}


def compare_figures(estimate: Dict[str, Any], entered: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Findings where the estimate and the figure typed from the activity statement disagree."""
    out = []
    for fig in BAS_FIGURES:
        try:
            actual = float(entered[fig])
        except (KeyError, TypeError, ValueError):
            continue
        est = estimate[fig]
        diff = round(est - actual, 2)
        rel = abs(diff) / max(abs(actual), 1.0)
        if rel <= TOLERANCE_MEDIUM:
            continue
        sev = "High" if rel > TOLERANCE_HIGH else "Medium"
        out.append({"severity": sev, "kind": "bas_figure_differs", "key_parts": [fig],
                    "text": f"{fig}: the ledger gives {est:,.2f} but the BAS shows {actual:,.2f} (difference {diff:+,.2f}, {rel * 100:.0f}%)",
                    "detail": {"figure": fig, "estimate": est, "bas": actual, "difference": diff}})
    return out


# ------------------------------------------------------------------ findings
def finding_key(stage: str, kind: str, *parts: Any) -> str:
    return hashlib.sha1("|".join([stage, kind, *[str(p).lower() for p in parts]]).encode()).hexdigest()[:16]


def store_findings(db: Session, wf: ClientWorkflow, stage: str, produced: List[Dict[str, Any]]) -> Dict[str, int]:
    """Upsert this check's findings. Statuses a person set survive; findings no longer produced become 'fixed'; a fixed
    finding that comes back reopens. Caller commits."""
    existing = {f.key: f for f in db.query(WorkflowFinding).filter(WorkflowFinding.workflow_id == wf.id, WorkflowFinding.stage == stage).all()}
    now = datetime.datetime.utcnow()
    new = reopened = fixed = 0
    seen = set()
    for p in produced:
        key = finding_key(stage, p["kind"], *p["key_parts"])
        seen.add(key)
        f = existing.get(key)
        if f is None:
            db.add(WorkflowFinding(workflow_id=wf.id, key=key, stage=stage, severity=p["severity"], kind=p["kind"], text=p["text"],
                                   detail=p.get("detail"), status="open"))
            new += 1
            continue
        f.severity, f.text, f.detail, f.seen, f.updated_at = p["severity"], p["text"], p.get("detail"), True, now
        if f.status == "fixed":
            f.status, f.resolved_at = "open", None
            reopened += 1
        db.add(f)
    for key, f in existing.items():
        if key not in seen:
            f.seen, f.updated_at = False, now
            if f.status == "open":
                f.status, f.resolved_at = "fixed", now
                fixed += 1
            db.add(f)
    return {"new": new, "reopened": reopened, "fixed": fixed}


def findings_of(db: Session, wf: ClientWorkflow) -> List[WorkflowFinding]:
    rows = db.query(WorkflowFinding).filter(WorkflowFinding.workflow_id == wf.id).all()
    return sorted(rows, key=lambda f: (f.status != "open", _SEV_ORDER.get(f.severity, 9), f.stage, f.text))


def _open(findings: List[WorkflowFinding], stage: Optional[str] = None, severity: Optional[str] = None) -> List[WorkflowFinding]:
    return [f for f in findings if f.status == "open" and (stage is None or f.stage == stage) and (severity is None or f.severity == severity)]


# ------------------------------------------------------------------ running the reviews
def run_gl_review(db: Session, wf: ClientWorkflow, accounts: List[Dict[str, Any]], settings: Dict[str, Any]) -> Dict[str, Any]:
    """GL review: the ledger-setup checks, limited to this period's lines and the client's settings."""
    pool = load_history(db, wf.user_id, wf.provider, wf.client_id, wf.client_name)
    lines = _in_period(pool, wf.period_start, wf.period_end)
    result = si.analyse(lines, accounts, settings)
    produced = []
    for i in result["issues"]:
        produced.append({"severity": i["severity"], "kind": i["kind"],
                         "key_parts": [i["payee"], i["direction"], i["account"], i["tax"]],
                         "text": f"{i['issue']} ({i['payee']}, {i['account']} {i['account_name']}, {i['times']}x)",
                         "detail": {k: i[k] for k in ("payee", "direction", "account", "account_name", "tax", "times", "line_ids")}})
    counts = store_findings(db, wf, "gl", produced)
    wf.checks = {**(wf.checks or {}), "gl": {"ran_at": datetime.datetime.utcnow().isoformat() + "Z", "lines": len(lines),
                                              "lines_with_tax": result["coverage"]["lines_with_tax_info"]}}
    return {"lines": len(lines), **counts, "coverage": result["coverage"]}


def run_bas_review(db: Session, wf: ClientWorkflow, settings: Dict[str, Any]) -> Dict[str, Any]:
    """BAS review: estimate the GST figures from the ledger and reconcile them to the figures typed from the BAS."""
    pool = load_history(db, wf.user_id, wf.provider, wf.client_id, wf.client_name)
    lines = _in_period(pool, wf.period_start, wf.period_end)
    est = bas_estimate(lines)
    produced: List[Dict[str, Any]] = []
    if lines and est["lines_with_tax"] < len(lines):
        miss = len(lines) - est["lines_with_tax"]
        produced.append({"severity": "Medium", "kind": "bas_tax_coverage", "key_parts": ["coverage"],
                         "text": f"{miss} of {len(lines)} lines in the period have no tax information, so the estimate is incomplete",
                         "detail": {"lines": len(lines), "without_tax": miss}})
    produced += compare_figures(est, wf.bas_figures or {})
    counts = store_findings(db, wf, "bas", produced)
    wf.checks = {**(wf.checks or {}), "bas": {"ran_at": datetime.datetime.utcnow().isoformat() + "Z", "estimate": est}}
    return {"estimate": est, **counts}


# ------------------------------------------------------------------ stage status
def stages(db: Session, wf: ClientWorkflow, settings: Dict[str, Any], today: Optional[datetime.date] = None) -> List[Dict[str, Any]]:
    """The five stages with a status (done / needs_you / waiting / not_run / na) and a one-line summary."""
    findings = findings_of(db, wf)
    reg = settings.get("gst_registration", "unknown")
    out: List[Dict[str, Any]] = []

    # 1. Files
    cov = ledger_coverage(db, wf)
    ticks = wf.checklist or {}
    # A client that is not registered for GST has no BAS figures to supply.
    required = [k for k in CHECKLIST if not (k == "bas_summary" and reg == "not_registered")]
    missing = [CHECKLIST[k] for k in required if not ticks.get(k)]
    problems = []
    if not cov["complete"]:
        problems.append(f"ledger covers {cov['covered_days']} of {cov['total_days']} days (gaps: " + ", ".join(f"{a} to {b}" for a, b in cov["gaps"][:3]) + ")")
    if missing:
        problems.append("add: " + ", ".join(missing))
    out.append({"key": "files", "label": "Files", "status": "done" if not problems else "needs_you",
                "summary": "Ledger for the whole period and the pack are in" if not problems else "; ".join(problems), "detail": cov})

    # 2. Coding check
    run = best_run(db, wf)
    if run is None:
        out.append({"key": "coding", "label": "Coding check", "status": "needs_you", "summary": "No ledger has been fetched for this period yet", "detail": None})
    else:
        its = _in_period(run.items or [], wf.period_start, wf.period_end)
        un, rv = sum(1 for i in its if i["status"] == "unassigned"), sum(1 for i in its if i["status"] == "review")
        ok = bool(its) and not un and not rv
        out.append({"key": "coding", "label": "Coding check", "status": "done" if ok else "needs_you",
                    "summary": (f"All {len(its)} lines coded and reviewed" if ok else
                                "No lines in the period" if not its else f"{un} uncoded and {rv} awaiting review, of {len(its)} lines"),
                    "detail": {"run_id": str(run.id), "lines": len(its), "unassigned": un, "review": rv}})

    # 3. GL review
    gl = (wf.checks or {}).get("gl")
    if not gl:
        out.append({"key": "gl", "label": "GL review", "status": "not_run", "summary": "Not run yet", "detail": None})
    elif gl.get("lines", 0) == 0:
        # A green rating over nothing would be false assurance.
        out.append({"key": "gl", "label": "GL review", "status": "needs_you", "detail": gl,
                    "summary": "No coded lines in this period, so there was nothing to review. Fetch the ledger for the period or import its history, then run again"})
    else:
        high, med = _open(findings, "gl", "High"), _open(findings, "gl", "Medium")
        rating = "RED" if high else "AMBER" if med else "GREEN"
        out.append({"key": "gl", "label": "GL review", "status": "done",
                    "summary": f"{rating}" + (f" - {len(high)} High item(s)" if high else "") + (f", {len(med)} Medium" if med else "") +
                               f" ({gl['lines']} lines checked)", "rating": rating, "detail": gl})

    # 4. BAS review
    bas = (wf.checks or {}).get("bas")
    if reg == "not_registered":
        out.append({"key": "bas", "label": "BAS review", "status": "na", "summary": "Not registered for GST: no GST BAS to review", "detail": None})
    elif reg == "accrual":
        out.append({"key": "bas", "label": "BAS review", "status": "na",
                    "summary": "Accruals-basis BAS needs invoices and bills, which this bank-transaction ledger does not contain: review it against the accounting system's own GST report", "detail": None})
    elif reg != "cash":
        out.append({"key": "bas", "label": "BAS review", "status": "needs_you", "summary": "Set GST registration in Client details (Setup tab) first", "detail": None})
    elif not bas:
        out.append({"key": "bas", "label": "BAS review", "status": "not_run", "summary": "Ready to run: estimate the GST figures from the ledger", "detail": None})
    else:
        have = [f for f in BAS_FIGURES if (wf.bas_figures or {}).get(f) not in (None, "")]
        open_ = _open(findings, "bas")
        est = bas["estimate"]
        if est["lines"] == 0:
            st, msg = "needs_you", "No lines in this period, so there is nothing to estimate"
        elif est["lines_with_tax"] == 0:
            st, msg = "needs_you", ("None of the period's lines carries tax information, so no estimate is possible. Fetch the ledger again "
                                    "(tax codes are captured on new fetches) or import an export with a tax column")
        elif est.get("lines_with_gst_codes", 1) == 0:
            st, msg = "needs_you", (f"All {est['lines']} lines carry tax information but none has a GST code (they are 'No GST' / untaxed), so the estimate is zero. "
                                    "If this client is registered for GST, the transactions in the accounting system are missing their GST codes")
        elif not have:
            st, msg = "needs_you", "Estimate ready. Enter G1, 1A and 1B from the BAS to reconcile"
        elif open_:
            st, msg = "needs_you", f"{len(open_)} open finding(s)"
        else:
            st, msg = "done", "Ledger agrees with the BAS figures entered"
        out.append({"key": "bas", "label": "BAS review", "status": st, "summary": msg, "detail": bas["estimate"]})

    # 5. Sign-off
    blockers = signoff_blockers(out, findings)
    if wf.status == "signed_off":
        out.append({"key": "signoff", "label": "Sign-off", "status": "done",
                    "summary": f"Signed off by {wf.signed_off_by or 'unknown'} on {wf.signed_off_at.date() if wf.signed_off_at else ''}", "detail": None})
    elif blockers:
        out.append({"key": "signoff", "label": "Sign-off", "status": "waiting", "summary": "After the review: " + "; ".join(blockers[:2]), "detail": blockers})
    else:
        out.append({"key": "signoff", "label": "Sign-off", "status": "needs_you", "summary": "Ready to sign off", "detail": []})
    return out


def signoff_blockers(stage_list: List[Dict[str, Any]], findings: List[WorkflowFinding]) -> List[str]:
    """Why this review cannot be signed off yet (empty = it can)."""
    by = {s["key"]: s for s in stage_list}
    why = []
    if by["files"]["status"] != "done":
        why.append("files are incomplete")
    if by["coding"]["status"] != "done":
        why.append("coding is not finished")
    if by["gl"]["status"] == "not_run":
        why.append("the GL review has not been run")
    elif by["gl"]["status"] == "needs_you":
        why.append("the GL review had no lines to check")
    if _open(findings, severity="High"):
        why.append(f"{len(_open(findings, severity='High'))} High finding(s) are still open (resolve or accept each with a note)")
    if by["bas"]["status"] in ("not_run",):
        why.append("the BAS review has not been run")
    elif by["bas"]["status"] == "needs_you":
        why.append("the BAS review needs your input")
    return why
