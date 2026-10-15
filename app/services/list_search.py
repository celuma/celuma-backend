"""List search contract — server half (Céluma 1.4, issues #6/#7).

The specification is `celuma-engineering/specs/lists/search-and-pagination.md`.
The client half is `celuma-frontend/src/lib/search.ts`; both must give a query
the same meaning, because a list may filter in the browser (small catalogs) or
in SQL (server-paginated lists) and a user cannot tell which.

Contract, in short:

* Normalization: Unicode NFD, combining marks U+0300–U+036F removed,
  lowercase. "Muñóz" → "munoz", whether its accents arrive precomposed (NFC)
  or as separate combining marks (NFD).
* A query is split on whitespace into terms; separators inside a term are
  ignored ("CU-011026" → "cu011026"); every term must match (AND), each term
  may match a different field (OR across fields), but always inside ONE field
  value — never across the boundary between two fields.
* Limits: at most MAX_QUERY_LENGTH characters and MAX_TERMS terms. A longer
  query is rejected (`SearchQueryError`, a 422 from the list endpoints), never
  truncated: dropping a term would silently widen an AND query.
* A term containing a digit is an **identifier** term. It matches only as a
  literal substring of a field with separators removed, so "CU276262" never
  matches "CU276263" and "276262" matches "CU276262", "BX276262" and
  "IHQ276262". Identifier terms are never typo-corrected.
* Any other term is **text**. It matches as a literal substring too, or — when
  it has at least 4 characters — against a single digit-free word of a field
  within a conservative edit distance (1 for 4–6 characters, 2 for 7+), e.g.
  "biosia" → "Biopsia". The edit distance is computed by PostgreSQL's
  `fuzzystrmatch.levenshtein_less_equal` (see the Céluma 1.4 migration and
  ADR 0011).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional, Sequence

from sqlalchemy import and_, case, exists, func, literal, or_, select
from sqlalchemy.sql.elements import ColumnElement

#: Precomposed Latin letters (U+00C0–U+024F) whose NFD form is one ASCII letter
#: plus combining marks, and that letter. `translate()` with these two strings
#: is the SQL equivalent of "NFD, then drop U+0300–U+036F" for every character
#: a laboratory name or code realistically contains. Generated with
#: `unicodedata`; `tests/test_list_search.py` re-derives and compares it.
ACCENTED = (
    "ÀÁÂÃÄÅÇÈÉÊËÌÍÎÏÑÒÓÔÕÖÙÚÛÜÝàáâãäåçèéêëìíîïñòóôõöùúûüýÿĀāĂăĄąĆćĈĉĊċČčĎďĒēĔĕĖėĘęĚě"
    "ĜĝĞğĠġĢģĤĥĨĩĪīĬĭĮįİĴĵĶķĹĺĻļĽľŃńŅņŇňŌōŎŏŐőŔŕŖŗŘřŚśŜŝŞşŠšŢţŤťŨũŪūŬŭŮůŰűŲųŴŵŶŷŸŹźŻżŽž"
    "ƠơƯưǍǎǏǐǑǒǓǔǕǖǗǘǙǚǛǜǞǟǠǡǦǧǨǩǪǫǬǭǰǴǵǸǹǺǻȀȁȂȃȄȅȆȇȈȉȊȋȌȍȎȏȐȑȒȓȔȕȖȗȘșȚțȞȟȦȧȨȩȪȫȬȭȮȯȰȱȲȳ"
)
UNACCENTED = (
    "AAAAAACEEEEIIIINOOOOOUUUUYaaaaaaceeeeiiiinooooouuuuyyAaAaAaCcCcCcCcDdEeEeEeEeEe"
    "GgGgGgGgHhIiIiIiIiIJjKkLlLlLlNnNnNnOoOoOoRrRrRrSsSsSsSsTtTtUuUuUuUuUuUuWwYyYZzZzZz"
    "OoUuAaIiOoUuUuUuUuUuAaAaGgKkOoOojGgNnAaAaAaEeEeIiIiOoOoRrRrUuUuSsTtHhAaEeOoOoOoOoYy"
)

#: Query limits, identical in `celuma-frontend/src/lib/search.ts`. They keep
#: one request's SQL bounded. A query over either limit is REJECTED, never
#: shortened: the API answers 422 and the UI says the search was not applied.
#: Length counts Unicode code points of the NFC-normalized, trimmed query;
#: terms count what remains after punctuation-only terms are dropped.
#:
#: The length limit also bounds typo tolerance: a term has at most 200
#: characters, so the words it is compared with have at most 202 — inside
#: `levenshtein_less_equal`'s 255-character limit. No separate cap applies.
MAX_QUERY_LENGTH = 200
MAX_TERMS = 8

#: Combining diacritical marks U+0300–U+036F, removed by the NFD rule.
COMBINING_MARKS = "[\u0300-\u036f]"

_COMBINING = re.compile(COMBINING_MARKS)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


class SearchQueryError(ValueError):
    """The query exceeds a limit; it must be rejected, not shortened."""

    def __init__(self, reason: Literal["too_long", "too_many_terms"], limit: int, actual: int):
        self.reason, self.limit, self.actual = reason, limit, actual
        what = "characters" if reason == "too_long" else "terms"
        super().__init__(f"Search query has {actual} {what}; at most {limit} are allowed")


def normalize_text(value: str) -> str:
    """Lowercase + strip diacritics (NFD). "Muñóz" → "munoz"."""
    return _COMBINING.sub("", unicodedata.normalize("NFD", value)).lower()


def strip_separators(value: str) -> str:
    """Normalize and drop every non-alphanumeric character. "CTM-18" → "ctm18"."""
    return _NON_ALNUM.sub("", normalize_text(value))


def fuzzy_threshold(length: int) -> int:
    """Conservative typo budget by term length (identical to the client)."""
    if length < 4:
        return 0
    if length <= 6:
        return 1
    return 2


@dataclass(frozen=True)
class SearchTerm:
    kind: Literal["identifier", "text"]
    #: The term normalized and stripped of separators.
    value: str


def search_query_length(query: str) -> int:
    """The length MAX_QUERY_LENGTH limits (code points, NFC, trimmed)."""
    return len(unicodedata.normalize("NFC", query).strip())


def parse_search_query(query: Optional[str]) -> list[SearchTerm]:
    """Split a raw query into identifier/text terms (see the module docstring).

    Raises `SearchQueryError` when the query exceeds MAX_QUERY_LENGTH or
    MAX_TERMS: every term of an accepted query is applied.
    """
    if not query:
        return []
    length = search_query_length(query)
    if length > MAX_QUERY_LENGTH:
        raise SearchQueryError("too_long", MAX_QUERY_LENGTH, length)
    terms: list[SearchTerm] = []
    for raw in normalize_text(query.strip()).split():
        value = strip_separators(raw)
        if not value:
            continue  # punctuation-only term → ignored
        terms.append(SearchTerm("identifier" if any(c.isdigit() for c in value) else "text", value))
    if len(terms) > MAX_TERMS:
        raise SearchQueryError("too_many_terms", MAX_TERMS, len(terms))
    return terms


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


def sql_normalized(expr) -> ColumnElement:
    """SQL twin of `normalize_text` for a text expression (NULL stays NULL).

    `translate()` folds precomposed letters ("é" U+00E9); `regexp_replace`
    then removes combining marks, so a decomposed "e" + U+0301 folds the same
    way — without it "Pe\u0301rez" kept the mark and split into the words
    "pe" and "rez". Both only matter for non-ASCII values, and `translate()`
    scans its 244-character map once per input character (measured ~10x
    slower than `lower()` alone over a large tenant), so ASCII values take the
    plain `lower()` branch.
    """
    folded = func.regexp_replace(func.translate(expr, ACCENTED, UNACCENTED), COMBINING_MARKS, "", "g")
    return case(
        (expr.op("~")("[^[:ascii:]]"), func.lower(folded)),
        else_=func.lower(expr),
    )


def sql_stripped(expr) -> ColumnElement:
    """SQL twin of `strip_separators`."""
    return func.regexp_replace(sql_normalized(expr), "[^a-z0-9]+", "", "g")


def sort_key(expr) -> ColumnElement:
    """Case- and accent-insensitive, environment-independent ordering key.

    `COLLATE "C"` on the normalized value makes the order identical on the
    developer's Postgres and RDS whatever their database collation; NULL sorts
    as the empty string, the way the client sorters treated a missing value.
    """
    return func.coalesce(sql_normalized(expr), "").collate("C")


def _literal_match(expr, term: SearchTerm) -> ColumnElement[bool]:
    # `term.value` is [a-z0-9]+, so it carries no LIKE wildcard to escape.
    return func.coalesce(sql_stripped(expr), "").contains(term.value)


def _fuzzy_word_match(expr, term: SearchTerm) -> Optional[ColumnElement[bool]]:
    """Some digit-free word of `expr` is within the typo budget of `term`."""
    k = fuzzy_threshold(len(term.value))
    if term.kind != "text" or k == 0:
        return None
    words = func.unnest(
        func.regexp_split_to_array(func.coalesce(sql_normalized(expr), ""), "[^a-z0-9]+")
    ).table_valued("word").render_derived()
    word = words.c.word
    lo, hi = len(term.value) - k, len(term.value) + k
    # CASE, not AND: SQL does not promise to evaluate the length guard first,
    # and levenshtein_less_equal rejects inputs longer than 255 characters.
    within = case(
        (
            and_(func.length(word).between(lo, hi), word.op("!~")("[0-9]")),
            func.levenshtein_less_equal(word, literal(term.value), k) <= k,
        ),
        else_=False,
    )
    return exists(select(1).select_from(words).where(within))


Predicate = Callable[[ColumnElement], ColumnElement[bool]]


@dataclass(frozen=True)
class SearchSpec:
    """The searchable values of one list row.

    `scalars` are text expressions of the row itself or of its to-one joins.
    `related` are builders for values that live in another table (patient and
    physician names, labels, assignees, reviewers). Each receives a predicate
    over one text expression of that table and returns a condition on the row,
    normally `row.fk IN (SELECT id FROM entity WHERE predicate(...))`: the
    entity's text is then normalized once per entity rather than once per list
    row, and a term is matched against each related value separately.
    """

    scalars: Sequence[ColumnElement]
    related: Sequence[Callable[[Predicate], ColumnElement[bool]]] = field(default_factory=tuple)


def _term_condition(term: SearchTerm, spec: SearchSpec) -> ColumnElement[bool]:
    parts: list[ColumnElement[bool]] = [_literal_match(expr, term) for expr in spec.scalars]
    # Word-level typo tolerance does not need field separation: joining the
    # scalar fields with a space keeps every word intact, and one split per row
    # is far cheaper than one per field.
    fuzzy = _fuzzy_word_match(func.concat_ws(" ", *spec.scalars), term) if spec.scalars else None
    if fuzzy is not None:
        parts.append(fuzzy)

    def related_predicate(expr) -> ColumnElement[bool]:
        options = [_literal_match(expr, term)]
        related_fuzzy = _fuzzy_word_match(expr, term)
        if related_fuzzy is not None:
            options.append(related_fuzzy)
        return or_(*options)

    parts.extend(build(related_predicate) for build in spec.related)
    return or_(*parts)


def search_condition(query: Optional[str], spec: SearchSpec) -> Optional[ColumnElement[bool]]:
    """The WHERE condition for `query`, or None when the query has no terms."""
    terms = parse_search_query(query)
    if not terms:
        return None
    return and_(*(_term_condition(term, spec) for term in terms))
