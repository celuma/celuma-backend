"""Inventory of every HTTP route the app serves, for tests that assert on it.

Up to FastAPI 0.116.x `app.routes` was a flat list where every entry had a
`.path`. Newer releases keep each `include_router(...)` as one grouped entry
(an internal `_IncludedRouter` with no `.path`) and resolve its routes lazily,
so reading `route.path` raises and `hasattr(route, "path")` silently skips
every included router, which is almost the whole API.

`iter_routes` flattens both shapes into `(full_path, methods)` pairs and
includes routes hidden from the OpenAPI schema. It fails loudly on an entry it
cannot read instead of skipping it, so an assertion such as "no print endpoint
exists" can never pass by looking at nothing.
"""
from __future__ import annotations

from typing import Iterator


def iter_routes(app) -> Iterator[tuple[str, frozenset[str]]]:
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if callable(contexts):
            for context in contexts():
                yield context.path, frozenset(getattr(context, "methods", None) or ())
        elif hasattr(route, "path"):
            yield route.path, frozenset(getattr(route, "methods", None) or ())
        else:
            raise AssertionError(f"route inventory cannot read {route!r}")


def route_paths(app) -> set[str]:
    return {path for path, _ in iter_routes(app)}
