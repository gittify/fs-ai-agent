"""Unit tests for the four fixes: leading "The", UPC leading-zero forms, singular/plural, name containment."""
import pytest

from recall_agent.matcher import (Item, LotInfo, assess, core_words, name_contained, product_close, product_hit, _stem)
from recall_agent.queryplan import build_plan, flip_number, upc_forms


def terms(item):
    return [(q.field, q.term) for q in build_plan(item)]


# ---- 1. a leading "The" is dropped from search words, but the matcher still calls it only "close"
def test_a_leading_the_is_dropped_from_the_search_words():
    assert core_words("The Golden Spoon Company") == ["Golden", "Spoon"]
    assert core_words("The") == ["The"]  # a name that is only "The" is left alone


def test_the_plan_finds_a_firm_filed_without_its_leading_the():
    got = terms(Item("The Golden Spoon Company", "supplier"))
    assert ("recalling_firm", "Golden Spoon") in got            # the suffix-stripped query now also drops "The"
    assert ("recalling_firm", "Golden") not in got              # "golden" is a generic first word: no one-word query
    assert ("recalling_firm", "Maple") in terms(Item("The Maple Ridge Dairy", "supplier"))  # a distinctive one is queried


def test_a_leading_the_stays_a_review_not_a_hold():
    item = Item("The Golden Spoon Company", "supplier", ["LOT 8841A"], lot_info=[LotInfo("LOT 8841A")])
    out = assess(item, [{"recall_number": "F-1", "recalling_firm": "Golden Spoon Co.", "code_info": "LOT 8841A"}])
    assert out["verdict"] == "possible"


# ---- 2. UPC forms
def test_upc_forms_cover_zero_dropped_and_gtin14():
    assert upc_forms("012345678905") == ["012345678905", "0 12345 67890 5", "12345678905", "00012345678905"]
    assert upc_forms("4006381333931") == ["4006381333931", "04006381333931"]
    assert upc_forms("00012345678905") == ["00012345678905", "12345678905"]  # deduped


def test_a_short_code_is_not_expanded_into_noise():
    assert upc_forms("0000123") == ["0000123"]  # fewer than 7 significant digits: no stripped or padded forms


# ---- 3. singular / plural
@pytest.mark.parametrize("word,other", [("chickpea", "chickpeas"), ("chickpeas", "chickpea"), ("cherry", "cherries"),
                                        ("cherries", "cherry"), ("potato", "potatoes"), ("potatoes", "potato"),
                                        ("glass", "glasses"), ("box", "boxes"), ("boxes", "box"), ("pea", "peas")])
def test_flip_number(word, other):
    assert flip_number(word) == other


def test_the_plan_queries_both_numbers_for_the_last_word_of_an_ingredient():
    got = [t for f, t in terms(Item("sweet potato", "ingredient")) if f == "product_description"]
    assert got == ["sweet potato", "sweet potatoes"]


@pytest.mark.parametrize("a,b", [("chickpea", "Organic chickpeas, 15 oz"), ("cherry", "Dried cherries, 8 oz"),
                                 ("almond", "Almonds, roasted"), ("sweet potato", "Sweet potatoes, fresh")])
def test_plural_forms_match_as_close_but_not_as_exact(a, b):
    assert product_close(a, b) and not product_hit(a, b)


@pytest.mark.parametrize("a,b", [("pea", "Pear juice, 12 oz"), ("rice", "Price cutter"), ("date", "Update kit"),
                                 ("bay leaf", "Bay leaves, whole")])
def test_the_stemmer_does_not_conflate_different_words(a, b):
    assert not product_close(a, b)  # the last is a known irregular-plural gap, listed in the stress set


def test_stemming_is_consistent_on_both_sides():
    assert _stem("peas") == _stem("pea") == "pea" and _stem("cherries") == _stem("cherry") == "cherry"
    assert _stem("glass") == "glass" and _stem("bus") == "bus"


def test_a_plural_match_with_our_lot_is_a_review_never_a_hold():
    item = Item("chickpea", "ingredient", ["CP-1001"], lot_info=[LotInfo("CP-1001")])
    out = assess(item, [{"recall_number": "F-1", "product_description": "Organic chickpeas", "code_info": "CP-1001"}])
    assert out["verdict"] == "possible" and out["matches"][0]["identity"] == "close"


# ---- 4. name containment ("dba" parents, affiliates)
@pytest.mark.parametrize("ours,theirs", [("Sunrise Foods", "Atlas Holdings LLC dba Sunrise Foods"),
                                         ("Maple Ridge Dairy", "Valley Holdings Inc d/b/a Maple Ridge Dairy"),
                                         ("Sunrise Foods Inc", "Sunrise Foods Distribution Group LLC"),
                                         ("Blue Heron Seafood Processing Co", "Blue Heron Seafood")])
def test_contained_names_are_close(ours, theirs):
    assert name_contained(ours, theirs) and name_contained(theirs, ours)


@pytest.mark.parametrize("ours,theirs", [("Sunrise Foods", "Sunrise Bakery LLC"), ("Acme Spice Traders", "Acme Dairy Farms"),
                                         ("Harvest Moon Foods", "Harvest Hill Farms"), ("Delta Spice Co", "Delta Airlines Catering")])
def test_sharing_a_word_is_not_containment(ours, theirs):
    assert not name_contained(ours, theirs)


def test_a_single_word_name_is_never_treated_as_contained():
    assert not name_contained("Globex", "Globex Imports Ltd")  # too ambiguous: "Acme" would match half the database


def test_containment_with_our_lot_is_a_review_never_a_hold():
    item = Item("Sunrise Foods Inc", "supplier", ["LOT 8841A"], lot_info=[LotInfo("LOT 8841A")])
    out = assess(item, [{"recall_number": "F-1", "recalling_firm": "Atlas Holdings LLC dba Sunrise Foods", "code_info": "LOT 8841A"}])
    assert out["verdict"] == "possible" and out["matches"][0]["identity"] == "close"
