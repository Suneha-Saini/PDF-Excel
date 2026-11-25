"""Ledger-setup review: learn how a client's ledger has been coded and rank POTENTIAL problems High / Medium / Info.

Works on the client's coded history (live runs + imported exports). Everything here is a *question for an accountant*,
never a verdict: multiple tax codes on an account can be legitimate, and a payment to an income account can be a real
refund. Each row says what to check.

Heuristics are conservative and explicit:
  * tax checks need tax info on the line. Lines without it (older fetches, exports with no tax column) are skipped and
    the coverage is reported, never treated as "untaxed".
  * the GST-free account list is an AUSTRALIAN-GST heuristic and only runs when the client's tax codes look like GST.
"""
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from app.services.coding.text import contact_key, tokens

SEVERITIES = ("High", "Medium", "Info")
MAX_ISSUES = 500
NO_TAX = "(none)"

# Xero's standard Australian tax types -> the label an export shows, so live lines and imported lines agree.
XERO_AU_TAX = {
    "OUTPUT": "GST on Income", "OUTPUT2": "GST on Income", "OUTPUT3": "GST on Income",
    "INPUT": "GST on Expenses", "INPUT2": "GST on Expenses", "INPUT3": "GST on Expenses",
    "EXEMPTOUTPUT": "GST Free Income", "EXEMPTINPUT": "GST Free Expenses",
    "CAPEXINPUT": "GST on Capital", "CAPEXINPUT2": "GST on Capital", "EXEMPTCAPITAL": "GST Free Capital",
    "GSTONIMPORTS": "GST on Imports", "BASEXCLUDED": "BAS Excluded", "NONE": NO_TAX,
}

_INCOME_WORDS = ("income", "output", "sales")
_EXPENSE_WORDS = ("expense", "input", "purchase", "capital", "capex", "import")
_GST_FREE_ACCOUNT = re.compile(
    r"\b(bank fees?|bank charges?|interest|wages?|salar(?:y|ies)|superannuation|super(?:annuation)? guarantee|payg|"
    r"depreciation|drawings?|dividends?|loan(?:s)?|income tax|rates and taxes|fines?|penalt(?:y|ies))\b", re.I)


def canon_tax(raw: Any) -> Optional[str]:
    """Readable, comparable tax label, or None when the line has no tax information at all."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    return XERO_AU_TAX.get(s.upper(), s)


def tax_side(label: Optional[str]) -> Optional[str]:
    """'income' (a sales-side code) / 'expense' (a purchases-side code) / None when unknown or neutral."""
    if not label or label == NO_TAX:
        return None
    # "Input Taxed" is a kind of supply, not the purchases-side "INPUT" code: it only has a side if the label says so
    # ("Input Taxed Sales", "Input Taxed Purchases").
    low = label.lower().replace("input taxed", " ")
    inc, exp = any(w in low for w in _INCOME_WORDS), any(w in low for w in _EXPENSE_WORDS)
    return "income" if inc and not exp else "expense" if exp and not inc else None


def gst_bearing(label: Optional[str]) -> bool:
    if not label or label == NO_TAX:
        return False
    low = label.lower()
    if any(w in low for w in ("free", "excluded", "no gst", "input taxed", "exempt")):
        return False
    return "gst on" in low or low.startswith(("gst", "input", "output"))


def looks_like_gst(labels: List[str]) -> bool:
    return any("gst" in l.lower() or l.upper().startswith(("INPUT", "OUTPUT")) for l in labels)


def _payee(it: Dict[str, Any]) -> Tuple[str, str]:
    """(grouping key, display name). Lines without a contact fall back to their meaningful words."""
    ck = contact_key(it)
    if ck:
        return ck, (it.get("contact") or "").strip()
    toks = sorted(tokens(it.get("description")))
    return " ".join(toks[:6]), (it.get("description") or "").strip()[:40] or "(no payee)"


def _tax_of(it: Dict[str, Any]) -> Optional[str]:
    if "tax_code" in it:                                    # live line: Xero TaxType / QuickBooks TaxCodeRef (None = no tax)
        return canon_tax(it["tax_code"]) or NO_TAX
    return canon_tax(it.get("tax_label"))                   # imported line: only what the export's tax column said


_DRAWINGS = re.compile(r"\b(drawings?|owner'?s? draws?)\b", re.I)
_DIVIDENDS_OR_DIRECTOR_LOAN = re.compile(r"\b(dividends?|director'?s? loans?|shareholder'?s? loans?|franking)\b", re.I)


tax_of = _tax_of          # public name: other modules (BAS estimate) read a line's tax the same way


def analyse(lines: List[Dict[str, Any]], accounts: List[Dict[str, Any]], settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`settings` = the client's entity_type / gst_registration (see services.client_settings). 'unknown' or absent means
    nothing is assumed: GST context is guessed from the tax codes in use and entity checks are skipped."""
    settings = settings or {}
    reg = settings.get("gst_registration") or "unknown"
    entity = settings.get("entity_type") or "unknown"
    names = {str(a["code"]): a["name"] for a in accounts}
    classes = {str(a["code"]): (a.get("class") or "").upper() for a in accounts}
    coded = [i for i in lines if i.get("current_code") and str(i["current_code"]) in names]

    # ---- group lines by who it was with, direction, account and tax code
    groups: Dict[tuple, Dict[str, Any]] = {}
    for it in coded:
        pk, display = _payee(it)
        direction = "Spend" if it["amount"] < 0 else "Receive"
        tax = _tax_of(it)
        g = groups.setdefault((pk, direction, str(it["current_code"]), tax), {"n": 0, "ids": [], "names": Counter()})
        g["n"] += 1
        g["names"][display] += 1
        if len(g["ids"]) < 5:
            g["ids"].append(it["id"])

    with_tax = [k for k in groups if k[3] is not None]
    tax_lines = sum(groups[k]["n"] for k in with_tax)
    # GST context: what the accountant said wins; only when it is not set do we guess from the tax codes in use.
    if reg in ("cash", "accrual"):
        gst_ctx = True
    elif reg == "not_registered":
        gst_ctx = False
    else:
        gst_ctx = looks_like_gst([k[3] for k in with_tax])

    issues: List[Dict[str, Any]] = []

    def add(sev, kind, text, key, g, tax=None):
        pk, direction, code, _ = key
        issues.append({"severity": sev, "kind": kind, "issue": text, "payee": g["names"].most_common(1)[0][0],
                       "direction": direction, "account": code, "account_name": names.get(code, ""),
                       "tax": tax if tax is not None else (key[3] or ""), "times": g["n"], "line_ids": g["ids"]})

    # Direction vs account type does not depend on tax, so it is one row per payee + direction + account.
    pda: Dict[tuple, Dict[str, Any]] = {}
    for (pk, direction, code, _tax), g in groups.items():
        m = pda.setdefault((pk, direction, code), {"n": 0, "ids": [], "names": Counter()})
        m["n"] += g["n"]
        m["names"] += g["names"]
        m["ids"] = (m["ids"] + g["ids"])[:5]
    for (pk, direction, code), m in pda.items():
        cls = classes.get(code, "")
        if direction == "Spend" and cls in ("REVENUE", "INCOME"):
            add("Medium", "direction_vs_account", "money out coded to an income account - a refund to a customer? check the account",
                (pk, direction, code, None), m, tax="")
        elif direction == "Receive" and cls == "EXPENSE":
            add("Medium", "direction_vs_account", "money in coded to an expense account - a refund from a supplier? check the account",
                (pk, direction, code, None), m, tax="")

        # Medium: an account that belongs to a different kind of entity (only when the entity type has been set)
        acct_name = names.get(code, "")
        if entity == "company" and _DRAWINGS.search(acct_name):
            add("Medium", "entity_account", f"{acct_name} on a company - drawings belong to sole traders and partnerships; for a company this is usually a director loan or dividend",
                (pk, direction, code, None), m, tax="")
        elif entity in ("sole_trader", "partnership", "individual") and _DIVIDENDS_OR_DIRECTOR_LOAN.search(acct_name):
            what = {"sole_trader": "a sole trader", "partnership": "a partnership", "individual": "an individual"}[entity]
            add("Medium", "entity_account", f"{acct_name} on {what} - dividends and director/shareholder loans normally only exist in a company",
                (pk, direction, code, None), m, tax="")

    # "No tax" next to a non-taxable code ('NON', 'GST Free ...', 'BAS Excluded') is the same outcome, not a second tax code.
    labels_by_account: Dict[str, set] = defaultdict(set)
    for k in with_tax:
        labels_by_account[k[2]].add(k[3])
    ignore_none = {code for code, ls in labels_by_account.items()
                   if NO_TAX in ls and all(not gst_bearing(l) for l in ls if l != NO_TAX)}
    countable = [k for k in with_tax if not (k[3] == NO_TAX and k[2] in ignore_none)]

    for key, g in groups.items():
        pk, direction, code, tax = key
        acct = f"{code} {names.get(code, '')}".strip()

        # High: the tax code belongs to the other side of the transaction
        side = tax_side(tax)
        if direction == "Spend" and side == "income":
            add("High", "tax_direction", "an income tax code on money out", key, g)
        elif direction == "Receive" and side == "expense":
            add("High", "tax_direction", "an expense tax code on money in", key, g)

        # High: GST on a client that is not registered for GST (it cannot charge or claim it)
        if reg == "not_registered" and tax is not None and gst_bearing(tax):
            add("High", "gst_not_registered", "GST code on a client that is not registered for GST", key, g)

        # Medium: GST on an account that is usually GST-free (Australian heuristic)
        if gst_ctx and tax is not None and gst_bearing(tax) and _GST_FREE_ACCOUNT.search(names.get(code, "")):
            add("Medium", "gst_on_gst_free_account", f"GST code on {names.get(code, acct)} - usually GST free or no GST", key, g)

    # ---- the same payee on the same account with different tax codes
    by_pa: Dict[tuple, List[tuple]] = defaultdict(list)
    for key in countable:
        by_pa[(key[0], key[1], key[2])].append(key)
    for (pk, direction, code), keys in by_pa.items():
        if len(keys) >= 2 and sum(groups[k]["n"] for k in keys) >= 3:
            keys.sort(key=lambda k: -groups[k]["n"])
            dist = ", ".join(f"'{k[3]}' {groups[k]['n']}" for k in keys)
            merged = {"n": sum(groups[k]["n"] for k in keys), "ids": [i for k in keys for i in groups[k]["ids"]][:5],
                      "names": sum((groups[k]["names"] for k in keys), Counter())}
            add("Medium", "payee_two_tax_codes", f"same payee, two tax codes on {names.get(code, code)}: {dist} - confirm which is right",
                (pk, direction, code, None), merged, tax=" / ".join(k[3] for k in keys))

    # ---- accounts that use more than one tax code (a table, plus Info rows for the payees on the minority codes)
    per_account: Dict[str, Counter] = defaultdict(Counter)
    for k in countable:
        per_account[k[2]][k[3]] += groups[k]["n"]
    multi = []
    for code, c in per_account.items():
        if len(c) < 2:
            continue
        total = sum(c.values())
        dominant = c.most_common(1)[0][0]
        dist = ", ".join(f"'{t}' {n}" for t, n in c.most_common())
        multi.append({"account": code, "account_name": names.get(code, ""), "lines": total,
                      "tax_codes": [{"tax": t, "lines": n} for t, n in c.most_common()]})
        minority = sorted((k for k in countable if k[2] == code and k[3] != dominant), key=lambda k: -groups[k]["n"])[:40]
        for k in minority:
            add("Info", "account_multi_tax", f"{names.get(code, code)} uses {len(c)} tax codes ({dist}) - confirm which is right per payee", k, groups[k])
    multi.sort(key=lambda m: -m["lines"])

    # ---- one payee coded to several accounts
    by_payee: Dict[tuple, Counter] = defaultdict(Counter)
    shown: Dict[tuple, Dict[str, Any]] = {}
    for (pk, direction, code, tax), g in groups.items():
        by_payee[(pk, direction)][code] += g["n"]
        s = shown.setdefault((pk, direction), {"n": 0, "ids": [], "names": Counter()})
        s["n"] += g["n"]
        s["names"] += g["names"]
        s["ids"] = (s["ids"] + g["ids"])[:5]
    for (pk, direction), c in by_payee.items():
        total = sum(c.values())
        spread = [(code, n) for code, n in c.most_common() if n / total >= 0.2]
        if total >= 5 and len(spread) >= 2:
            dist = ", ".join(f"{names.get(code, code)} {n}" for code, n in c.most_common(4))
            add("Info", "payee_split_accounts", f"coded to {len(c)} accounts ({dist}) - confirm which is right",
                (pk, direction, c.most_common(1)[0][0], None), shown[(pk, direction)], tax="")

    order = {s: i for i, s in enumerate(SEVERITIES)}
    issues.sort(key=lambda x: (order[x["severity"]], -x["times"]))
    counts = Counter(i["severity"] for i in issues)
    return {
        "issues": issues[:MAX_ISSUES], "truncated": len(issues) > MAX_ISSUES,
        "multi_tax_accounts": multi,
        "summary": {"High": counts["High"], "Medium": counts["Medium"], "Info": counts["Info"]},
        "coverage": {"coded_lines": len(coded), "lines_with_tax_info": tax_lines,
                     "gst_checks_ran": gst_ctx, "gst_registration": reg, "entity_type": entity},
        "note": "These are questions for an accountant, not errors: several tax codes on one account, or a payment to an income "
                "account, can be legitimate. Tax checks only use lines that carry tax information.",
    }
