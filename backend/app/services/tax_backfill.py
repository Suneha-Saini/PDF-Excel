"""Backfill tax codes onto lines that were stored before tax capture existed.

Older runs hold ledger lines without a `tax_code`, so the tax-based checks (Setup issues, the BAS estimate) cannot use them.
This re-reads the same transactions from the platform (READ-ONLY) and adds the tax code to the stored lines, matched by
their ids. It only ever ADDS a missing `tax_code` key: suggestions, statuses, approvals and decisions are never touched, and
`updated_at` is left alone so run history does not reorder.
"""
import datetime
import logging
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session

from app.database.models import LedgerRun
from app.services.providers.base import LedgerProvider

logger = logging.getLogger("app.services.tax_backfill")

BUSY = ("fetching", "categorising", "applying")
MAX_SPAN_DAYS = 800          # one backfill never reads more than ~2 years: keeps API use bounded


def _runs(db: Session, user_id, provider: str, client_id: str, client_name: Optional[str]) -> List[LedgerRun]:
    same = LedgerRun.client_id == client_id
    if client_name:   # runs saved before client tracking are matched by name
        same = or_(same, (LedgerRun.client_id.is_(None)) & (LedgerRun.organization == client_name))
    return (db.query(LedgerRun).filter(LedgerRun.user_id == user_id, LedgerRun.provider == provider, same,
                                       LedgerRun.items.isnot(None)).all())


def needs_tax(item: Dict[str, Any]) -> bool:
    return "tax_code" not in item


def plan(runs: List[LedgerRun]) -> Dict[str, Any]:
    """What a backfill would have to read: how many lines lack tax and over which dates."""
    dates = sorted({i["date"] for r in runs for i in (r.items or []) if needs_tax(i) and i.get("date")})
    n = sum(1 for r in runs if r.status not in BUSY for i in (r.items or []) if needs_tax(i))
    return {"lines_missing": n, "date_from": dates[0] if dates else None, "date_to": dates[-1] if dates else None}


def backfill(db: Session, user_id, system: LedgerProvider, provider: str, client_id: str, client_name: Optional[str],
             progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Returns {"fetched", "lines_updated", "lines_not_found", "runs_updated", "runs_skipped_busy", "date_from", "date_to"}."""
    runs = _runs(db, user_id, provider, client_id, client_name)
    p = plan(runs)
    out = {"fetched": 0, "lines_updated": 0, "lines_not_found": 0, "runs_updated": 0,
           "runs_skipped_busy": sum(1 for r in runs if r.status in BUSY), "date_from": p["date_from"], "date_to": p["date_to"]}
    if not p["lines_missing"]:
        return out

    d_from, d_to = datetime.date.fromisoformat(p["date_from"]), datetime.date.fromisoformat(p["date_to"])
    if (d_to - d_from).days > MAX_SPAN_DAYS:
        d_from = d_to - datetime.timedelta(days=MAX_SPAN_DAYS)       # the most recent ~2 years; older lines stay as they are
        out["date_from"] = d_from.isoformat()
    fresh, _ = system.fetch_ledger(client_id, d_from, d_to, progress)    # read-only
    tax_by_id = {i["id"]: i.get("tax_code") for i in fresh if "tax_code" in i}
    out["fetched"] = len(fresh)

    for run in runs:
        if run.status in BUSY:
            continue
        items, changed = run.items or [], 0
        for it in items:
            if not needs_tax(it):
                continue
            if it["id"] in tax_by_id:
                it["tax_code"] = tax_by_id[it["id"]]      # None is a real value: the line has no tax
                changed += 1
            elif d_from.isoformat() <= (it.get("date") or "") <= d_to.isoformat():
                out["lines_not_found"] += 1               # in range but no longer on the platform (deleted / not AUTHORISED)
        if changed:
            run.items = items
            flag_modified(run, "items")                   # the list was edited in place; updated_at is deliberately NOT touched
            db.add(run)
            out["runs_updated"] += 1
            out["lines_updated"] += changed
    db.commit()
    logger.info("Tax backfill for %s/%s: %s", provider, client_name or client_id, out)
    return out
