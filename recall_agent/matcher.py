"""Deterministic recall matching.

This runs beside the model, not inside it. The model never sees its verdict;
guardrails in agent.py use it to catch the model being wrong in either
direction (calling a lookalike firm a hit, or waving through a real one).

Levels per recall record (identity is exact when the firm or product matches exactly, or when
an inventory UPC appears in the recall text):
    strong    identity matches exactly AND a lot we hold appears in code_info
    possible  identity matches exactly or closely, but no confirmed lot overlap
    none      neither
Overall verdict is the strongest level across all records seen for the item.

Tuning knob: an exact supplier match with no lot overlap lands in "possible",
so it goes to the coordinator. If that is too noisy for real use, add a level
for "recall found, none of our lots affected" that auto-logs instead.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

SUFFIXES = {"inc", "llc", "ltd", "co", "corp", "corporation", "company",
            "incorporated", "limited", "lp", "llp"}
STOP = {"and", "the", "of", "a", "oz", "lb", "pack", "jar", "jars", "bag", "bags"}


@dataclass
class LotInfo:
    """One line of inventory. code may be blank for item-level SKU/UPC info with no lot recorded."""
    code: str
    sku: str = ""
    upc: str = ""  # digits only
    qty_received: float | int | None = None
    qty_on_hand: float | int | None = None


@dataclass
class Item:
    name: str
    kind: str  # "supplier" (match on recalling firm) or "ingredient" (match on product text)
    lots: list[str] = field(default_factory=list)  # lot codes currently on hand
    aliases: list[str] = field(default_factory=list)
    contact: str = ""  # who gets the hold notice (used by the SES step)
    lot_info: list[LotInfo] = field(default_factory=list)  # SKU/UPC/quantities per lot


def _tokens(s: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", (s or "").lower().replace("&", " and "))
    return [t for t in toks if t not in SUFFIXES]


def core_tokens(name: str) -> list[str]:
    """Lowercased words of a name with company suffixes (Inc, LLC, ...) dropped, for matching."""
    return _tokens(name)


def core_words(name: str) -> list[str]:
    """The same words with their original casing, for readable search terms. Punctuation such as "&" is
    dropped, as it is in the FDA's own tokenization, so a phrase search still lines up."""
    words = [w for w in re.findall(r"[A-Za-z0-9]+", name or "") if w.lower() not in SUFFIXES]
    if len(words) > 1 and words[0].lower() == "the":  # "The Golden Spoon Co" is filed as "Golden Spoon Co."
        words = words[1:]
    return words


def firm_score(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    jaccard = len(set(ta) & set(tb)) / len(set(ta) | set(tb))
    ratio = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    return max(jaccard, ratio)


def _lot_norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


lot_norm = _lot_norm  # public name for other modules


def lot_overlap(lots: list[str], code_info: str) -> list[str]:
    hay = _lot_norm(code_info)
    return [lot for lot in lots if len(_lot_norm(lot)) >= 4 and _lot_norm(lot) in hay]


def _digit_runs(text: str) -> set[str]:
    return {re.sub(r"\D", "", m) for m in re.findall(r"\d(?:[\d \-]{5,}\d)", text or "")}


def upc_hit(item: Item, rec: dict) -> str:
    """The inventory UPC found in the recall's product text or code info, or "".

    Compared with leading zeros stripped, so UPC-A, EAN-13 and GTIN-14 forms of the same
    code match. Needs 7+ significant digits so short numbers can't match by accident.
    """
    upcs = {li.upc for li in item.lot_info if li.upc}
    if not upcs:
        return ""
    runs = {r.lstrip("0") for r in _digit_runs(f"{rec.get('product_description') or ''}\n{rec.get('code_info') or ''}")}
    return next((u for u in sorted(upcs) if len(u.lstrip("0")) >= 7 and u.lstrip("0") in runs), "")


def product_hit(name: str, description: str) -> bool:
    want = {t for t in _tokens(name) if t not in STOP}
    have = set(_tokens(description))
    return bool(want) and want <= have


def _stem(t: str) -> str:
    """Light plural stemmer, applied to BOTH sides of a comparison, so it only has to be consistent."""
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and t.endswith(("oes", "ses", "xes", "zes", "ches", "shes")):
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def product_close(name: str, description: str) -> bool:
    """Same ingredient up to singular/plural ("chickpea" vs "chickpeas"). Never 'exact', so it can only
    lead to a review, not a hold."""
    want = {_stem(t) for t in _tokens(name) if t not in STOP}
    have = {_stem(t) for t in _tokens(description)}
    return bool(want) and want <= have


def name_contained(a: str, b: str) -> bool:
    """One firm name appears as a contiguous run of words inside the other (shorter side 2+ words):
    "Sunrise Foods" inside "Atlas Holdings LLC dba Sunrise Foods". Plausible, never confirmed."""
    ta, tb = _tokens(a), _tokens(b)
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(short) >= 2 and any(long_[i:i + len(short)] == short for i in range(len(long_) - len(short) + 1))


def _identity(item: Item, rec: dict) -> str:
    names = [item.name, *item.aliases]
    if item.kind == "ingredient":
        desc = rec.get("product_description", "")
        if any(product_hit(n, desc) for n in names):
            return "exact"
        return "close" if any(product_close(n, desc) for n in names) else "none"
    firm = rec.get("recalling_firm", "")
    best = max((firm_score(n, firm) for n in names), default=0.0)
    if best >= 1.0:
        return "exact"
    return "close" if best >= 0.75 or any(name_contained(n, firm) for n in names) else "none"


def assess(item: Item, records: list[dict]) -> dict:
    order = {"none": 0, "possible": 1, "strong": 2}
    matches, verdict = [], "none"
    for rec in records:
        upc = upc_hit(item, rec)
        ident = "exact" if upc else _identity(item, rec)
        if ident == "none":
            continue
        lots = lot_overlap(item.lots, rec.get("code_info", ""))
        level = "strong" if (ident == "exact" and lots) else "possible"
        matches.append({"recall_number": rec.get("recall_number"),
                        "classification": rec.get("classification"),
                        "identity": ident, "upc_match": upc, "lot_overlap": lots, "level": level})
        if order[level] > order[verdict]:
            verdict = level
    return {"verdict": verdict, "matches": matches}
