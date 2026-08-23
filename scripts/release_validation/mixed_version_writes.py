"""Mixed-version data compatibility check (§16).

TEST TOOLING ONLY. Never imported from `app/`.

After a database has reached `v1_3_0`, a Céluma 1.2 application task may still
be serving traffic during an ECS rolling deploy. Its writes name only the
columns that existed at `v1_2_0` and set nothing the 1.3 schema added. This
script issues exactly those writes and reports whether each succeeds.

The schema is known to be additive; what this adds is the data-level check
that the additive columns and their constraints do not reject a 1.2-shaped
INSERT -- a NOT NULL column without a server default, or a CHECK constraint on
a pre-existing table, would show up here and nowhere in a schema diff.

    python scripts/release_validation/mixed_version_writes.py relval_small
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

psycopg2.extras.register_uuid()

DB_HOST = os.environ.get("RELVAL_DB_HOST", "celuma-relval-db")
DB_PORT = os.environ.get("RELVAL_DB_PORT", "5432")
DB_USER = os.environ.get("RELVAL_DB_USER", "postgres")
DB_PASS = os.environ.get("RELVAL_DB_PASS", "postgres")
OUT_DIR = Path(os.environ.get("RELVAL_OUT", "/app/.release-validation"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("database")
    args = ap.parse_args()

    conn = psycopg2.connect(
        f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{args.database}")
    conn.autocommit = False
    results: list[dict] = []

    with conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        revision = cur.fetchone()[0]
        cur.execute("SELECT id FROM tenant ORDER BY id LIMIT 1")
        tenant_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM branch WHERE tenant_id = %s ORDER BY id LIMIT 1",
                    (tenant_id,))
        branch_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM app_user WHERE tenant_id = %s ORDER BY id LIMIT 1",
                    (tenant_id,))
        user_id = cur.fetchone()[0]

    patient_id, order_id, sample_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    storage_id, report_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    # Every statement names 1.2-era columns only. Nothing here mentions
    # storage_object.tenant_id, tenant.reports_v2_enabled, or any column the
    # 1.3 migration added.
    steps: list[tuple[str, str, tuple]] = [
        ("create_patient", """
            INSERT INTO patient (tenant_id, branch_id, created_at, id, patient_code,
                                 first_name, last_name, dob, sex, phone, email)
            VALUES (%s, %s, now(), %s, 'MIXVER-P1', 'Sint', 'Etico',
                    DATE '1980-01-01', 'F', '5550000000', 'mixver@example.invalid')
         """, (tenant_id, branch_id, patient_id)),
        ("update_patient", """
            UPDATE patient SET phone = '5551111111' WHERE id = %s
         """, (patient_id,)),
        ("create_order", """
            INSERT INTO "order" (created_at, id, tenant_id, branch_id, patient_id,
                                 order_code, status, requested_by, billed_lock, created_by)
            VALUES (now(), %s, %s, %s, %s, 'MIXVER-O1', 'RECEIVED', 'Dr. Sint', false, %s)
         """, (order_id, tenant_id, branch_id, patient_id, user_id)),
        ("create_sample", """
            INSERT INTO sample (id, tenant_id, branch_id, order_id, sample_code,
                                type, state, collected_at, received_at)
            VALUES (%s, %s, %s, %s, 'MIXVER-S1', 'BIOPSIA', 'RECEIVED', now(), now())
         """, (sample_id, tenant_id, branch_id, order_id)),
        ("create_storage_object_1_2_columns", """
            INSERT INTO storage_object (created_at, id, provider, region, bucket,
                                        object_key, etag, content_type, size_bytes,
                                        created_by)
            VALUES (now(), %s, 'aws', 'mx-central-1', 'celuma-media-synthetic',
                    'mixver/report.json', 'mixver-etag', 'application/json', 2048, %s)
         """, (storage_id, user_id)),
        ("create_report", """
            INSERT INTO report (created_at, id, tenant_id, branch_id, order_id,
                                status, title, created_by)
            VALUES (now(), %s, %s, %s, %s, 'DRAFT', 'Informe mixver', %s)
         """, (report_id, tenant_id, branch_id, order_id, user_id)),
        ("create_report_version", """
            INSERT INTO report_version (created_at, id, report_id, version_no,
                                        json_storage_id, authored_by, authored_at,
                                        is_current)
            VALUES (now(), %s, %s, 1, %s, %s, now(), true)
         """, (version_id, report_id, storage_id, user_id)),
        ("update_report", """
            UPDATE report SET status = 'IN_REVIEW' WHERE id = %s
         """, (report_id,)),
        ("update_storage_object", """
            UPDATE storage_object SET etag = 'mixver-etag-2' WHERE id = %s
         """, (storage_id,)),
    ]

    for name, sql, params in steps:
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
            results.append({"step": name, "ok": True})
        except Exception as exc:  # noqa: BLE001 - the failure IS the measurement
            conn.rollback()
            results.append({"step": name, "ok": False, "error": type(exc).__name__,
                            "message": str(exc).strip()[:300]})

    # A 1.2 writer never sets storage_object.tenant_id, so its objects are
    # unattributed and contribute nothing to the usage counter. Recording that
    # explicitly is the point: it is expected behaviour under a rolling
    # deploy, and Block C/D's incremental accounting is what repairs it.
    with conn.cursor() as cur:
        cur.execute("SELECT tenant_id FROM storage_object WHERE id = %s", (storage_id,))
        row = cur.fetchone()
        attributed = row[0] is not None if row else None
    conn.close()

    out = {
        "database": args.database,
        "alembic_version": revision,
        "at": datetime.now(timezone.utc).isoformat(),
        "steps": results,
        "all_succeeded": all(r["ok"] for r in results),
        "storage_object_written_by_1_2_writer_is_attributed": attributed,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{args.database}-mixed-version.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
    return 0 if out["all_succeeded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
