"""Field pointers for refused registrations (Céluma 1.5, #67).

When the unified order registration is refused, the web form keeps every value
the user entered and marks the field, and the sample row, to correct. Request
validation (422) already names the location of each problem (`loc`, e.g.
`["body", "samples", 1, "received_at"]`). The checks the route makes against
the database (branch, patient, physicians, study type, each sample's type, the
report template, the permission to type a sample code) raise plain
`HTTPException`s that only carry a sentence.

`pointing_to` keeps such an error's status code and message and adds:

    {"code": "<machine code>", "message": "<unchanged message>", "fields": ["samples.1.sample_type_id", ...]}

`fields` are dotted paths into the request body (sample rows by index). It
points only at the caller's own submitted fields, so it reveals nothing the
caller did not send; a type of another laboratory stays indistinguishable from
a missing one (both 404 `sample_type_not_found`).

Nothing about whether a request is refused changes: the same checks run in the
same order, before anything is written, and the transaction still rolls back,
so a refused request leaves no idempotency receipt and the corrected request
may reuse its key (ADR 0014, Amendment 1).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterable, Iterator, Sequence

from fastapi import HTTPException


class RegistrationFieldError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, fields: Sequence[str], headers=None):
        super().__init__(status_code, {"code": code, "message": message, "fields": list(fields)}, headers=headers)


@contextmanager
def pointing_to(code: str, fields: Iterable[str]) -> Iterator[None]:
    """Re-raise an `HTTPException` of the wrapped check with `fields` attached.

    A structured detail (`{"code", "message"}`) keeps its own code; a plain
    sentence takes `code`. An error that already points somewhere is left alone.
    """
    try:
        yield
    except RegistrationFieldError:
        raise
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            inner_code = detail.get("code") or code
            message = str(detail.get("message", ""))
        else:
            inner_code = code
            message = str(detail)
        raise RegistrationFieldError(exc.status_code, inner_code, message, list(fields), exc.headers) from exc


def sample_rows(samples: Sequence, predicate) -> list[int]:
    """Indexes of the submitted sample rows for which `predicate(row)` holds."""
    return [i for i, row in enumerate(samples) if predicate(row)]


def row_fields(indexes: Iterable[int], field: str) -> list[str]:
    return [f"samples.{i}.{field}" for i in indexes]
