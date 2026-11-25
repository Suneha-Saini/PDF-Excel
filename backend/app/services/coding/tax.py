"""Tax-code consistency. Several tax codes on one account is NOT automatically wrong (mixed-supply accounts exist),
so an account is reported for review, and individual lines are called outliers only when one code overwhelmingly
dominates. Findings are warnings: recoding tax is not part of Apply."""
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

DOMINANT_SHARE = 0.9
DOMINANT_MIN_LINES = 10
NO_TAX = "(none)"


def build_profiles(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per account code: how its lines are tax-coded."""
    counts: Dict[str, Counter] = defaultdict(Counter)
    for it in items:
        code = it.get("current_code")
        if code and "tax_code" in it:               # lines fetched before tax codes were captured are skipped
            counts[code][it.get("tax_code") or NO_TAX] += 1
    out = {}
    for code, c in counts.items():
        n = sum(c.values())
        ranked = c.most_common()
        out[code] = {"n": n, "distribution": [{"tax": t, "count": k, "share": k / n} for t, k in ranked],
                     "dominant": ranked[0][0], "dominant_share": ranked[0][1] / n}
    return out


def findings(items: List[Dict[str, Any]], names: Dict[str, str],
             defaults: Optional[Dict[str, Optional[str]]] = None) -> List[Dict[str, Any]]:
    profiles = build_profiles(items)
    label = lambda code: f"{code} {names.get(code, '')}".strip()
    out: List[Dict[str, Any]] = []
    for code, p in profiles.items():
        if len(p["distribution"]) < 2:
            continue
        dist = ", ".join(f"{d['tax']} {round(d['share'] * 100)}% ({d['count']})" for d in p["distribution"])
        out.append({"kind": "multiple_tax_codes", "account": code, "line_ids": [],
                    "explanation": f"{label(code)} is used with {len(p['distribution'])} tax codes across {p['n']} lines: "
                                   f"{dist}. Not necessarily an error; needs accountant review."})
        if p["dominant_share"] >= DOMINANT_SHARE and p["n"] >= DOMINANT_MIN_LINES:
            odd = [i["id"] for i in items if i.get("current_code") == code and "tax_code" in i
                   and (i.get("tax_code") or NO_TAX) != p["dominant"]]
            out.append({"kind": "tax_outlier", "account": code, "line_ids": odd,
                        "explanation": f"{len(odd)} line(s) on {label(code)} use a tax code other than {p['dominant']}, "
                                       f"which covers {round(p['dominant_share'] * 100)}% of {p['n']} lines."})

    for code, default in (defaults or {}).items():           # account declares a default tax code but the line has none
        if not default or default == "NONE":
            continue
        miss = [i["id"] for i in items if i.get("current_code") == code and "tax_code" in i and not i.get("tax_code")]
        if miss:
            out.append({"kind": "missing_tax_code", "account": code, "line_ids": miss,
                        "explanation": f"{len(miss)} line(s) on {label(code)} have no tax code although the account has a default."})
    return out
