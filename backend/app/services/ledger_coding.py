"""Ledger coding: Xero bank transactions -> categorised ledger lines -> integrity checks.

Each *line item* of a bank transaction is one ledger line ("item") that must end up in exactly one
account. Spend lines are negative, receive lines positive. Pipeline:

  build_items()  ->  apply_rules()  ->  ai_categorise()  ->  refresh()  ->  run_checks()

Only plain SPEND / RECEIVE transactions that are not reconciled can be recoded in Xero; everything
else is kept at its current code ("locked"/"unsupported") so totals still tie out.
"""
import datetime
import logging
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.ai.generic import complete_json
from app.services.coding import tax as tax_checks
from app.services.coding.decide import assess
from app.services.coding.history import HistoryIndex

logger = logging.getLogger("app.services.ledger_coding")

AI_BATCH = 30
MIN_CONFIDENCE = 0.6
EPS = 0.005


# ------------------------------------------------------------------ building items
def _date(tx: Dict[str, Any]) -> str:
    return str(tx.get("DateString") or tx.get("Date") or "")[:10]


def build_items(transactions: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return (items, tx_cache). tx_cache keeps what is needed to write changes back to Xero."""
    items: List[Dict[str, Any]] = []
    cache: Dict[str, Any] = {}

    for tx in transactions:
        tx_id = tx["BankTransactionID"]
        tx_type = tx.get("Type", "")
        sign = -1.0 if tx_type.startswith("SPEND") else 1.0
        contact = (tx.get("Contact") or {}).get("Name") or ""
        reconciled = bool(tx.get("IsReconciled"))
        recodable = tx_type in ("SPEND", "RECEIVE") and not reconciled

        cache[tx_id] = {
            "Type": tx_type,
            "Date": _date(tx),
            "Reference": tx.get("Reference"),
            "LineAmountTypes": tx.get("LineAmountTypes"),
            "ContactID": (tx.get("Contact") or {}).get("ContactID"),
            "BankAccountID": (tx.get("BankAccount") or {}).get("AccountID"),
            "BankAccountName": (tx.get("BankAccount") or {}).get("Name"),
            "Lines": [
                {
                    "LineItemID": li.get("LineItemID"),
                    "Description": li.get("Description"),
                    "Quantity": li.get("Quantity", 1),
                    "UnitAmount": li.get("UnitAmount"),
                    "AccountCode": li.get("AccountCode"),
                    "TaxType": li.get("TaxType"),
                }
                for li in tx.get("LineItems", [])
            ],
        }

        for idx, li in enumerate(tx.get("LineItems", [])):
            current = li.get("AccountCode")
            status, source, proposed = "unassigned", None, None
            if not recodable:
                status, source, proposed = "locked", "locked", current
            items.append({
                "id": f"{tx_id}:{idx}",
                "tx_id": tx_id,
                "line_index": idx,
                "date": _date(tx),
                "description": (li.get("Description") or tx.get("Reference") or "").strip(),
                "contact": contact,
                "reference": tx.get("Reference") or "",
                "type": tx_type,
                "direction": "spend" if sign < 0 else "receive",
                "amount": round(sign * float(li.get("LineAmount") or 0.0), 2),
                "current_code": current,
                "tax_code": li.get("TaxType"),
                "proposed_code": proposed,
                "source": source,
                "confidence": None,
                "reason": ("Reconciled in Xero" if reconciled else "Transfer/other type - not recodable")
                          if not recodable else "",
                "status": status,
                "bank_account": (tx.get("BankAccount") or {}).get("Name"),
            })
    return items, cache


# ------------------------------------------------------------------ rules
def _matches(rule: Dict[str, Any], item: Dict[str, Any]) -> bool:
    if rule["direction"] in ("spend", "receive") and rule["direction"] != item["direction"]:
        return False
    haystack = f"{item['description']} {item['contact']} {item['reference']}".lower()
    keywords = [k.strip().lower() for k in rule["keywords"].split(",") if k.strip()]
    return any(k in haystack for k in keywords)


def apply_rules(items: List[Dict[str, Any]], rules: List[Dict[str, Any]], overwrite: bool = False) -> int:
    """First matching active rule (by priority) wins. Manual edits are never overwritten."""
    ordered = sorted([r for r in rules if r.get("active", True)], key=lambda r: (r["priority"], r["id"]))
    applied = 0
    for it in items:
        if it["status"] == "locked" or it["source"] in ("manual", "payee"):   # a payee rule is more specific than a keyword
            continue
        if it["source"] == "rule" and not overwrite:
            continue
        for rule in ordered:
            if _matches(rule, it):
                it.update(proposed_code=rule["account_code"], source="rule", confidence=1.0, approved=False,
                          reason=f"Rule: {rule['name']}")
                applied += 1
                break
    return applied


# ------------------------------------------------------------------ payee rules
def apply_payee_rules(items: List[Dict[str, Any]], rules: List[Dict[str, Any]], accounts: List[Dict[str, Any]]) -> int:
    """First in the order: payee rules (a reviewer said 'this payee goes to this account'), then keyword rules, then
    history, then AI. Only a person's own decision (manual / keep / approved) or a locked line is never overridden.

    Earlier payee results are cleared first, so deleting or changing a rule takes effect on re-apply."""
    from app.services.coding.text import payee_key, payee_matches

    known = {a["code"] for a in accounts}
    exact: Dict[tuple, Dict[str, Any]] = {}
    fuzzy: List[Dict[str, Any]] = []
    for r in rules:
        if r.get("active", True) and r["account_code"] in known:
            exact[(r["payee_key"], r["direction"])] = r
            if r["payee_key"].startswith("d:"):
                fuzzy.append(r)

    for it in items:        # forget what rules said last time
        if it.get("source") == "payee" and not it.get("approved"):
            it.update(proposed_code=None, source=None, confidence=None, reason="", payee_rule_id=None)

    applied = 0
    for it in items:
        if it["status"] == "locked" or it.get("source") in ("manual", "keep") or it.get("approved"):
            continue
        key, _ = payee_key(it)
        if not key:
            continue
        r = exact.get((key, it["direction"])) or next(
            (f for f in fuzzy if f["direction"] == it["direction"] and payee_matches(f["payee_key"], key)), None)
        if r:
            it.update(proposed_code=r["account_code"], source="payee", confidence=1.0, level=None, history=None,
                      auto_approved=False, reason=f"Payee rule: {r['payee_display']}", payee_rule_id=r["id"])
            applied += 1
    return applied


# ------------------------------------------------------------------ history
def apply_history(items: List[Dict[str, Any]], history: List[Dict[str, Any]], accounts: List[Dict[str, Any]],
                  auto_approve: bool = False) -> int:
    """Suggest a category from how comparable lines were coded before. Runs after rules, before AI.

    Only lines with no rule/manual/keep decision are touched. A suggestion carries a categorical `level`
    (high/medium/low), never an invented percentage. Rule lines are left alone, but if strong history disagrees
    with the rule the line is annotated (`history_conflict`) so the user can see it. Returns lines suggested.

    auto_approve is True only once the client has EARNED it (services.accuracy.policy_status): HIGH-level suggestions
    that change a code are then pre-approved (`auto_approved`) instead of waiting for review. Nothing is written to
    the accounting system until the user presses Apply, and auto-approved lines stay visible and are logged."""
    names = {a["code"]: a["name"] for a in accounts}
    by_id = {i["id"]: i for i in history}
    by_id.update({i["id"]: i for i in items})            # this run's own lines are history for each other
    index = HistoryIndex(list(by_id.values()))
    suggested = 0

    for it in items:
        if it["status"] == "locked" or it.get("approved"):
            continue
        ev = index.evidence_for(it)
        d = assess(it, ev, names)

        if it.get("source") in ("rule", "payee"):
            # Conflict = strong history points somewhere else than the rule, whether or not the books already agree with
            # history ("agree") or differ from it ("flag"). Either way a person should see it.
            if d.outcome in ("flag", "agree") and d.level == "high" and d.code != it["proposed_code"]:
                what = "Payee rule" if it["source"] == "payee" else "Rule"
                it["history_conflict"] = (f"{what} says {it['proposed_code']}, but history says {d.code}: " + d.explanation)[:300]
            else:
                it.pop("history_conflict", None)
            continue
        if it.get("source") not in (None, "history"):
            continue                                          # manual / keep / ai decisions are not overridden

        if d.outcome == "no_evidence":
            if it.get("source") == "history":                 # earlier evidence no longer holds: clear it
                it.update(proposed_code=None, source=None, confidence=None, reason="", level=None, history=None,
                          auto_approved=False)
            continue
        it.update(proposed_code=d.code, source="history", confidence=None, level=d.level, reason=d.explanation[:200],
                  history={"n": d.n, "share": round(d.share, 2), "level": d.level, "examples": d.example_ids},
                  auto_approved=bool(auto_approve and d.level == "high" and d.code != it.get("current_code")))
        suggested += 1
    return suggested


# ------------------------------------------------------------------ AI
_SYSTEM = (
    "You are a meticulous bookkeeper assigning bank transaction lines to a chart of accounts. "
    "Respond with ONE JSON object: {\"assignments\":[{\"n\":<number>,\"code\":\"<account code>\","
    "\"confidence\":<0..1>,\"reason\":\"<short reason>\"}]}. "
    "Use ONLY account codes from the provided chart. Give exactly one assignment per line number. "
    "Money received should go to a revenue/income account (or another sensible non-expense account); "
    "money spent should go to an expense, asset or liability account. If the line's current code is already "
    "appropriate, keep it. If unsure, pick the closest account and use a low confidence."
)


def _chart_text(accounts: List[Dict[str, Any]]) -> str:
    return "\n".join(f"{a['code']} | {a['name']} | {a.get('type')}" for a in accounts)


def ai_categorise(items: List[Dict[str, Any]], accounts: List[Dict[str, Any]], provider,
                  progress: Optional[Callable[[str], None]] = None, only_unassigned: bool = False) -> int:
    """Fill proposals for lines that no rule matched. Returns number of lines the AI answered."""
    # History comes from the user's own books and is checked against context, so it outranks a generic model.
    pending = [it for it in items if it["status"] != "locked" and it["source"] not in ("rule", "manual", "history", "payee")]
    if only_unassigned:
        pending = [it for it in pending if not it["proposed_code"]]
    codes = {a["code"] for a in accounts}
    chart = _chart_text(accounts)
    answered = 0

    for start in range(0, len(pending), AI_BATCH):
        batch = pending[start:start + AI_BATCH]
        if progress:
            progress(f"AI categorising lines {start + 1}-{start + len(batch)} of {len(pending)}...")
        lines = "\n".join(
            f"{n} | {it['date']} | {it['direction'].upper()} | {abs(it['amount']):.2f} | "
            f"{it['contact']} | {it['description']} | current={it['current_code'] or '-'}"
            for n, it in enumerate(batch, 1)
        )
        user = f"CHART OF ACCOUNTS (code | name | type):\n{chart}\n\nLINES (n | date | direction | amount | contact | description | current):\n{lines}"
        try:
            data = complete_json(provider, _SYSTEM, user)
        except Exception as exc:
            logger.warning(f"AI batch failed: {exc}")
            for it in batch:
                it["reason"] = f"AI unavailable: {str(exc)[:120]}"
            continue

        by_n = {}
        for a in data.get("assignments", []) if isinstance(data, dict) else []:
            try:
                by_n[int(a.get("n"))] = a
            except (TypeError, ValueError):
                continue
        for n, it in enumerate(batch, 1):
            a = by_n.get(n)
            if not a:
                it["reason"] = "AI gave no answer for this line"
                continue
            code = str(a.get("code") or "").strip()
            if code not in codes:
                it["reason"] = f"AI suggested unknown code '{code}'"
                continue
            try:
                conf = max(0.0, min(1.0, float(a.get("confidence", 0.5))))
            except (TypeError, ValueError):
                conf = 0.5
            it.update(proposed_code=code, source="ai", confidence=round(conf, 2), approved=False,
                      reason=str(a.get("reason") or "")[:200])
            answered += 1
    return answered


# ------------------------------------------------------------------ status + checks
def refresh(items: List[Dict[str, Any]], accounts: List[Dict[str, Any]]) -> None:
    """Recompute each line's status from its proposal."""
    by_code = {a["code"]: a for a in accounts}
    for it in items:
        if it["status"] == "locked":
            continue
        code = it["proposed_code"]
        if not code:
            it["status"] = "unassigned"
            continue
        acc = by_code.get(code)
        if acc is None:
            it["status"] = "review"
            it["reason"] = f"Code {code} is not in the chart of accounts"
            continue
        klass = (acc.get("class") or "").upper()
        mismatch = (it["direction"] == "receive" and klass == "EXPENSE") or (it["direction"] == "spend" and klass == "REVENUE")
        trusted = it["source"] in ("rule", "manual", "keep", "payee") or it.get("approved")
        if mismatch and it["source"] != "manual" and not it.get("approved"):
            it["status"] = "review"
            it["reason"] = f"{it['direction'].title()} line assigned to a {klass.lower()} account - please confirm"
        elif it["source"] == "history" and not it.get("approved"):
            # Only a HIGH-level agreement with what the books already say is trusted. Anything that would change
            # a code, or is less than high, waits for a person.
            it["status"] = "ok" if (it.get("level") == "high" and (code == it["current_code"] or it.get("auto_approved"))) else "review"
        elif trusted or (it["confidence"] or 0) >= MIN_CONFIDENCE:
            it["status"] = "ok"
        else:
            it["status"] = "review"


def summarise(items: List[Dict[str, Any]], accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    names = {a["code"]: a["name"] for a in accounts}
    groups: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "received": 0.0, "spent": 0.0})
    for it in items:
        key = it["proposed_code"] or "UNASSIGNED"
        g = groups[key]
        g["count"] += 1
        if it["amount"] >= 0:
            g["received"] += it["amount"]
        else:
            g["spent"] += -it["amount"]
    out = []
    for code, g in sorted(groups.items()):
        out.append({"code": code, "name": names.get(code, "Unassigned" if code == "UNASSIGNED" else ""),
                    "count": g["count"], "received": round(g["received"], 2), "spent": round(g["spent"], 2),
                    "net": round(g["received"] - g["spent"], 2)})
    return out


def run_checks(items: List[Dict[str, Any]], accounts: List[Dict[str, Any]], system: str = "Xero") -> Dict[str, Any]:
    """Integrity checks. `balanced` is True only when no check has level 'error'."""
    checks: List[Dict[str, Any]] = []
    codes = {a["code"] for a in accounts}

    def add(name: str, ok: bool, detail: str, level: str = "error") -> None:
        checks.append({"name": name, "ok": ok, "level": "ok" if ok else level, "detail": detail})

    # 1. Every line has a category (locked lines keep their current one)
    unassigned = [i for i in items if i["status"] == "unassigned"]
    add("Every line is categorised", not unassigned, f"{len(unassigned)} line(s) still unassigned" if unassigned else f"All {len(items)} lines have a category")

    # 2. No line appears twice / counted in two categories
    ids = [i["id"] for i in items]
    dup_ids = len(ids) - len(set(ids))
    cat_lines = sum(g["count"] for g in summarise(items, accounts))
    add("No line counted in two categories", dup_ids == 0 and cat_lines == len(items),
        f"{len(items)} lines -> {cat_lines} category entries" + (f", {dup_ids} duplicate id(s)" if dup_ids else ""))

    # 3. Category totals tie to the ledger total (receipts - payments)
    ledger_net = round(sum(i["amount"] for i in items), 2)
    cat_net = round(sum(g["net"] for g in summarise(items, accounts)), 2)
    ledger_in = round(sum(i["amount"] for i in items if i["amount"] >= 0), 2)
    ledger_out = round(-sum(i["amount"] for i in items if i["amount"] < 0), 2)
    add("Category totals balance to the ledger", abs(ledger_net - cat_net) < EPS,
        f"Received {ledger_in:,.2f} - Spent {ledger_out:,.2f} = {ledger_net:,.2f}; categories sum to {cat_net:,.2f}")

    # 4. Codes exist in the chart
    bad = [i for i in items if i["proposed_code"] and i["proposed_code"] not in codes and i["status"] != "locked"]
    add(f"All account codes exist in {system}", not bad, f"{len(bad)} line(s) use an unknown code" if bad else "All codes valid")

    # 5. Human review must be finished; then warnings (don't block)
    review = [i for i in items if i["status"] == "review"]
    add("Every suggestion reviewed", not review,
        f"{len(review)} low-confidence or odd line(s) - change them or use Accept suggestions" if review
        else "No lines waiting for review")

    seen: Dict[Tuple, int] = defaultdict(int)
    for i in items:
        seen[(i["date"], i["amount"], i["description"].lower())] += 1
    dupes = sum(1 for v in seen.values() if v > 1)
    add("Possible duplicate transactions", dupes == 0, f"{dupes} group(s) share date, amount and description", level="warn")

    # Tax codes: reported, never blocking (several codes on one account can be legitimate)
    names = {a["code"]: a["name"] for a in accounts}
    tax = tax_checks.findings(items, names)
    if any("tax_code" in i for i in items):
        multi = sum(1 for f in tax if f["kind"] == "multiple_tax_codes")
        outliers = sum(len(f["line_ids"]) for f in tax if f["kind"] == "tax_outlier")
        add("Tax codes consistent", not tax,
            f"{multi} account(s) use more than one tax code, {outliers} line(s) differ from an account's usual code - see Insights"
            if tax else "No inconsistent tax codes found", level="warn")

    conflicts = sum(1 for i in items if i.get("history_conflict"))
    if conflicts:
        add("Rules agree with history", False, f"{conflicts} rule line(s) conflict with strong history - see Insights", level="warn")

    locked = sum(1 for i in items if i["status"] == "locked")
    add("Locked lines (reconciled/transfers)", locked == 0, f"{locked} line(s) can't be changed in {system} and keep their current code", level="warn")

    return {
        "balanced": all(c["ok"] or c["level"] == "warn" for c in checks),
        "checks": checks,
        "totals": {"lines": len(items), "received": ledger_in, "spent": ledger_out, "net": ledger_net},
        "categories": summarise(items, accounts),
    }


def changes_to_apply(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [i for i in items if i["status"] != "locked" and i["proposed_code"] and i["proposed_code"] != i["current_code"]]


def build_update_payloads(changes: List[Dict[str, Any]], txs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Group changed lines by transaction and build Xero update bodies (all lines of the tx are sent)."""
    by_tx: Dict[str, Dict[int, str]] = defaultdict(dict)
    for c in changes:
        by_tx[c["tx_id"]][c["line_index"]] = c["proposed_code"]

    payloads = []
    for tx_id, line_codes in by_tx.items():
        tx = txs[tx_id]
        lines = []
        for idx, line in enumerate(tx["Lines"]):
            li = {k: v for k, v in line.items() if v is not None}
            if idx in line_codes:
                li["AccountCode"] = line_codes[idx]
            lines.append(li)
        body = {
            "BankTransactionID": tx_id,
            "Type": tx["Type"],
            "Date": tx["Date"],
            "BankAccount": {"AccountID": tx["BankAccountID"]},
            "LineItems": lines,
        }
        if tx.get("ContactID"):
            body["Contact"] = {"ContactID": tx["ContactID"]}
        if tx.get("LineAmountTypes"):
            body["LineAmountTypes"] = tx["LineAmountTypes"]
        if tx.get("Reference"):
            body["Reference"] = tx["Reference"]
        payloads.append(body)
    return payloads
