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


def test_query_length_and_term_count_are_bounded():
    assert len(parse_search_query("a" * (MAX_QUERY_LENGTH + 50))[0].value) == MAX_QUERY_LENGTH
    assert len(parse_search_query(" ".join(f"t{i}" for i in range(MAX_TERMS + 5)))) == MAX_TERMS


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
