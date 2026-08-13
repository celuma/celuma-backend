#!/usr/bin/env python3
"""Capture a normalized, comparable snapshot of a PostgreSQL schema.

Written for the Céluma 1.3 pre-Phase-5 migration squash, whose load-bearing
claim is that the squashed `v1_3_0` produces exactly the schema the
`v1_3_0 -> v1_10_0 -> v1_11_0 -> v1_12_0 -> v1_13_0` chain produced. Proving
that needs a schema description that is (a) complete enough to catch a
dropped constraint or a changed default and (b) stable enough that two runs
against two identical databases compare equal byte for byte.

What is captured, per public table:

    columns      name, ordinal position, type (with length/precision),
                 nullability, column default
    constraints  name -> `pg_get_constraintdef` output (PK, FK, UNIQUE, CHECK)
    indexes      name -> `pg_indexes.indexdef` output

`pg_get_constraintdef` and `indexdef` are PostgreSQL's own normalized
renderings of a parsed definition, which is exactly the property this needs:
a CHECK written inline in `CREATE TABLE` and the same CHECK added later by
`ALTER TABLE ... ADD CONSTRAINT` render identically, so a squash that builds
the final form directly compares equal to a chain that reached it in steps.

Deliberately NOT captured, because they are volatile and would produce
differences that mean nothing:

    OIDs, relfilenodes, table/index sizes, row counts, planner statistics,
    the `alembic_version` table (its whole point is to differ — the squash
    changes the revision identity and nothing else), and physical/on-disk
    ordering of constraints and indexes (both are emitted as sorted maps).

Usage:

    python scripts/capture_schema_snapshot.py --database celuma_migration_test \
        --output tests/fixtures/schema/v1_13_0_pre_squash_schema.json

The database is read only — this script issues no DDL and no DML.
"""
from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402


#: Alembic's bookkeeping table. Excluded on purpose: the squash changes the
#: revision id it holds, which is the one difference the equivalence check
#: expects rather than forbids.
_EXCLUDED_TABLES = ("alembic_version",)


_COLUMNS_SQL = """
    SELECT
        c.table_name,
        c.column_name,
        c.ordinal_position,
        c.data_type,
        c.character_maximum_length,
        c.numeric_precision,
        c.numeric_scale,
        c.datetime_precision,
        c.is_nullable,
        c.column_default
    FROM information_schema.columns c
    JOIN information_schema.tables t
      ON t.table_schema = c.table_schema
     AND t.table_name = c.table_name
     AND t.table_type = 'BASE TABLE'
    WHERE c.table_schema = 'public'
      AND c.table_name <> ALL(:excluded)
    ORDER BY c.table_name, c.ordinal_position
"""

#: `conrelid::regclass` renders the table name; `pg_get_constraintdef` renders
#: the normalized definition. Both are text, neither carries an OID.
_CONSTRAINTS_SQL = """
    SELECT
        rel.relname AS table_name,
        con.conname AS constraint_name,
        con.contype AS constraint_type,
        pg_get_constraintdef(con.oid) AS definition
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid = rel.relnamespace
    WHERE ns.nspname = 'public'
      AND rel.relname <> ALL(:excluded)
    ORDER BY rel.relname, con.conname
"""

_INDEXES_SQL = """
    SELECT tablename AS table_name, indexname, indexdef
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename <> ALL(:excluded)
    ORDER BY tablename, indexname
"""


def capture(database: str) -> dict:
    """Return the normalized schema of `database` as a JSON-ready dict."""
    url = make_url(settings.database_url).set(database=database)
    engine = create_engine(url)
    params = {"excluded": list(_EXCLUDED_TABLES)}

    try:
        with engine.connect() as conn:
            columns = conn.execute(text(_COLUMNS_SQL), params).mappings().all()
            constraints = conn.execute(text(_CONSTRAINTS_SQL), params).mappings().all()
            indexes = conn.execute(text(_INDEXES_SQL), params).mappings().all()
    finally:
        engine.dispose()

    tables: dict[str, dict] = {}

    for row in columns:
        table = tables.setdefault(
            row["table_name"], {"columns": {}, "constraints": {}, "indexes": {}}
        )
        table["columns"][row["column_name"]] = {
            "position": row["ordinal_position"],
            "type": row["data_type"],
            "max_length": row["character_maximum_length"],
            "numeric_precision": row["numeric_precision"],
            "numeric_scale": row["numeric_scale"],
            "datetime_precision": row["datetime_precision"],
            "nullable": row["is_nullable"] == "YES",
            "default": row["column_default"],
        }

    for row in constraints:
        table = tables.setdefault(
            row["table_name"], {"columns": {}, "constraints": {}, "indexes": {}}
        )
        table["constraints"][row["constraint_name"]] = {
            "type": row["constraint_type"],
            "definition": row["definition"],
        }

    for row in indexes:
        table = tables.setdefault(
            row["table_name"], {"columns": {}, "constraints": {}, "indexes": {}}
        )
        table["indexes"][row["indexname"]] = row["indexdef"]

    return {"tables": dict(sorted(tables.items()))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        required=True,
        help="Database name to read (host/credentials come from DATABASE_URL).",
    )
    parser.add_argument(
        "--output",
        help="Write JSON here instead of stdout.",
    )
    args = parser.parse_args()

    snapshot = capture(args.database)
    rendered = json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False)

    if args.output:
        import pathlib

        path = pathlib.Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
        table_count = len(snapshot["tables"])
        print(f"Captured {table_count} tables from {args.database} -> {args.output}")
    else:
        print(rendered)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
