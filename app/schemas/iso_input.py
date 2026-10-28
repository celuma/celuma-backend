"""Strict input shape for user-supplied dates (Céluma 1.4, issue #33)."""
import re
from datetime import datetime

from pydantic_core import PydanticCustomError

#: ISO 8601 / RFC 3339 date or date-time: `YYYY-MM-DD`, optionally followed by `T`/space, `HH:MM[:SS[.fraction]]`
#: and an optional `Z` or `±HH[:MM]` offset. Pydantic would also read a numeric STRING such as `1790000000` as a
#: Unix epoch (and `20260930` as a basic-format date); a calendar date must never be guessed from digits.
_ISO_SHAPE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d{1,9})?)?\s?(?:[Zz]|[+-]\d{2}(?::?\d{2})?)?)?"
)


def require_iso_date_input(value, *, message: str = "a date (YYYY-MM-DD) or ISO date-time is required"):
    """Accept a `datetime` or an ISO date/date-time string; refuse everything else (None, bool, numbers,
    blank, epoch or any other numeric string, free text) with the custom error `iso_date_required`.
    Calendar validity (month 13, Feb 30) is left to the datetime parser that runs next."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and _ISO_SHAPE.fullmatch(value.strip()):
        return value.strip()
    raise PydanticCustomError("iso_date_required", message)
