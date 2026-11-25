"""Turn history evidence into a recommendation with an honest, categorical confidence level.

The thresholds are explicit POLICY knobs, not calibrated probabilities. `evaluate.backtest()` measures how often
each level actually agrees with the books so they can be tuned from real data.
"""
import dataclasses
from typing import Any, Dict, Optional

from app.services.coding.history import Evidence

STRONG = {"share": 0.9, "n": 10}
MODERATE = {"share": 0.7, "n": 5}
WEAK = {"share": 0.5, "n": 3}
#: "Context verified": mostly the same supplier, or near-identical narratives.
CONTEXT_SIMILARITY = 0.8
CONTEXT_SAME_CONTACT = 0.6


@dataclasses.dataclass
class Decision:
    outcome: str                  # "agree" | "flag" | "no_evidence"
    code: Optional[str]           # recommended account code (the history's top account)
    level: Optional[str]          # "high" | "medium" | "low"
    strength: Optional[str]       # "strong" | "moderate" | "weak"
    context_ok: bool
    n: int
    share: float
    explanation: str
    example_ids: list


def strength_of(ev: Evidence) -> Optional[str]:
    top = ev.by_account[0]
    if top["share"] >= STRONG["share"] and ev.n >= STRONG["n"]:
        return "strong"
    if top["share"] >= MODERATE["share"] and ev.n >= MODERATE["n"]:
        return "moderate"
    if top["share"] >= WEAK["share"] and ev.n >= WEAK["n"]:
        return "weak"
    return None


def assess(item: Dict[str, Any], ev: Optional[Evidence], names: Dict[str, str]) -> Decision:
    """`names` = chart of accounts as {code: name}. History pointing at an account that is not in the chart (archived)
    is ignored rather than suggested."""
    none = Decision("no_evidence", None, None, None, False, ev.n if ev else 0, 0.0,
                    "Too little comparable history to assess this line.", [])
    if ev is None:
        return none
    strength = strength_of(ev)
    top = ev.by_account[0]
    if strength is None:
        return none
    if top["code"] not in names:
        none.explanation = f"History points to account {top['code']}, which is not in the current chart."
        return none

    context_ok = ev.top_mean_similarity >= CONTEXT_SIMILARITY or ev.top_same_contact_share >= CONTEXT_SAME_CONTACT
    if strength == "strong" and context_ok:
        level = "high"
    elif strength == "strong" or (strength == "moderate" and context_ok):
        level = "medium"
    else:
        level = "low"

    share_txt = f"{round(top['share'] * 100)}% of {ev.n} comparable past lines were coded to {top['code']} {names[top['code']]}"
    note = "" if context_ok else " Comparable lines are only loosely similar, so context is not confirmed."
    current = item.get("current_code")
    if current == top["code"]:
        outcome, why = "agree", f"Consistent with history: {share_txt}."
    elif current:
        outcome, why = "flag", f"Currently {current} {names.get(current, '')}; {share_txt}.{note}".replace("  ", " ")
    else:
        outcome, why = "flag", f"{share_txt}.{note}"
    return Decision(outcome, top["code"], level, strength, context_ok, ev.n, top["share"], why, ev.example_ids)
