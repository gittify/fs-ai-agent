"""The standard search plan: coverage that code guarantees, not the model.

The first live eval showed the model searches the exact name on file and stops. A recall filed under
"SUNRISE FOODS, LLC" or "Sunrise Food Company", or findable only by product or UPC, was never
retrieved, and nothing downstream can flag a recall that was never returned. So the code always runs
these queries for every item, and the model may add more:

  supplier     the name on file; the name without company suffixes (Inc, LLC, ...); each alias the same
               way; and the first distinctive word of the name, to catch variant spellings
  ingredient   the ingredient name and each alias, plus the other singular/plural form, against the
               product description
  any item     each UPC on file as given, grouped (UPC-A), without its leading zero, and as a 14-digit
               GTIN, against the product description, since a recall may list our product under another
               firm's name
  A leading "The" is dropped from the suffix-stripped and first-word queries.

Queries are cheap and the matcher filters the results, so over-fetching only costs a few requests.
Generic first words ("global", "natural", ...) are skipped because they return too much noise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .matcher import Item, core_words

MIN_FIRST_WORD = 4
GENERIC_FIRST_WORDS = {
    "global", "international", "natural", "fresh", "american", "national", "premium", "quality", "golden",
    "great", "best", "pure", "organic", "royal", "united", "world", "foods", "food", "brands", "company",
    "group", "trading", "imports", "import", "exports", "farms", "farm", "valley", "pacific", "atlantic",
}


@dataclass(frozen=True)
class Query:
    field: str
    term: str
    why: str


def term_key(term: str) -> tuple[str, ...]:
    """Case/punctuation-insensitive identity of a search term (used to dedupe and to track retries)."""
    return tuple(re.findall(r"[a-z0-9]+", (term or "").lower()))


def upc_forms(upc: str) -> list[str]:
    """The ways a UPC/GTIN is commonly printed: as given, UPC-A grouped 0 12345 67890 5, without its
    leading zero (spreadsheets and some labels drop it), and zero-padded to a 14-digit GTIN."""
    sig = upc.lstrip("0")
    forms = [upc]
    if len(upc) == 12:
        forms.append(f"{upc[0]} {upc[1:6]} {upc[6:11]} {upc[11]}")
    if len(sig) >= 7:
        forms += [sig, sig.zfill(14)]
    return list(dict.fromkeys(forms))


def flip_number(word: str) -> str:
    """The other singular/plural form of a word, by simple rules (an extra query that finds nothing is
    harmless; a missing one is not). Irregular plurals (leaf/leaves) are not handled."""
    lw = word.lower()
    if lw.endswith("ies") and len(lw) > 4:
        return word[:-3] + "y"
    if lw.endswith(("oes", "ses", "xes", "zes", "ches", "shes")) and len(lw) > 4:
        return word[:-2]
    if lw.endswith("ss"):
        return word + "es"
    if lw.endswith("s") and len(lw) > 3:
        return word[:-1]
    if lw.endswith("y") and len(lw) > 2 and lw[-2] not in "aeiou":
        return word[:-1] + "ies"
    if lw.endswith(("s", "x", "z", "ch", "sh", "o")):
        return word + "es"
    return word + "s"


def build_plan(item: Item) -> list[Query]:
    plan: list[Query] = []
    seen: set = set()

    def add(field: str, term: str, why: str) -> None:
        key = (field, term_key(term))
        if key[1] and key not in seen:
            seen.add(key)
            plan.append(Query(field, " ".join(term.split()), why))

    names = [(item.name, "name on file")] + [(a, "alias") for a in item.aliases]
    if item.kind == "supplier":
        for name, why in names:
            add("recalling_firm", name, why)
            add("recalling_firm", " ".join(core_words(name)), f"{why}, without company suffix")
        first = core_words(item.name)
        if len(first) >= 2 and len(first[0]) >= MIN_FIRST_WORD and first[0].lower() not in GENERIC_FIRST_WORDS:
            add("recalling_firm", first[0], "first word of the name (catches variant spellings)")
    else:
        for name, why in names:
            add("product_description", name, f"ingredient {why}")
            words = name.split()
            if words:  # "chickpea" is filed as "chickpeas", and the reverse
                add("product_description", " ".join(words[:-1] + [flip_number(words[-1])]),
                    f"ingredient {why}, other singular/plural form")
    for upc in sorted({li.upc for li in item.lot_info if li.upc}):
        for form in upc_forms(upc):
            add("product_description", form, "UPC in the recall text")
    return plan
