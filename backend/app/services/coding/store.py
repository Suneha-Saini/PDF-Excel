"""Where history comes from: every ledger line the user has already fetched for this client."""
from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, or_
from sqlmodel import Session

from app.database.models import HistoryImport, LedgerRun
from app.services.coding.text import contact_key, item_tokens, jaccard


def client_name_for(db: Session, user_id, provider: str, client_id: str) -> Optional[str]:
    """The display name this client was saved under, so runs saved before client tracking (matched by name) still count."""
    row = (db.query(LedgerRun.organization).filter(LedgerRun.user_id == user_id, LedgerRun.provider == provider,
                                                   LedgerRun.client_id == client_id, LedgerRun.organization.isnot(None)).first())
    return row[0] if row else None


def load_history(db: Session, user_id, provider: str, client_id: Optional[str], client_name: Optional[str] = None,
                 exclude_run_id=None, max_runs: int = 50) -> List[Dict[str, Any]]:
    """Coded lines from this client's earlier runs, newest run first so an up-to-date line wins over an old copy.

    Runs saved before client tracking have no client_id; they are matched by organisation name instead."""
    same_client = LedgerRun.client_id == client_id
    if client_name:
        same_client = or_(same_client, and_(LedgerRun.client_id.is_(None), LedgerRun.organization == client_name))
    q = db.query(LedgerRun).filter(LedgerRun.user_id == user_id, LedgerRun.provider == provider, same_client,
                                   LedgerRun.items.isnot(None))
    if exclude_run_id is not None:
        q = q.filter(LedgerRun.id != exclude_run_id)
    seen: Dict[str, Dict[str, Any]] = {}
    for run in q.order_by(LedgerRun.updated_at.desc()).limit(max_runs).all():
        for it in run.items or []:
            if it.get("current_code") and it["id"] not in seen:
                seen[it["id"]] = it

    # Lines the user imported from a ledger export (12+ months): the bulk of the evidence for most clients.
    # Live lines win over imported ones; overlapping imports collapse because their ids are content hashes.
    if client_id:
        live: Dict[tuple, list] = defaultdict(list)           # (date, amount) of every live line -> who it was with
        for it in seen.values():
            live[(it.get("date"), round(it["amount"], 2))].append((item_tokens(it), contact_key(it)))

        for imp in db.query(HistoryImport).filter(HistoryImport.user_id == user_id, HistoryImport.provider == provider,
                                                  HistoryImport.client_id == client_id).all():
            for it in imp.items or []:
                if it["id"] in seen:
                    continue
                # The same real transaction can be in both a live run and an export: count it once (the live copy).
                toks, ck = item_tokens(it), contact_key(it)
                if any((ck and ck == c) or jaccard(toks, t) >= 0.6 for t, c in live.get((it.get("date"), round(it["amount"], 2)), ())):
                    continue
                seen[it["id"]] = it
    return list(seen.values())
