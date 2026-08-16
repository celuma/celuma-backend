"""Integrity snapshots and post-migration verification (Phase 5 Block B).

TEST TOOLING ONLY. Never imported from `app/`.

Two halves:

`snapshot(conn)` captures a deterministic integrity fingerprint -- counts,
per-tenant counts and relationship hashes, never row contents. It is taken
before and after the migration; the difference is the evidence for §17's
"clinical rows preserved" claim. IDs and aggregates only, so no PHI is copied
even though the synthetic data contains none.

`verify(conn, pre)` runs the post-migration checks: tenant isolation, storage
attribution, the usage baseline, notification/threshold safety, FK and orphan
integrity, and seat semantics.

On independence
---------------
`recompute_billable_bytes` deliberately does NOT reuse the migration's
eight-CTE aggregate. It re-derives each tenant's billable total in Python from
the Block C contract, accumulating object-by-object. That buys two things the
SQL cannot check against itself:

  * a genuinely separate implementation of the tenant-logo suffix rule
    (Python string handling, not `right()`/`split_part()`); and
  * a distinct-object total alongside the per-category total, which exposes
    any storage object reachable through two billable categories. The frozen
    contract sums per category, so the two agreeing is a property worth
    measuring rather than assuming.
"""
from __future__ import annotations

from typing import Any

#: Tables that existed before 1.3 and must survive the migration untouched.
CLINICAL_TABLES = [
    "tenant", "branch", "app_user", "patient", "order", "sample", "report",
    "report_version", "storage_object", "sample_image", "sample_image_rendition",
    "requesting_physician", "study_type", "report_template", "report_section",
    "report_review", "invoice", "invoice_item", "payment", "price_catalog",
    "order_event", "audit_log", "order_comment", "label", "assignment",
    "user_branch", "user_invitation", "user_role", "role", "permission",
    "role_permission", "blacklisted_token", "password_reset_token",
]

#: Created by v1_3_0. Present only after the migration.
NEW_1_3_TABLES = [
    "report_template_version", "report_letterhead", "report_letterhead_version",
    "notification", "notification_recipient", "notification_delivery",
    "notification_preference", "tenant_usage", "tenant_limits",
    "tenant_usage_reconciliation", "tenant_usage_threshold_state",
]


def _scalar(conn, sql: str, args: tuple = ()) -> Any:
    with conn.cursor() as cur:
        # Passing an empty args tuple would make psycopg2 treat the `%` in
        # every LIKE pattern below as a parameter placeholder.
        cur.execute(sql, args) if args else cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else None


def _rows(conn, sql: str, args: tuple = ()) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, args) if args else cur.execute(sql)
        return cur.fetchall()


def _table_exists(conn, table: str) -> bool:
    return _scalar(conn, "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))


def snapshot(conn) -> dict:
    """A deterministic integrity fingerprint of the database.

    Relationship hashes are `md5` over ordered id-pairs: two databases with
    the same hash have the same rows joined to the same parents, which is the
    property §17 needs and which raw counts alone would not catch (a migration
    that swapped two tenants' patients preserves every count).
    """
    snap: dict[str, Any] = {"counts": {}, "per_tenant": {}, "hashes": {}}

    for table in CLINICAL_TABLES:
        if _table_exists(conn, table):
            quoted = f'"{table}"' if table == "order" else table
            snap["counts"][table] = _scalar(conn, f"SELECT COUNT(*) FROM {quoted}")

    for table in NEW_1_3_TABLES:
        if _table_exists(conn, table):
            snap["counts"][table] = _scalar(conn, f"SELECT COUNT(*) FROM {table}")

    # Per-tenant clinical counts: cross-tenant reassignment shows up here even
    # when the global count is unchanged.
    for table in ("patient", "order", "sample", "report", "app_user", "branch",
                  "sample_image"):
        quoted = f'"{table}"' if table == "order" else table
        snap["per_tenant"][table] = {
            str(t): c for t, c in
            _rows(conn, f"SELECT tenant_id, COUNT(*) FROM {quoted} GROUP BY tenant_id")
        }

    # Relationship fingerprints.
    #
    # Each is the sum of a 32-bit slice of every row's md5. That is
    # order-independent without an ORDER BY, so it runs as a single streaming
    # aggregate -- no sort, no temp file. `string_agg(... ORDER BY id)` over
    # 650k storage objects spills to disk and was observed exhausting the
    # container's filesystem; this form does not.
    #
    # It is a checksum, not a cryptographic commitment: it detects the
    # reassignments and losses this block tests for (any changed parent id
    # changes the sum), and nothing here depends on it resisting a crafted
    # collision.
    def fingerprint(expression: str, table: str) -> str:
        return _scalar(conn, f"""
            SELECT COALESCE(SUM(
                ('x' || substr(md5({expression}), 1, 8))::bit(32)::bigint
            ), 0)::text FROM {table}""")

    snap["hashes"]["patient_tenant"] = fingerprint(
        "id::text || '>' || tenant_id::text || '/' || branch_id::text", "patient")
    snap["hashes"]["order_patient"] = fingerprint(
        "id::text || '>' || COALESCE(patient_id::text, '-') || '/' || tenant_id::text",
        '"order"')
    snap["hashes"]["sample_order"] = fingerprint(
        "id::text || '>' || order_id::text || '/' || tenant_id::text", "sample")
    snap["hashes"]["report_order"] = fingerprint(
        "id::text || '>' || order_id::text || '/' || tenant_id::text", "report")
    snap["hashes"]["version_report"] = fingerprint(
        "id::text || '>' || report_id::text || '/' || version_no::text", "report_version")
    snap["hashes"]["user_tenant"] = fingerprint(
        "id::text || '>' || tenant_id::text || '/' || is_active::text", "app_user")
    snap["hashes"]["image_sample"] = fingerprint(
        "id::text || '>' || sample_id::text || '/' || storage_id::text", "sample_image")
    # storage_object identity excludes tenant_id on purpose: the migration is
    # expected to change that column and nothing else.
    snap["hashes"]["storage_identity"] = fingerprint(
        "id::text || '>' || object_key || '/' || COALESCE(size_bytes, -1)::text",
        "storage_object")

    snap["storage_total_bytes"] = _scalar(
        conn, "SELECT COALESCE(SUM(size_bytes), 0) FROM storage_object")
    snap["db_size_bytes"] = _scalar(conn, "SELECT pg_database_size(current_database())")
    return snap


# --------------------------------------------------------------------------
# Independent billable-usage recomputation
# --------------------------------------------------------------------------

BILLABLE_CATEGORIES = (
    "sample_image", "sample_rendition", "official_pdf", "legacy_pdf",
    "report_json", "tenant_logo", "letterhead_asset", "signature",
)


def _resolve_logo_in_python(conn) -> dict[str, tuple[str, int]]:
    """Resolve each tenant's current logo without using the migration's SQL.

    The contract: among the tenant's own storage objects under its logo key
    prefix, the one whose `object_key` is the suffix of the stored `logo_url`
    (after stripping any query string or fragment). Exactly one candidate
    resolves; zero or several leave the tenant unresolved.
    """
    tenants = _rows(conn, "SELECT id, logo_url FROM tenant WHERE logo_url IS NOT NULL")
    candidates = _rows(conn, """
        SELECT tenant_id, id, object_key, COALESCE(size_bytes, 0)
        FROM storage_object
        WHERE tenant_id IS NOT NULL AND object_key LIKE 'tenants/%/logo/%'
    """)
    by_tenant: dict[str, list[tuple]] = {}
    for tid, so_id, key, size in candidates:
        by_tenant.setdefault(str(tid), []).append((so_id, key, size))

    resolved: dict[str, tuple[str, int]] = {}
    for tid, url in tenants:
        clean = url.split("#", 1)[0].split("?", 1)[0]
        matches = [
            (so_id, size)
            for so_id, key, size in by_tenant.get(str(tid), [])
            if clean.endswith("/" + key)
        ]
        if len(matches) == 1:
            resolved[str(tid)] = (str(matches[0][0]), matches[0][1])
    return resolved


def recompute_billable_bytes(conn) -> dict[str, dict]:
    """Per-tenant billable bytes, re-derived in Python from the Block C contract.

    Returns `{tenant_id: {"by_category": {...}, "total": int,
    "distinct_total": int, "objects": int}}`. `total` follows the frozen
    contract (sum per category). `distinct_total` counts each storage object
    once regardless of how many categories reach it; the two differing would
    mean a real double-count, which is why both are reported.
    """
    per_tenant: dict[str, dict] = {}
    seen: dict[str, dict[str, int]] = {}

    def add(tenant_id, category: str, object_id, size) -> None:
        tid = str(tenant_id)
        entry = per_tenant.setdefault(
            tid, {"by_category": {c: 0 for c in BILLABLE_CATEGORIES},
                  "total": 0, "distinct_total": 0, "objects": 0})
        entry["by_category"][category] += size or 0
        entry["total"] += size or 0
        objects = seen.setdefault(tid, {})
        if str(object_id) not in objects:
            objects[str(object_id)] = size or 0
            entry["distinct_total"] += size or 0
            entry["objects"] += 1

    queries = {
        "sample_image": """
            SELECT si.tenant_id, so.id, so.size_bytes
            FROM sample_image si JOIN storage_object so ON so.id = si.storage_id""",
        "sample_rendition": """
            SELECT si.tenant_id, so.id, so.size_bytes
            FROM sample_image_rendition sir
            JOIN sample_image si ON si.id = sir.sample_image_id
            JOIN storage_object so ON so.id = sir.storage_id""",
        "official_pdf": """
            SELECT tenant_id, id, size_bytes FROM storage_object
            WHERE tenant_id IS NOT NULL AND sha256_hex IS NOT NULL""",
        "legacy_pdf": """
            SELECT r.tenant_id, so.id, so.size_bytes
            FROM report_version rv
            JOIN report r ON r.id = rv.report_id
            JOIN storage_object so ON so.id = rv.pdf_storage_id
            WHERE so.sha256_hex IS NULL""",
        "report_json": """
            SELECT r.tenant_id, so.id, so.size_bytes
            FROM report_version rv
            JOIN report r ON r.id = rv.report_id
            JOIN storage_object so ON so.id = rv.json_storage_id""",
        "letterhead_asset": """
            SELECT tenant_id, id, size_bytes FROM storage_object
            WHERE tenant_id IS NOT NULL
              AND (object_key LIKE 'report-letterheads/%'
                   OR object_key LIKE 'report-templates/%')""",
        "signature": """
            SELECT u.tenant_id, so.id, so.size_bytes
            FROM app_user u JOIN storage_object so ON so.id = u.signature_storage_id""",
    }
    for category, sql in queries.items():
        for tenant_id, object_id, size in _rows(conn, sql):
            add(tenant_id, category, object_id, size)

    for tid, (object_id, size) in _resolve_logo_in_python(conn).items():
        add(tid, "tenant_logo", object_id, size)

    # Tenants with no billable object at all must still appear, with zero --
    # that is the §23 invariant the comparison has to be able to see.
    for (tid,) in _rows(conn, "SELECT id FROM tenant"):
        per_tenant.setdefault(
            str(tid), {"by_category": {c: 0 for c in BILLABLE_CATEGORIES},
                       "total": 0, "distinct_total": 0, "objects": 0})
    return per_tenant


# --------------------------------------------------------------------------
# Post-migration verification
# --------------------------------------------------------------------------

def verify(conn, pre: dict) -> dict:
    """Every post-migration check Block B owes the release, as one dict."""
    post = snapshot(conn)
    out: dict[str, Any] = {"post_snapshot": post}

    # -- §17 clinical preservation ----------------------------------------
    preservation = {}
    for table, before in pre["counts"].items():
        after = post["counts"].get(table)
        preservation[table] = {"pre": before, "post": after,
                               "delta": (after - before) if after is not None else None}
    out["clinical_preservation"] = preservation
    out["rows_lost"] = {t: v["delta"] for t, v in preservation.items()
                        if v["delta"] is not None and v["delta"] < 0}

    out["relationship_hashes_stable"] = {
        k: (pre["hashes"].get(k) == post["hashes"].get(k)) for k in pre["hashes"]
    }

    # -- §18 tenant isolation ---------------------------------------------
    # Each query returns the number of rows in a state that must be impossible.
    isolation_queries = {
        "patient_branch_tenant_mismatch": """
            SELECT COUNT(*) FROM patient p JOIN branch b ON b.id = p.branch_id
            WHERE b.tenant_id <> p.tenant_id""",
        "order_patient_tenant_mismatch": """
            SELECT COUNT(*) FROM "order" o JOIN patient p ON p.id = o.patient_id
            WHERE p.tenant_id <> o.tenant_id""",
        "sample_order_tenant_mismatch": """
            SELECT COUNT(*) FROM sample s JOIN "order" o ON o.id = s.order_id
            WHERE o.tenant_id <> s.tenant_id""",
        "report_order_tenant_mismatch": """
            SELECT COUNT(*) FROM report r JOIN "order" o ON o.id = r.order_id
            WHERE o.tenant_id <> r.tenant_id""",
        "version_report_tenant_mismatch": """
            SELECT COUNT(*) FROM report_version rv JOIN report r ON r.id = rv.report_id
            JOIN "order" o ON o.id = r.order_id WHERE o.tenant_id <> r.tenant_id""",
        "sample_image_tenant_mismatch": """
            SELECT COUNT(*) FROM sample_image si JOIN sample s ON s.id = si.sample_id
            WHERE s.tenant_id <> si.tenant_id""",
        "user_branch_tenant_mismatch": """
            SELECT COUNT(*) FROM user_branch ub
            JOIN app_user u ON u.id = ub.user_id
            JOIN branch b ON b.id = ub.branch_id
            WHERE b.tenant_id <> u.tenant_id""",
        # The attribution the migration itself performed, checked against the
        # relational parent rather than against the migration's own SQL.
        "storage_vs_sample_image_tenant_mismatch": """
            SELECT COUNT(*) FROM storage_object so JOIN sample_image si ON si.storage_id = so.id
            WHERE so.tenant_id IS NOT NULL AND so.tenant_id <> si.tenant_id""",
        "storage_vs_rendition_tenant_mismatch": """
            SELECT COUNT(*) FROM storage_object so
            JOIN sample_image_rendition sir ON sir.storage_id = so.id
            JOIN sample_image si ON si.id = sir.sample_image_id
            WHERE so.tenant_id IS NOT NULL AND so.tenant_id <> si.tenant_id""",
        "storage_vs_report_tenant_mismatch": """
            SELECT COUNT(*) FROM storage_object so
            JOIN report_version rv ON rv.json_storage_id = so.id OR rv.pdf_storage_id = so.id
            JOIN report r ON r.id = rv.report_id
            WHERE so.tenant_id IS NOT NULL AND so.tenant_id <> r.tenant_id""",
        "storage_vs_signature_tenant_mismatch": """
            SELECT COUNT(*) FROM storage_object so JOIN app_user u ON u.signature_storage_id = so.id
            WHERE so.tenant_id IS NOT NULL AND so.tenant_id <> u.tenant_id""",
        "tenant_logo_cross_tenant": """
            SELECT COUNT(*) FROM tenant t JOIN storage_object so ON so.id = t.logo_storage_id
            WHERE so.tenant_id IS DISTINCT FROM t.id""",
        "notification_recipient_cross_tenant": """
            SELECT COUNT(*) FROM notification_recipient nr
            JOIN notification n ON n.id = nr.notification_id
            JOIN app_user u ON u.id = nr.user_id
            WHERE u.tenant_id <> n.tenant_id""",
    }
    out["tenant_isolation"] = {
        name: _scalar(conn, sql) for name, sql in isolation_queries.items()
    }
    out["cross_tenant_mismatches"] = sum(out["tenant_isolation"].values())

    # -- §19 storage attribution -------------------------------------------
    # Every pre-1.3 object falls into exactly one bucket.
    attribution = {}
    category_sql = {
        "sample_image": "JOIN sample_image si ON si.storage_id = so.id",
        "sample_rendition": ("JOIN sample_image_rendition sir ON sir.storage_id = so.id"),
        "report_json": "JOIN report_version rv ON rv.json_storage_id = so.id",
        "report_legacy_pdf": "JOIN report_version rv ON rv.pdf_storage_id = so.id",
        "live_signature": "JOIN app_user u ON u.signature_storage_id = so.id",
    }
    for name, join in category_sql.items():
        total = _scalar(conn, f"SELECT COUNT(DISTINCT so.id) FROM storage_object so {join}")
        attributed = _scalar(
            conn,
            f"SELECT COUNT(DISTINCT so.id) FROM storage_object so {join} "
            "WHERE so.tenant_id IS NOT NULL")
        attribution[name] = {
            "objects": total, "attributed": attributed,
            "unresolved": total - attributed,
        }
    attribution["tenant_logo"] = {
        "objects": _scalar(conn, """
            SELECT COUNT(*) FROM storage_object
            WHERE object_key LIKE 'tenants/%/logo%'"""),
        "attributed": _scalar(conn, """
            SELECT COUNT(*) FROM storage_object
            WHERE object_key LIKE 'tenants/%/logo%' AND tenant_id IS NOT NULL"""),
    }
    attribution["tenant_logo"]["unresolved"] = (
        attribution["tenant_logo"]["objects"] - attribution["tenant_logo"]["attributed"])
    attribution["non_billable_control"] = {
        "objects": _scalar(conn,
                           "SELECT COUNT(*) FROM storage_object WHERE object_key LIKE 'avatars/%'"),
        # Controls must stay unattributed: attribution here would be a defect.
        "attributed": _scalar(conn, """
            SELECT COUNT(*) FROM storage_object
            WHERE object_key LIKE 'avatars/%' AND tenant_id IS NOT NULL"""),
    }
    attribution["non_billable_control"]["unresolved"] = (
        attribution["non_billable_control"]["objects"]
        - attribution["non_billable_control"]["attributed"])
    out["attribution"] = attribution
    out["storage_unattributed_total"] = _scalar(
        conn, "SELECT COUNT(*) FROM storage_object WHERE tenant_id IS NULL")

    # -- §20 tenant-logo gap ------------------------------------------------
    out["logo"] = {
        "tenants_with_logo_url": _scalar(
            conn, "SELECT COUNT(*) FROM tenant WHERE logo_url IS NOT NULL"),
        "resolved": _scalar(
            conn, "SELECT COUNT(*) FROM tenant WHERE logo_storage_id IS NOT NULL"),
        "unresolved": _scalar(conn, """
            SELECT COUNT(*) FROM tenant
            WHERE logo_url IS NOT NULL AND logo_storage_id IS NULL"""),
        "resolved_by_independent_python": len(_resolve_logo_in_python(conn)),
        "cross_tenant": out["tenant_isolation"]["tenant_logo_cross_tenant"],
    }

    # -- §21/§22/§23 usage baseline ----------------------------------------
    tenant_count = _scalar(conn, "SELECT COUNT(*) FROM tenant")
    usage_rows = _scalar(conn, "SELECT COUNT(*) FROM tenant_usage")
    migrated = {str(t): b for t, b in
                _rows(conn, "SELECT tenant_id, billable_storage_bytes FROM tenant_usage")}
    independent = recompute_billable_bytes(conn)

    mismatches = []
    max_abs_diff = 0
    total_diff = 0
    for tid, expected in independent.items():
        actual = migrated.get(tid)
        if actual is None:
            mismatches.append({"tenant": tid, "reason": "missing_usage_row",
                               "expected": expected["total"], "actual": None})
            continue
        diff = actual - expected["total"]
        total_diff += diff
        max_abs_diff = max(max_abs_diff, abs(diff))
        if diff != 0:
            mismatches.append({"tenant": tid, "reason": "byte_mismatch",
                               "expected": expected["total"], "actual": actual,
                               "diff": diff})

    double_counted = {
        tid: v["total"] - v["distinct_total"]
        for tid, v in independent.items() if v["total"] != v["distinct_total"]
    }

    out["usage"] = {
        "tenant_count": tenant_count,
        "tenant_usage_rows": usage_rows,
        "missing_usage_rows": tenant_count - usage_rows,
        "duplicate_usage_rows": _scalar(conn, """
            SELECT COALESCE(SUM(c - 1), 0) FROM (
                SELECT COUNT(*) AS c FROM tenant_usage GROUP BY tenant_id HAVING COUNT(*) > 1
            ) d"""),
        "negative_usage_rows": _scalar(
            conn, "SELECT COUNT(*) FROM tenant_usage WHERE billable_storage_bytes < 0"),
        "zero_usage_rows": _scalar(
            conn, "SELECT COUNT(*) FROM tenant_usage WHERE billable_storage_bytes = 0"),
        "total_migrated_bytes": _scalar(
            conn, "SELECT COALESCE(SUM(billable_storage_bytes), 0) FROM tenant_usage"),
        "total_expected_bytes": sum(v["total"] for v in independent.values()),
        "mismatching_tenants": len(mismatches),
        "mismatches": mismatches[:20],
        "max_abs_difference": max_abs_diff,
        "total_difference": total_diff,
        "double_counted_tenants": double_counted,
        "per_tenant_expected": {t: v["total"] for t, v in independent.items()},
        "per_tenant_categories": {t: v["by_category"] for t, v in independent.items()},
    }

    # -- §25/§27 notification and threshold safety --------------------------
    out["migration_side_effects"] = {
        "notification": _scalar(conn, "SELECT COUNT(*) FROM notification"),
        "notification_recipient": _scalar(conn, "SELECT COUNT(*) FROM notification_recipient"),
        "notification_delivery": _scalar(conn, "SELECT COUNT(*) FROM notification_delivery"),
        "notification_preference": _scalar(conn, "SELECT COUNT(*) FROM notification_preference"),
        "tenant_usage_threshold_state": _scalar(
            conn, "SELECT COUNT(*) FROM tenant_usage_threshold_state"),
        "tenant_limits": _scalar(conn, "SELECT COUNT(*) FROM tenant_limits"),
        "tenant_usage_reconciliation": _scalar(
            conn, "SELECT COUNT(*) FROM tenant_usage_reconciliation"),
    }

    # -- §24 seat semantics --------------------------------------------------
    seat_rows = _rows(conn, """
        SELECT t.id,
               (SELECT COUNT(*) FROM app_user u WHERE u.tenant_id = t.id),
               (SELECT COUNT(DISTINCT u.id) FROM app_user u
                  JOIN user_role ur ON ur.user_id = u.id
                  JOIN role r ON r.id = ur.role_id
                 WHERE u.tenant_id = t.id AND u.is_active AND r.code <> 'physician'),
               (SELECT COUNT(DISTINCT u.id) FROM app_user u
                 WHERE u.tenant_id = t.id AND u.is_active
                   AND EXISTS (SELECT 1 FROM user_role ur JOIN role r ON r.id = ur.role_id
                                WHERE ur.user_id = u.id AND r.code = 'physician')
                   AND NOT EXISTS (SELECT 1 FROM user_role ur JOIN role r ON r.id = ur.role_id
                                    WHERE ur.user_id = u.id AND r.code <> 'physician'))
        FROM tenant t ORDER BY t.id""")
    out["seats"] = {
        str(t): {"registered_users": reg, "active_internal_users": internal,
                 "active_physician_portal_users": portal}
        for t, reg, internal, portal in seat_rows
    }

    # -- §29 constraint and orphan integrity ---------------------------------
    orphans = {
        "storage_tenant_orphan": """
            SELECT COUNT(*) FROM storage_object so
            WHERE so.tenant_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM tenant t WHERE t.id = so.tenant_id)""",
        "usage_tenant_orphan": """
            SELECT COUNT(*) FROM tenant_usage tu
            WHERE NOT EXISTS (SELECT 1 FROM tenant t WHERE t.id = tu.tenant_id)""",
        "logo_storage_orphan": """
            SELECT COUNT(*) FROM tenant t WHERE t.logo_storage_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM storage_object so WHERE so.id = t.logo_storage_id)""",
        "sample_image_storage_orphan": """
            SELECT COUNT(*) FROM sample_image si
            WHERE NOT EXISTS (SELECT 1 FROM storage_object so WHERE so.id = si.storage_id)""",
    }
    out["orphans"] = {k: _scalar(conn, v) for k, v in orphans.items()}
    out["invalid_constraints"] = _rows(conn, """
        SELECT conrelid::regclass::text, conname FROM pg_constraint
        WHERE NOT convalidated AND connamespace = 'public'::regnamespace""")
    return out
