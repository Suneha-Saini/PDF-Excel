"""Backtest of the history signal: for every line already coded in the books, predict its account from all the OTHER
lines, then compare. This measures real agreement per confidence level instead of asserting accuracy.

Caveat shown to the user: the books are the reference, and they may themselves contain errors. This is agreement
with existing coding, not proof of correctness."""
from collections import Counter
from typing import Any, Dict, List

from app.services.coding.decide import assess
from app.services.coding.history import HistoryIndex


MAX_EVALUATED = 3000      # keeps the backtest fast on big imported histories; sampled evenly, not randomly


def backtest(items: List[Dict[str, Any]], names: Dict[str, str]) -> Dict[str, Any]:
    coded = [i for i in items if i.get("current_code")]
    index = HistoryIndex(coded)          # all coded lines are evidence...
    step = max(1, -(-len(coded) // MAX_EVALUATED))
    sample = coded[::step]               # ...but at most ~3000 of them are scored
    levels: Dict[str, Dict[str, int]] = {lv: {"n": 0, "agree": 0} for lv in ("high", "medium", "low")}
    confusion: Counter = Counter()
    with_evidence = 0

    for it in sample:
        d = assess(it, index.evidence_for(it), names)
        if d.outcome == "no_evidence":
            continue
        with_evidence += 1
        lv = levels[d.level]
        lv["n"] += 1
        if d.code == it["current_code"]:
            lv["agree"] += 1
        else:
            confusion[(it["current_code"], d.code)] += 1

    total = len(sample)
    pct = lambda a, b: round(100 * a / b, 1) if b else None
    return {
        "coded_lines": len(coded),
        "evaluated": total,
        "with_evidence": with_evidence,
        "coverage_pct": pct(with_evidence, total),
        "by_level": {lv: {"lines": v["n"], "agree_with_books_pct": pct(v["agree"], v["n"])} for lv, v in levels.items()},
        # lines a person still has to look at if only 'high' agreements are trusted
        "needs_review_pct": pct(total - levels["high"]["agree"], total) if total else None,
        "top_disagreements": [{"books": a, "history_says": b, "lines": c,
                               "books_name": names.get(a, ""), "history_name": names.get(b, "")}
                              for (a, b), c in confusion.most_common(5)],
        "note": "Agreement with the existing coding in the books, which may itself contain errors.",
    }
