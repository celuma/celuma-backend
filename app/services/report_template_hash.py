"""Canonical fingerprint of a report template's clinical structure.

Céluma 1.3.1 Block C, finding C-8. One function, deliberately in its own module:
it is shared by two callers with nothing else in common, and neither is the right
home for it.

* `report_template_autoversion.snapshot_and_activate_template_version` uses it to
  decide whether a template save represents a real clinical change worth a new
  internal revision. That is where this logic was born, as the private
  `_hash_template_block`.
* `app/api/v1/reports.py::create_report` uses it as an **optimistic-concurrency
  fingerprint**: the editor receives the hash alongside the `template_json` it
  bootstraps with and echoes it back at save time, so the backend can refuse to
  freeze a clinical structure the author never worked against (C-8).

The two uses share one requirement — *"is this the same clinical structure?"* —
and must answer it identically, which is why there is exactly one implementation.

### What this hash is, and is not

It is an **integrity/equivalence fingerprint**. It is deliberately NOT:

* an authorization token — it proves nothing about the caller, and every tenant
  and permission check still runs independently. A hash is only ever compared
  against the template the caller has already been authorized to read;
* provenance — that is `ReportVersion.template_version_id`, and a report created
  through `template_id` legitimately has none (CEL-131-05). The hash is not
  persisted anywhere;
* a replacement for `ReportTemplateVersion` — it creates no row, reads no row,
  and restores only the *immutability-equivalent* guarantee the old pinned-version
  flow happened to provide, not the version concept itself.

It must stay **server-computed**. The frontend treats it as opaque and echoes it
back unchanged; it must never recompute it, least of all from its own normalized
editor template, which is intentionally not byte-equivalent to the stored column
(see docs/celuma-1.3.1/block-c/template-mutation-race.md §7).

### Why a content hash rather than a timestamp

`ReportTemplate` has no `updated_at` column, so a timestamp would require a
schema change. More importantly a timestamp would be **wrong**: an administrator
who opens a template and saves it unchanged would invalidate every open editor,
because the row was written even though the clinical structure did not move. The
hash is canonical over content, so a no-op save is a no-op here too.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional


def hash_clinical_template_block(template_block: Optional[Dict[str, Any]]) -> str:
    """Canonical SHA-256 of a template's clinical structure block.

    `sort_keys=True` makes the result independent of key ordering, so two
    structurally identical templates always hash the same regardless of how
    their JSON happened to be serialized. `default=str` keeps the function
    total: a stray non-JSON-serializable value (a `datetime` that found its way
    into `template_json`) degrades to its string form rather than raising, which
    matters because both callers are on paths that must not 500 over a template
    quirk.

    `None` and `{}` hash identically: an absent clinical structure and an empty
    one are the same thing to every caller.
    """
    canonical = json.dumps(template_block or {}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
