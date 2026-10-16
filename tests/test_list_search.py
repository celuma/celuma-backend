"""Unit tests for the server half of the list search contract (Céluma 1.4, #7).

No database: these pin normalization, term classification and the accent map
that `translate()` uses. The SQL behaviour is covered end to end in
`tests/http/test_list_search_and_pagination.py`.
"""
import unicodedata

import pytest

from app.services.list_search import (
    ACCENTED,
    MAX_QUERY_LENGTH,
    MAX_TERMS,
    UNACCENTED,
    SearchQueryError,
    SearchSpec,
    SearchTerm,
    fuzzy_threshold,
    normalize_text,
    parse_search_query,
    search_condition,
    strip_separators,
)


def test_normalize_text_matches_the_client():
    assert normalize_text("Muñóz") == "munoz"
    assert normalize_text("PATOLOGÍA") == "patologia"


def test_precomposed_and_decomposed_accents_normalize_alike():
    assert normalize_text("P\u00e9rez") == normalize_text("Pe\u0301rez") == "perez"
    assert strip_separators("Mun\u0303o\u0301z") == "munoz"


@pytest.mark.parametrize("raw, stripped", [("CTM-18", "ctm18"), ("CU-011026", "cu011026"),
                                           ("cu 011026", "cu011026"), ("Dr. Pérez", "drperez")])
def test_strip_separators(raw, stripped):
    assert strip_separators(raw) == stripped


@pytest.mark.parametrize("query, expected", [
    ("CU276262", [SearchTerm("identifier", "cu276262")]),
    ("276262", [SearchTerm("identifier", "276262")]),
    ("cu-011026", [SearchTerm("identifier", "cu011026")]),
    ("Muñóz", [SearchTerm("text", "munoz")]),
    ("juan  P-12", [SearchTerm("text", "juan"), SearchTerm("identifier", "p12")]),
    ("  -- . ", []),
    ("", []),
    (None, []),
])
def test_terms_are_classified_by_the_presence_of_a_digit(query, expected):
    assert parse_search_query(query) == expected


# Query limits (R1). These boundary cases mirror
# celuma-frontend/src/test/lib/search.test.ts ("query limits"): the same query
# is accepted or rejected on both sides, and an accepted query keeps every term.
EIGHT_WORDS = "uno dos tres cuatro cinco seis siete ocho"


@pytest.mark.parametrize("query, terms", [
    (EIGHT_WORDS, 8),
    (EIGHT_WORDS + " -- ..", 8),  # punctuation-only terms do not count
    ("a" * MAX_QUERY_LENGTH, 1),
    ("  " + "a" * MAX_QUERY_LENGTH + "  ", 1),  # surrounding whitespace does not count
    ("e\u0301" * MAX_QUERY_LENGTH, 1),  # counted after NFC: 200 "é", not 400 code points
])
def test_queries_at_the_limits_are_accepted_whole(query, terms):
    assert len(parse_search_query(query)) == terms


@pytest.mark.parametrize("query, reason, actual", [
    (EIGHT_WORDS + " CU276262", "too_many_terms", 9),
    ("a" * (MAX_QUERY_LENGTH + 1), "too_long", MAX_QUERY_LENGTH + 1),
    ("e\u0301" * (MAX_QUERY_LENGTH + 1), "too_long", MAX_QUERY_LENGTH + 1),
])
def test_queries_over_a_limit_are_rejected_not_truncated(query, reason, actual):
    with pytest.raises(SearchQueryError) as excinfo:
        parse_search_query(query)
    assert (excinfo.value.reason, excinfo.value.actual) == (reason, actual)
    assert excinfo.value.limit == (MAX_TERMS if reason == "too_many_terms" else MAX_QUERY_LENGTH)


@pytest.mark.parametrize("length, budget", [(1, 0), (3, 0), (4, 1), (6, 1), (7, 2), (20, 2)])
def test_typo_budget_matches_the_client(length, budget):
    assert fuzzy_threshold(length) == budget


def test_accent_map_is_the_nfd_rule_for_latin_letters():
    """`translate()` must agree with "NFD, drop U+0300–U+036F" — the client's
    `normalizeText` — for every precomposed Latin letter U+00C0–U+024F."""
    accented, plain = [], []
    for cp in range(0xC0, 0x250):
        ch = chr(cp)
        base = "".join(c for c in unicodedata.normalize("NFD", ch) if not "̀" <= c <= "ͯ")
        if base != ch and len(base) == 1 and base.isascii():
            accented.append(ch)
            plain.append(base)
    assert ACCENTED == "".join(accented)
    assert UNACCENTED == "".join(plain)
    assert len(ACCENTED) == len(UNACCENTED)


def test_an_empty_query_adds_no_condition():
    spec = SearchSpec(scalars=())
    assert search_condition("", spec) is None
    assert search_condition("  ...  ", spec) is None
