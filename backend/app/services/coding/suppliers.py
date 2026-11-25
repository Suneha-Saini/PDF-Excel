"""Supplier checks. The ABN checks prove an ABN is *well-formed*, nothing more: existence and ownership need the
ABR register, which is not integrated, and the results say so."""
from typing import Any, Dict, List

from app.services.coding.text import jaccard, tokens

_WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
DUPLICATE_NAME_SIMILARITY = 0.8


def abn_checksum_valid(abn: Any) -> bool:
    """ATO checksum for an Australian Business Number."""
    digits = "".join(str(abn or "").split())
    if len(digits) != 11 or not digits.isdigit():
        return False
    nums = [int(d) for d in digits]
    nums[0] -= 1
    return sum(n * w for n, w in zip(nums, _WEIGHTS)) % 89 == 0


def supplier_findings(suppliers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """suppliers: [{"id", "name", "abn"}]."""
    out: List[Dict[str, Any]] = []
    clean = lambda s: "".join(str(s.get("abn") or "").split())
    any_abn = any(clean(s) for s in suppliers)

    if suppliers and not any_abn:
        # Not an Australian ledger (or ABNs were never recorded): one note, not a finding per supplier.
        return [{"kind": "no_abns_recorded", "ids": [], "explanation":
                 f"None of the {len(suppliers)} suppliers has an ABN recorded, so ABN checks were skipped."}]

    for s in suppliers:
        abn = clean(s)
        if not abn:
            out.append({"kind": "missing_abn", "ids": [s["id"]], "explanation": f"Supplier \"{s['name']}\" has no ABN recorded."})
        elif not abn_checksum_valid(abn):
            out.append({"kind": "invalid_abn_format", "ids": [s["id"]], "explanation":
                        f"Supplier \"{s['name']}\" has ABN \"{abn}\", which fails the ABN checksum (typo or not an ABN)."})

    by_abn: Dict[str, List[Dict[str, Any]]] = {}
    for s in suppliers:
        abn = clean(s)
        if abn and abn_checksum_valid(abn):
            by_abn.setdefault(abn, []).append(s)
    shared_pairs = set()
    for abn, group in by_abn.items():
        if len(group) > 1:
            out.append({"kind": "shared_abn", "ids": [g["id"] for g in group], "explanation":
                        f"ABN {abn} is recorded on {len(group)} suppliers ({', '.join(chr(34) + g['name'] + chr(34) for g in group)}): likely duplicates."})
            shared_pairs.update((a["id"], b["id"]) for a in group for b in group)

    toks = [tokens(s["name"]) for s in suppliers]
    for i in range(len(suppliers)):
        for j in range(i + 1, len(suppliers)):
            a, b = suppliers[i], suppliers[j]
            if (a["id"], b["id"]) in shared_pairs:
                continue                                    # already reported through the shared ABN
            sim = jaccard(toks[i], toks[j])
            if sim >= DUPLICATE_NAME_SIMILARITY:
                out.append({"kind": "possible_duplicate", "ids": [a["id"], b["id"]], "name_similarity": round(sim, 2),
                            "explanation": f"Suppliers \"{a['name']}\" and \"{b['name']}\" have near-identical names."})
    return out
