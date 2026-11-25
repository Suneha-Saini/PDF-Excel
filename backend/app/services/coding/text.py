"""Bank narratives are long and noisy (card numbers, dates, location, "PURCHASE", "PTY LTD"), which breaks plain
keyword rules. Lines are compared as sets of *meaningful* tokens instead."""
import re
from typing import Any, Dict, FrozenSet, Optional, Tuple

_NOISE = frozenset((
    "CARD VISA MASTERCARD EFTPOS PURCHASE DEBIT CREDIT PAYMENT TRANSFER DIRECT DEPOSIT WITHDRAWAL "
    "PTY LTD LIMITED INC AU AUS AUSTRALIA NSW VIC QLD WA SA TAS ACT NT THE OF AND TO FROM REF REFERENCE "
    "ON DATE VALUE BPAY OSKO PAYID INTERNET BANKING ATM POS TAP PAY"
).split())
_SPLIT = re.compile(r"[^A-Z0-9]+")


def tokens(text: Optional[str]) -> FrozenSet[str]:
    out = set()
    for raw in _SPLIT.split((text or "").upper()):
        if len(raw) < 2 or raw in _NOISE:
            continue
        digits = sum(ch.isdigit() for ch in raw)
        if digits == len(raw):                       # pure number: card digits, amounts, dates
            continue
        if len(raw) >= 5 and digits / len(raw) > 0.5:   # reference-like, e.g. TXN83921
            continue
        out.add(raw)
    return frozenset(out)


def jaccard(a: FrozenSet[str], b: FrozenSet[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


# Contacts that are filler, not a real payee. This app's own statement export files every line under "Suspense Contact";
# treated as a real supplier it makes every line look like the same supplier (and adds the same words to every line).
_PLACEHOLDER_CONTACTS = {"suspense contact", "suspense", "unknown", "unspecified", "unallocated", "no contact", "no payee",
                         "(no payee)", "not specified", "n/a", "none", "-"}


def _placeholders() -> set:
    try:
        from app.config import settings
        return _PLACEHOLDER_CONTACTS | {str(settings.XERO_SUSPENSE_CONTACT_NAME or "").strip().lower()}
    except Exception:  # noqa: BLE001 - settings are optional for pure helpers
        return _PLACEHOLDER_CONTACTS


def contact_key(item: Dict[str, Any]) -> str:
    """The real payee, lower-cased, or "" when the line has no meaningful contact."""
    c = (item.get("contact") or "").strip().lower()
    return "" if (not c or c in _placeholders()) else c


def item_tokens(item: Dict[str, Any]) -> FrozenSet[str]:
    """What an accountant reads when coding a line: description, reference and who it was with."""
    who = (item.get("contact") or "") if contact_key(item) else ""
    return tokens(f"{item.get('description') or ''} {item.get('reference') or ''} {who}")


FUZZY_PAYEE_SIMILARITY = 0.8


def payee_key(item: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Who a line was with: (key, display name). A named contact gives an exact key ("c:..."). Without one the key is
    the line's meaningful words ("d:..."), which are matched loosely because bank narratives vary. None = no payee."""
    ck = contact_key(item)
    if ck:
        return "c:" + ck, (item.get("contact") or "").strip()
    toks = sorted(tokens(item.get("description")))[:12]
    if not toks:
        return None, ""
    return "d:" + " ".join(toks), (item.get("description") or "").strip()[:60]


def payee_matches(rule_key: Optional[str], line_key: Optional[str]) -> bool:
    if not rule_key or not line_key:
        return False
    if rule_key == line_key:
        return True
    if rule_key.startswith("d:") and line_key.startswith("d:"):
        a, b = frozenset(rule_key[2:].split()), frozenset(line_key[2:].split())
        return min(len(a), len(b)) >= 2 and jaccard(a, b) >= FUZZY_PAYEE_SIMILARITY
    return False
