"""Learn from how the books were coded before. Historical coding is EVIDENCE, not truth: it can contain the very
errors we are hunting, so callers get the share/size of the evidence and must check context before acting."""
import dataclasses
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from app.services.coding.text import contact_key, item_tokens, jaccard

#: A prior line is "comparable" when its tokens overlap at least this much (or it is the same contact).
SIMILARITY_THRESHOLD = 0.5
#: Same supplier is strong context even when the narrative differs.
SAME_CONTACT_FLOOR = 0.8
#: Performance guards for large histories (an imported 12-month export can be tens of thousands of lines):
#: a token shared by more lines than this carries no information (it behaves like a stop word), and only the
#: best-overlapping candidates are scored.
POSTING_CAP = 4000
MAX_CANDIDATES = 3000


@dataclasses.dataclass
class Evidence:
    n: int                                   # comparable, already-coded lines (excluding the line itself)
    by_account: List[Dict[str, Any]]         # [{"code", "count", "share"}] most-supported first
    top_mean_similarity: float               # mean similarity of the lines behind the top account
    top_same_contact_share: float            # fraction of those sharing the supplier/contact
    example_ids: List[str]


class HistoryIndex:
    """Inverted-index lookup over coded history (lines whose books `current_code` is set)."""

    def __init__(self, coded_items: List[Dict[str, Any]]):
        self._feats: List[Dict[str, Any]] = []
        self._by_token: Dict[str, List[int]] = defaultdict(list)
        self._by_contact: Dict[str, List[int]] = defaultdict(list)
        for it in coded_items:
            if not it.get("current_code"):
                continue
            idx = len(self._feats)
            toks = item_tokens(it)
            ck = contact_key(it)
            self._feats.append({"id": it["id"], "code": it["current_code"], "toks": toks, "contact": ck,
                                "sign": 1 if it["amount"] > 0 else -1 if it["amount"] < 0 else 0,
                                "date": it.get("date"), "amount": it["amount"], "imported": it.get("origin") == "import"})
            for t in toks:
                self._by_token[t].append(idx)
            if ck:
                self._by_contact[ck].append(idx)

    def __len__(self) -> int:
        return len(self._feats)

    def _candidates(self, toks, ck) -> List[int]:
        counts: Counter = Counter()
        for t in toks:
            posting = self._by_token.get(t, ())
            if len(posting) <= POSTING_CAP:
                counts.update(posting)
        if ck:
            counts.update(self._by_contact.get(ck, ()))
        if len(counts) > MAX_CANDIDATES:
            return [i for i, _ in counts.most_common(MAX_CANDIDATES)]
        return list(counts)

    def evidence_for(self, item: Dict[str, Any]) -> Optional[Evidence]:
        """Comparable prior lines for `item`, same direction only (money in vs out is different context)."""
        toks, ck = item_tokens(item), contact_key(item)
        sign = 1 if item["amount"] > 0 else -1 if item["amount"] < 0 else 0
        is_import = item.get("origin") == "import"

        acc: Dict[str, Dict[str, Any]] = {}
        total, n = 0.0, 0
        for i in self._candidates(toks, ck):
            f = self._feats[i]
            if f["id"] == item["id"] or f["sign"] != sign:
                continue
            # An imported line is the same real transaction as a live line with the same date and amount. Counting it
            # would let a line "predict itself" through its own twin, so those are never evidence for each other.
            if f["imported"] != is_import and f["date"] == item.get("date") and abs(f["amount"] - item["amount"]) < 0.005:
                continue
            same = bool(ck) and f["contact"] == ck
            sim = max(jaccard(toks, f["toks"]), SAME_CONTACT_FLOOR if same else 0.0)
            if sim < SIMILARITY_THRESHOLD:
                continue
            a = acc.setdefault(f["code"], {"w": 0.0, "c": 0, "same": 0, "ids": []})
            a["w"] += sim
            a["c"] += 1
            a["same"] += 1 if same else 0
            if len(a["ids"]) < 5:
                a["ids"].append(f["id"])
            total += sim
            n += 1
        if n == 0:
            return None

        ranked = sorted(acc.items(), key=lambda kv: (-kv[1]["w"] / total, -kv[1]["c"]))
        top = ranked[0][1]
        return Evidence(
            n=n,
            by_account=[{"code": c, "count": a["c"], "share": a["w"] / total} for c, a in ranked],
            top_mean_similarity=top["w"] / top["c"],
            top_same_contact_share=top["same"] / top["c"],
            example_ids=top["ids"],
        )
