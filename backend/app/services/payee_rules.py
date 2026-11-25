"""Remembered payee rules: load them for the pipeline, propose them from history, and create them from a reviewer's work.

A rule is created only by a person's action: ticking suggested payees, pressing Remember on a line they coded, or
'Remember reviewed lines' for lines they decided. History alone never creates a rule silently."""
import datetime
import uuid
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlmodel import Session

from app.database.models import PayeeRule
from app.services.coding.setup_issues import canon_tax
from app.services.coding.text import payee_key, payee_matches


def rule_dicts(db: Session, user_id, provider: str, client_id: Optional[str]) -> List[Dict[str, Any]]:
    """Active rules for this client in the shape the pipeline wants."""
    if not client_id:
        return []
    rows = db.query(PayeeRule).filter(PayeeRule.user_id == user_id, PayeeRule.provider == provider,
                                      PayeeRule.client_id == client_id, PayeeRule.active == True).all()  # noqa: E712
    return [{"id": r.id, "payee_key": r.payee_key, "payee_display": r.payee_display, "direction": r.direction,
             "account_code": r.account_code, "active": True} for r in rows]


def note_override(db: Session, rule_id: Optional[int]) -> None:
    """A person changed a line that a payee rule had coded: the rule may be stale."""
    if not rule_id:
        return
    r = db.get(PayeeRule, rule_id)
    if r:
        r.overrides += 1
        r.updated_at = datetime.datetime.utcnow()
        db.add(r)


def _direction(it: Dict[str, Any]) -> str:
    return "spend" if it["amount"] < 0 else "receive"


def _tax(it: Dict[str, Any]) -> Optional[str]:
    return canon_tax(it["tax_code"]) if "tax_code" in it else canon_tax(it.get("tax_label"))


# ------------------------------------------------------------------ what history suggests
def candidates(history: List[Dict[str, Any]], names: Dict[str, str], existing: Iterable[Tuple[str, str]],
               min_lines: int = 3, min_share: float = 0.9, limit: int = 300) -> List[Dict[str, Any]]:
    """Payees whose past lines were (almost) always coded to one account, and that have no rule yet.
    The user ticks which to keep: nothing is created from this list automatically."""
    have = set(existing)
    groups: Dict[tuple, Dict[str, Any]] = {}
    for it in history:
        code = it.get("current_code")
        key, display = payee_key(it)
        if not key or not code:
            continue
        g = groups.setdefault((key, _direction(it)), {"codes": Counter(), "display": Counter(), "last": "", "tax": Counter()})
        g["codes"][str(code)] += 1
        g["display"][display] += 1
        g["last"] = max(g["last"], it.get("date") or "")
        t = _tax(it)
        if t:
            g["tax"][t] += 1

    out = []
    for (key, direction), g in groups.items():
        total = sum(g["codes"].values())
        code, n = g["codes"].most_common(1)[0]
        if total < min_lines or n / total < min_share or code not in names or (key, direction) in have:
            continue
        out.append({"payee_key": key, "payee_display": g["display"].most_common(1)[0][0], "direction": direction,
                    "account_code": code, "account_name": names[code], "lines": total, "agree_pct": round(100 * n / total, 1),
                    "last_seen": g["last"] or None, "tax_label": g["tax"].most_common(1)[0][0] if g["tax"] else None})
    out.sort(key=lambda c: (-c["lines"], c["payee_display"].lower()))
    return out[:limit]


# ------------------------------------------------------------------ what a reviewer decided
def remembered_entries(items: List[Dict[str, Any]], names: Dict[str, str], only_ids: Optional[set] = None) -> Dict[str, Any]:
    """Rules from lines a PERSON decided (typed a code, or approved a suggestion). A payee a person coded to two
    different accounts in the same run is skipped as ambiguous rather than guessed."""
    found: Dict[tuple, Dict[str, Any]] = {}
    clashes = set()
    skipped = Counter()
    for it in items:
        if only_ids is not None and it["id"] not in only_ids:
            continue
        human = it.get("source") == "manual" or it.get("approved")
        if it["status"] == "locked" or not human or not it.get("proposed_code"):
            skipped["not a person's decision"] += 1
            continue
        code = str(it["proposed_code"])
        key, display = payee_key(it)
        if not key:
            skipped["no payee on the line"] += 1
            continue
        if code not in names:
            skipped["account not in the chart"] += 1
            continue
        k = (key, _direction(it))
        if k in found and found[k]["account_code"] != code:
            clashes.add(k)
        found[k] = {"payee_key": key, "payee_display": display, "direction": k[1], "account_code": code,
                    "account_name": names[code], "tax_label": _tax(it)}
    for k in clashes:
        found.pop(k, None)
    if clashes:
        skipped["payee coded to two accounts"] += len(clashes)
    return {"entries": list(found.values()), "skipped": dict(skipped)}


# ------------------------------------------------------------------ writing
def create_rules(db: Session, user_id, provider: str, client_id: str, entries: List[Dict[str, Any]], source: str,
                 abns: Optional[Dict[str, str]] = None, update_existing: bool = False) -> Dict[str, Any]:
    """Create rules as ONE batch (one Undo). Existing rule for the same payee + direction is left alone, or, for a
    reviewer's correction (update_existing), re-pointed at the new account. Caller commits."""
    batch = uuid.uuid4().hex
    created = updated = skipped = 0
    for e in entries:
        row = (db.query(PayeeRule).filter(PayeeRule.user_id == user_id, PayeeRule.provider == provider,
                                          PayeeRule.client_id == client_id, PayeeRule.payee_key == e["payee_key"],
                                          PayeeRule.direction == e["direction"]).first())
        if row is not None:
            if update_existing and (row.account_code != e["account_code"] or not row.active):
                row.account_code, row.account_name = e["account_code"], e.get("account_name")
                row.active, row.overrides, row.updated_at = True, 0, datetime.datetime.utcnow()
                db.add(row)
                updated += 1
            else:
                skipped += 1
            continue
        abn = (abns or {}).get((e.get("payee_display") or "").strip().lower())
        db.add(PayeeRule(user_id=user_id, provider=provider, client_id=client_id, payee_key=e["payee_key"],
                         payee_display=(e.get("payee_display") or "")[:120], direction=e["direction"],
                         account_code=e["account_code"], account_name=e.get("account_name"), tax_label=e.get("tax_label"),
                         abn=abn, source=source, batch_id=batch))
        created += 1
    return {"batch_id": batch if created else None, "created": created, "updated": updated, "skipped": skipped}


def abn_lookup(system, client_id: str) -> Dict[str, str]:
    """{supplier name (lower-case): ABN}, best effort. Never raises: an ABN is a nice-to-have on a rule."""
    try:
        return {s["name"].strip().lower(): "".join(str(s["abn"]).split()) for s in system.list_suppliers(client_id)
                if s.get("name") and s.get("abn")}
    except Exception:  # noqa: BLE001
        return {}


# ------------------------------------------------------------------ what the table shows
def usage(history: List[Dict[str, Any]], rules: List[PayeeRule]) -> Dict[int, Dict[str, Any]]:
    """How many history lines each rule matches (and agrees with), and when it was last seen. Computed, never stored."""
    by_dir: Dict[str, List[tuple]] = defaultdict(list)
    for it in history:
        key, _ = payee_key(it)
        if key and it.get("current_code"):
            by_dir[_direction(it)].append((key, str(it["current_code"]), it.get("date") or ""))
    out: Dict[int, Dict[str, Any]] = {}
    for r in rules:
        times = agree = 0
        last = ""
        for key, code, date in by_dir.get(r.direction, ()):
            if payee_matches(r.payee_key, key):
                times += 1
                agree += 1 if code == r.account_code else 0
                last = max(last, date)
        out[r.id] = {"times": times, "agree": agree, "last_seen": last or None}
    return out
