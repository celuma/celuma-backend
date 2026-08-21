#!/usr/bin/env python3
"""Refuse to start unless object storage is provably disposable.

Céluma 1.3, Phase 5, Block F §23. Block C declined to run C-002 (official PDF
end to end) and C-004 (browser E2E) because the API container was configured
with `S3_BUCKET_NAME=celuma-media-stg` and a real `AKIA…` key, so every write
those suites make would have landed in the staging bucket. Block D proved the
application already supports an isolated endpoint through `S3_ENDPOINT_URL`
alone — no application change needed — and left one requirement for this
block: make it *impossible*, not merely intended, for a validation run to
reach a real bucket.

This script is that requirement. `docker-compose.test.yml` runs it ahead of
`start.sh`, so a misconfigured stack dies at startup with a named reason
instead of authenticating somewhere real and quietly succeeding.

It reads the application's own `settings` rather than `os.environ`, so what is
checked is what `S3Service` would actually use — including the fallbacks in
`app/core/config.py`. A check against the raw environment would pass while the
effective configuration differed.

Deliberately not a pytest: it must run in the container's start-up path, where
there is no test runner and no conftest.

Exit codes: 0 all checks pass, 1 a check failed, 2 the settings could not be
imported at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Running `python scripts/assert_isolated_storage.py` puts `scripts/` on
# `sys.path[0]`, not the working directory, so `import app` would fail even
# from the repository root. Prepending the root keeps the guard invocable
# either way — which matters because it runs from a compose `command:`, where
# a subtle path assumption would surface as "the guard is broken" rather than
# "storage is not isolated".
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Substrings that mark a bucket as belonging to a real environment. Matched
#: case-insensitively against the effective bucket name. `celuma-media-test`
#: (the disposable bucket) contains none of them; `celuma-media-stg` and
#: `celuma-media-prod` each contain two.
FORBIDDEN_BUCKET_MARKERS = ("stg", "staging", "prod", "production")

#: A real AWS access key id starts with one of these. The disposable stack
#: uses `blockftest`, which starts with neither.
REAL_KEY_PREFIXES = ("AKIA", "ASIA")


class IsolationFailure(Exception):
    """A check that must hold for a validation run to be safe."""


def _check(condition: bool, ok: str, failure: str) -> None:
    if condition:
        print(f"[PASS] {ok}")
    else:
        raise IsolationFailure(failure)


def main() -> int:
    try:
        from app.core.config import settings
    except Exception as exc:  # noqa: BLE001 — this runs before the app exists
        print(f"[FAIL] could not import application settings: {exc}")
        return 2

    bucket = (settings.s3_bucket_name or "").strip()
    endpoint = (settings.s3_endpoint_url or "").strip()
    access_key = (settings.aws_access_key_id or "").strip()

    try:
        _check(
            bool(bucket),
            f"a bucket is configured — bucket={bucket}",
            "S3_BUCKET_NAME is empty; refusing to run with an unknown target",
        )

        offending = [m for m in FORBIDDEN_BUCKET_MARKERS if m in bucket.lower()]
        _check(
            not offending,
            f"the bucket is not a staging/production bucket — bucket={bucket}",
            f"S3_BUCKET_NAME={bucket!r} looks like a real environment "
            f"(matched {offending}); refusing to run",
        )

        _check(
            bool(endpoint),
            f"an isolated S3 endpoint override is configured — endpoint={endpoint}",
            "S3_ENDPOINT_URL is empty, so the client would resolve a real AWS "
            "endpoint; refusing to run",
        )

        _check(
            "amazonaws.com" not in endpoint.lower(),
            f"the endpoint is not an AWS endpoint — endpoint={endpoint}",
            f"S3_ENDPOINT_URL={endpoint!r} points at AWS; refusing to run",
        )

        _check(
            not access_key.upper().startswith(REAL_KEY_PREFIXES),
            # The prefix only — never the key. Four characters is enough to
            # show the check ran and not enough to be a credential.
            f"no real AWS access key is present — access_key_prefix="
            f"{access_key[:4] or '(unset)'}",
            "AWS_ACCESS_KEY_ID looks like a real AWS key; refusing to run",
        )
    except IsolationFailure as exc:
        print(f"[FAIL] {exc}")
        print(
            "\nSTORAGE ISOLATION NOT PROVEN — this stack will not start.\n"
            "Run the release-validation suites only under "
            "docker-compose.test.yml, which configures a disposable "
            "S3-compatible endpoint and dummy credentials."
        )
        return 1

    print("\nSTORAGE ISOLATION PROVEN — every write goes to the disposable endpoint.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
