"""The scope of a laboratory-content cleanup (Céluma 1.4, issue #39; ADR 0021).

Pure database reads (preview) and the deletion statements (executor), driven by ONE
declarative table list so what the preview counts, what `scope_revision` hashes and what the
executor deletes cannot drift apart.

Scope root: every `order` of the target tenant, previously removed (tombstoned) orders included. Every
other table is reached through the verified foreign-key chain
(`celuma-engineering/specs/laboratory/laboratory-content-cleanup.md`, dependency matrix) or,
for the three polymorphic references (`assignment`, `notification`, `audit_log`), through the
id sets of the scoped rows. Nothing is selected by `tenant_id` alone except the root itself
and the polymorphic tables' own tenant guard, and no object is selected by bucket or prefix.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlmodel import Session, SQLModel

# --------------------------------------------------------------------------- id-set fragments

ORD = 'SELECT id FROM "order" WHERE tenant_id = :t'
SMP = f"SELECT id FROM sample WHERE order_id IN ({ORD})"
IMG = f"SELECT id FROM sample_image WHERE sample_id IN ({SMP})"
REN = f"SELECT id FROM sample_image_rendition WHERE sample_image_id IN ({IMG})"
REP = f"SELECT id FROM report WHERE order_id IN ({ORD})"
VER = f"SELECT id FROM report_version WHERE report_id IN ({REP})"
AMD = f"SELECT id FROM report_amendment WHERE report_id IN ({REP})"
COM = f"SELECT id FROM order_comment WHERE order_id IN ({ORD})"
INV = f"SELECT id FROM invoice WHERE order_id IN ({ORD})"
RVW = f"SELECT id FROM report_review WHERE order_id IN ({ORD}) OR report_id IN ({REP})"
PAY = f"SELECT id FROM payment WHERE invoice_id IN ({INV})"
ITM = f"SELECT id FROM invoice_item WHERE invoice_id IN ({INV})"
NOT = (
    "SELECT id FROM notification WHERE tenant_id = :t AND resource_type IN ('order','sample','report') "
    f"AND resource_id IN ({ORD} UNION {SMP} UNION {REP})"
)
#: Every id a scoped row can be known by in a polymorphic reference (audit `entity_id`).
ALL_SCOPED_IDS = " UNION ".join(
    f"({fragment})" for fragment in (ORD, SMP, IMG, REN, REP, VER, AMD, COM, INV, PAY, ITM, RVW, NOT)
)


@dataclass(frozen=True)
class ScopeTable:
    key: str          # stable name used in counts and the manifest
    table: str        # SQL table (quoted when needed)
    where: str        # predicate over alias x
    hash_ids_only: bool = False   # mutable operational logs: identity, not state
    in_manifest: bool = True


#: Tables DELETED by the cleanup (finance tables are listed separately: only when the option is ON).
CORE_TABLES: Tuple[ScopeTable, ...] = (
    ScopeTable("orders", '"order"', f"x.id IN ({ORD})"),
    ScopeTable("samples", "sample", f"x.id IN ({SMP})"),
    ScopeTable("sample_images", "sample_image", f"x.id IN ({IMG})"),
    ScopeTable("sample_image_renditions", "sample_image_rendition", f"x.id IN ({REN})"),
    ScopeTable("sample_labels", "sample_labels", f"x.sample_id IN ({SMP})"),
    ScopeTable("order_labels", "order_labels", f"x.order_id IN ({ORD})"),
    ScopeTable("order_comments", "order_comment", f"x.id IN ({COM})"),
    ScopeTable("order_comment_mentions", "order_comment_mention", f"x.comment_id IN ({COM})"),
    ScopeTable("order_events", "order_event", f"x.order_id IN ({ORD})"),
    ScopeTable("order_requesting_physicians", "order_requesting_physician", f"x.order_id IN ({ORD})"),
    ScopeTable("reports", "report", f"x.id IN ({REP})"),
    ScopeTable("report_versions", "report_version", f"x.id IN ({VER})"),
    ScopeTable("report_amendments", "report_amendment", f"x.id IN ({AMD})"),
    ScopeTable("report_reviews", "report_review", f"x.id IN ({RVW})"),
    ScopeTable(
        "assignments", "assignment",
        f"x.tenant_id = :t AND ((x.item_type = 'lab_order' AND x.item_id IN ({ORD})) "
        f"OR (x.item_type = 'sample' AND x.item_id IN ({SMP})) OR (x.item_type = 'report' AND x.item_id IN ({REP})))",
        hash_ids_only=True,
    ),
    ScopeTable("notifications", "notification", f"x.id IN ({NOT})", hash_ids_only=True),
    ScopeTable("notification_recipients", "notification_recipient", f"x.notification_id IN ({NOT})", hash_ids_only=True),
    ScopeTable("notification_deliveries", "notification_delivery", f"x.notification_id IN ({NOT})", hash_ids_only=True),
)
#: Order-linked billing is ALWAYS part of the cleanup (owner decision, 2026-10-28: "limpiar todo"). It follows the
#: scoped orders; the SaaS plan/subscription and the price catalog are never touched.
FINANCE_TABLES: Tuple[ScopeTable, ...] = (
    ScopeTable("invoices", "invoice", f"x.id IN ({INV})"),
    ScopeTable("invoice_items", "invoice_item", f"x.id IN ({ITM})"),
    ScopeTable("payments", "payment", f"x.id IN ({PAY})"),
)
#: The historical audit of the scoped operational records is DELETED with them (owner decision, 2026-10-28:
#: "que limpie todo"): no row, no payload, no redacted placeholder. Counted and disclosed, but not hashed into the
#: revision (it grows with ordinary reads and attempts); the deletion statement re-selects it at execution time.
#: The cleanup's OWN provenance rows (`entity_type = 'laboratory_cleanup'`) are not part of it.
AUDIT_TABLE = ScopeTable(
    "audit_rows", "audit_log",
    f"x.tenant_id = :t AND (x.order_id IN ({ORD}) OR x.sample_id IN ({SMP}) OR x.entity_id IN ({ALL_SCOPED_IDS}))",
    in_manifest=False,
)

#: Preserved master data, counted in the preview so the operator sees what survives.
PRESERVED_TABLES: Tuple[Tuple[str, str], ...] = (
    ("patients", "patient"),
    ("users", "app_user"),
    ("branches", "branch"),
    ("requesting_physicians", "requesting_physician"),
    ("study_types", "study_type"),
    ("sample_types", "sample_type"),
    ("price_catalog_entries", "price_catalog"),
    ("labels", "label"),
    ("report_templates", "report_template"),
    ("report_letterheads", "report_letterhead"),
    ("report_sections", "report_section"),
    ("numbering_configs", "numbering_config"),
    ("numbering_counters", "numbering_counter"),
)
#: Counted with their own SQL (version tables).
PRESERVED_VIA_PARENT: Tuple[Tuple[str, str], ...] = (
    ("report_template_versions", "SELECT count(*) FROM report_template_version WHERE tenant_id = :t"),
    ("report_letterhead_versions", "SELECT count(*) FROM report_letterhead_version WHERE tenant_id = :t"),
)
#: Evidence kept untouched (tenant totals; disclosed).
EVIDENCE_TABLES: Tuple[Tuple[str, str], ...] = (
    ("idempotency_receipts", "idempotency_receipt"),
    ("numbering_allocations", "numbering_allocation"),
    ("sample_code_corrections", "sample_code_correction"),
)

#: JSON columns that may name a managed object by key and that SURVIVE the cleanup — of ANY laboratory, not just the
#: target: a template of another laboratory may legitimately point at an object the target also uploaded. The
#: predicate (over alias x) only excludes rows the cleanup itself is deleting. The schema guard test compares this
#: set against every JSON column of the schema so a new one must be classified.
CONFIG_JSON_COLUMNS: Tuple[Tuple[str, str, str], ...] = (
    # (table, json column, predicate over alias x selecting the rows that SURVIVE)
    ("report_template", "template_json", "TRUE"),
    ("report_template_version", "configuration", "TRUE"),
    ("report_letterhead_version", "configuration", "TRUE"),
    ("report", "template", f"x.id NOT IN ({REP})"),
)
#: Plain-text columns holding a public URL that embeds an object key (any laboratory).
URL_REFERENCE_COLUMNS: Tuple[Tuple[str, str], ...] = (("tenant", "logo_url"), ("app_user", "avatar_url"))


def referenced_by_preserved_text(session: Session, tenant_id: str, keys: List[str]) -> set:
    """The subset of `keys` named inside ANY surviving configuration JSON or URL column of ANY laboratory.
    Evaluated by the database (`position(key in text)`), so no content is read into the application and no key
    is treated as a pattern. Callers decide what a hit means; nothing about the referencing row is disclosed."""
    if not keys:
        return set()
    found: set = set()
    sources = [(t, c, p) for t, c, p in CONFIG_JSON_COLUMNS] + [(t, c, "TRUE") for t, c in URL_REFERENCE_COLUMNS]
    for table, column, predicate in sources:
        rows = session.execute(text(
            f"SELECT k FROM unnest(CAST(:keys AS text[])) AS k WHERE EXISTS "
            f"(SELECT 1 FROM {table} x WHERE {predicate} AND position(k in x.{column}::text) > 0)"
        ), {"keys": keys, "t": tenant_id}).all()
        found.update(r[0] for r in rows)
    return found


def object_is_still_referenced(session: Session, tenant_id, bucket: str, key: str) -> bool:
    """Deletion-time re-check of one outbox object: another `storage_object` row (any laboratory) names this
    bucket/key, or surviving configuration text/URLs of any laboratory still mention it."""
    other_row = session.execute(
        text("SELECT 1 FROM storage_object WHERE bucket = :b AND object_key = :k LIMIT 1"), {"b": bucket, "k": key}
    ).first()
    if other_row is not None:
        return True
    return key in referenced_by_preserved_text(session, str(tenant_id), [key])


# --------------------------------------------------------------------------- results


@dataclass
class ObjectRef:
    storage_object_id: str
    provider: str
    bucket: str
    object_key: str
    size_bytes: Optional[int]
    category: str
    physical: bool = True   # False: the row goes, the physical object is shared and stays


@dataclass
class ObjectInventory:
    delete: List[ObjectRef] = field(default_factory=list)          # rows to delete (+ physical when .physical)
    kept_shared: List[str] = field(default_factory=list)           # storage ids whose ROW is kept
    blockers: List[dict] = field(default_factory=list)

    @property
    def physical(self) -> List[ObjectRef]:
        seen, out = set(), []
        for ref in self.delete:
            key = (ref.bucket, ref.object_key)
            if ref.physical and key not in seen:
                seen.add(key)
                out.append(ref)
        return out


@dataclass
class Scope:
    tenant_id: str
    counts: Dict[str, Dict[str, int]]
    revision: str
    objects: ObjectInventory
    blockers: List[dict]
    warnings: List[dict]

    def public(self, *, laboratory: dict) -> dict:
        physical = self.objects.physical
        by_category: Dict[str, Dict[str, int]] = {}
        for ref in physical:
            bucket = by_category.setdefault(ref.category, {"objects": 0, "bytes": 0})
            bucket["objects"] += 1
            bucket["bytes"] += int(ref.size_bytes or 0)
        return {
            "laboratory": laboratory,
            "delete": self.counts["delete"],
            "retain_as_evidence": self.counts["retain"],
            "preserve": self.counts["preserve"],
            "objects": {
                "delete_count": len(physical),
                "delete_bytes": sum(int(ref.size_bytes or 0) for ref in physical),
                "by_category": by_category,
                "kept_shared_count": len(self.objects.kept_shared),
            },
            "blockers": self.blockers,
            "warnings": self.warnings,
            "scope_revision": self.revision,
        }

    def approved_summary(self) -> dict:
        """The safe numbers stored on the operation row: no ids, keys, codes or names."""
        physical = self.objects.physical
        return {
            "delete": self.counts["delete"],
            "retain_as_evidence": self.counts["retain"],
            "objects": {
                "delete_count": len(physical),
                "delete_bytes": sum(int(ref.size_bytes or 0) for ref in physical),
                "kept_shared_count": len(self.objects.kept_shared),
            },
        }


# --------------------------------------------------------------------------- queries


def _scalar(session: Session, sql: str, **params):
    return session.execute(text(sql), params).scalar()


def tables_for(*_ignored) -> Tuple[ScopeTable, ...]:
    """Every table of the deletion scope. (Accepts and ignores legacy positional arguments: there is no option.)"""
    return CORE_TABLES + FINANCE_TABLES


def _count_and_hash(session: Session, st: ScopeTable, tenant_id: str) -> Tuple[int, str]:
    agg = "x.id::text" if st.hash_ids_only else "x::text"
    sql = (
        f"SELECT count(*), coalesce(encode(sha256(convert_to(string_agg({agg}, E'\\n' ORDER BY {agg}), 'UTF8')), 'hex'), '') "
        f"FROM {st.table} x WHERE {st.where}"
    )
    count, digest = session.execute(text(sql), {"t": tenant_id}).one()
    return int(count), digest


def count_rows(session: Session, st: ScopeTable, tenant_id: str) -> int:
    return int(_scalar(session, f"SELECT count(*) FROM {st.table} x WHERE {st.where}", t=tenant_id))


# --------------------------------------------------------------------------- objects


def _storage_references_from_metadata() -> List[Tuple[str, str]]:
    """Every (table, column) with a foreign key to `storage_object.id`, introspected so a future FK
    is protected without editing this module."""
    refs = []
    for table in SQLModel.metadata.tables.values():
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.column.table.name == "storage_object" and fk.column.name == "id":
                    refs.append((table.name, column.name))
    return sorted(refs)


#: Referencing tables whose in-scope rows are being deleted: only rows OUTSIDE the scope keep an object.
_SCOPED_REFERENCERS: Dict[str, str] = {
    "sample_image": IMG,
    "sample_image_rendition": REN,
    "report_version": VER,
}


def resolve_objects(session: Session, tenant_id: str) -> ObjectInventory:
    """Resolve the managed objects of the scoped graph and decide, per storage row, whether it is
    exclusively owned (see the spec, "Managed objects"). Never selects by bucket, prefix or tenant."""
    inventory = ObjectInventory()
    rows = session.execute(text(f"""
        SELECT s.id::text, s.storage_id::text, 'sample_image' FROM sample_image s WHERE s.id IN ({IMG})
        UNION ALL
        SELECT r.id::text, r.storage_id::text, 'sample_image_rendition' FROM sample_image_rendition r WHERE r.id IN ({REN})
        UNION ALL
        SELECT v.id::text, v.pdf_storage_id::text, 'pdf' FROM report_version v WHERE v.id IN ({VER}) AND v.pdf_storage_id IS NOT NULL
        UNION ALL
        SELECT v.id::text, v.json_storage_id::text, 'report_json' FROM report_version v WHERE v.id IN ({VER}) AND v.json_storage_id IS NOT NULL
        UNION ALL
        SELECT v.id::text, v.html_storage_id::text, 'report_html' FROM report_version v WHERE v.id IN ({VER}) AND v.html_storage_id IS NOT NULL
        UNION ALL
        -- Historical official-PDF attempts of a scoped report: retained (and billable) `storage_object` rows that
        -- no version points at any more. Owned by the report through the key the generator writes
        -- (`reports/<tenant>/<report>/...`), matched per SCOPED report id and only for the tenant's own rows.
        SELECT r.id::text, o.id::text, 'historical_artifact'
        FROM report r JOIN storage_object o
          ON o.tenant_id = r.tenant_id AND starts_with(o.object_key, 'reports/' || r.tenant_id::text || '/' || r.id::text || '/')
        WHERE r.id IN ({REP})
    """), {"t": tenant_id}).all()
    category_of: Dict[str, str] = {}
    for _owner, storage_id, category in rows:
        category_of.setdefault(storage_id, category)
    if not category_of:
        return inventory
    ids = sorted(category_of)

    objs = {
        row[0]: row for row in session.execute(
            text("SELECT id::text, tenant_id::text, provider, bucket, object_key, size_bytes, sha256_hex "
                 "FROM storage_object WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids}
        ).all()
    }
    row_deletable: Dict[str, bool] = {}
    for storage_id in ids:
        obj = objs.get(storage_id)
        if obj is None:
            # A dangling pointer in the graph: nothing to delete, and the referencing row goes anyway.
            continue
        if obj[1] is not None and obj[1] != tenant_id:
            inventory.blockers.append({
                "code": "foreign_storage_owner",
                "message": "Un objeto del alcance pertenece a otro laboratorio; no se eliminará y debe revisarse.",
            })
            row_deletable[storage_id] = False
            continue
        row_deletable[storage_id] = True

    # Any foreign key from outside the scoped rows keeps the row (logos, signatures, other tenants...).
    for table, column in _storage_references_from_metadata():
        scoped = _SCOPED_REFERENCERS.get(table)
        sql = f"SELECT DISTINCT x.{column}::text FROM {table} x WHERE x.{column} = ANY(CAST(:ids AS uuid[]))"
        if scoped:
            sql += f" AND x.id NOT IN ({scoped})"
        for (kept,) in session.execute(text(sql), {"ids": ids, "t": tenant_id}).all():
            if kept in row_deletable:
                row_deletable[kept] = False

    # References from PRESERVED configuration of ANY laboratory (templates, letterheads, URL columns) keep the row.
    candidate_keys = sorted({obj[4] for obj in objs.values() if row_deletable.get(obj[0]) and obj[4]})
    referenced = referenced_by_preserved_text(session, tenant_id, candidate_keys)
    for storage_id, obj in objs.items():
        if row_deletable.get(storage_id) and obj[4] in referenced:
            row_deletable[storage_id] = False

    # Physical sharing: another storage_object row (any tenant) with the same bucket/key that is not
    # itself being deleted keeps the PHYSICAL object even when this row goes.
    deletable_ids = [sid for sid, ok in row_deletable.items() if ok]
    shared_physical: set = set()
    if deletable_ids:
        for bucket, key in session.execute(text("""
            SELECT DISTINCT o.bucket, o.object_key FROM storage_object o
            WHERE o.id <> ALL(CAST(:ids AS uuid[]))
              AND (o.bucket, o.object_key) IN (
                SELECT bucket, object_key FROM storage_object WHERE id = ANY(CAST(:ids AS uuid[])))
        """), {"ids": deletable_ids}).all():
            shared_physical.add((bucket, key))

    for storage_id in ids:
        obj = objs.get(storage_id)
        if obj is None:
            continue
        if not row_deletable.get(storage_id):
            inventory.kept_shared.append(storage_id)
            continue
        category = category_of[storage_id]
        if category in ("pdf", "historical_artifact"):
            category = "official_pdf" if obj[6] else ("legacy_pdf" if category == "pdf" else "report_artifact")
        inventory.delete.append(ObjectRef(
            storage_object_id=storage_id, provider=obj[2], bucket=obj[3], object_key=obj[4],
            size_bytes=obj[5], category=category, physical=(obj[3], obj[4]) not in shared_physical,
        ))
    return inventory


# --------------------------------------------------------------------------- the scope


def compute_scope(session: Session, tenant_id, *_ignored) -> Scope:
    """Everything the preview shows, the revision it is bound to and the blockers. Read-only. The scope is always
    the complete one: orders, samples, reports and ALL their billing (invoices, items, payments) and audit."""
    t = str(tenant_id)
    counts_delete: Dict[str, int] = {}
    manifest: Dict[str, object] = {}
    for st in tables_for():
        count, digest = _count_and_hash(session, st, t)
        counts_delete[st.key] = count
        manifest[st.key] = [count, digest]
    counts_delete["audit_rows"] = count_rows(session, AUDIT_TABLE, t)

    objects = resolve_objects(session, t)
    manifest["objects"] = sorted(
        (ref.storage_object_id, ref.bucket, ref.object_key, ref.size_bytes, ref.physical) for ref in objects.delete
    )
    manifest["kept_shared"] = sorted(objects.kept_shared)

    # Technical evidence that stays (no business or clinical payload): code ledger, counters, corrections, receipts.
    retain: Dict[str, int] = {}
    retain["previously_removed_orders_included"] = int(_scalar(
        session, 'SELECT count(*) FROM "order" WHERE tenant_id = :t AND removed_at IS NOT NULL', t=t))
    for key, table in EVIDENCE_TABLES:
        retain[key] = int(_scalar(session, f"SELECT count(*) FROM {table} WHERE tenant_id = :t", t=t))

    preserve: Dict[str, int] = {}
    for key, table in PRESERVED_TABLES:
        preserve[key] = int(_scalar(session, f"SELECT count(*) FROM {table} WHERE tenant_id = :t", t=t))
    for key, sql in PRESERVED_VIA_PARENT:
        preserve[key] = int(_scalar(session, sql, t=t))
    preserve["shared_assets_kept"] = len(objects.kept_shared)

    blockers: List[dict] = list(objects.blockers)
    foreign = int(_scalar(session, f"""
        SELECT (SELECT count(*) FROM sample WHERE order_id IN ({ORD}) AND tenant_id <> :t)
             + (SELECT count(*) FROM report WHERE order_id IN ({ORD}) AND tenant_id <> :t)
             + (SELECT count(*) FROM invoice WHERE order_id IN ({ORD}) AND tenant_id <> :t)
    """, t=t))
    if foreign:
        blockers.append({
            "code": "cross_tenant_reference",
            "message": "Hay registros de otro laboratorio ligados a órdenes de este laboratorio; requiere revisión técnica.",
        })
    sending = int(_scalar(session, f"""
        SELECT count(*) FROM notification_delivery d WHERE d.status = 'SENDING'
        AND d.notification_id IN ({NOT})
    """, t=t))
    if sending:
        blockers.append({
            "code": "deliveries_in_flight",
            "message": "Hay correos de notificación en envío ligados a este contenido; espera a que terminen.",
        })
    running = int(_scalar(session, """
        SELECT count(*) FROM tenant_usage_reconciliation WHERE tenant_id = :t AND status = 'RUNNING'
    """, t=t))
    if running:
        blockers.append({
            "code": "reconciliation_running",
            "message": "Hay una conciliación de uso en curso; espera a que termine.",
        })
    open_ops = int(_scalar(session, """
        SELECT count(*) FROM laboratory_cleanup_operation WHERE tenant_id = :t AND status IN ('QUEUED','RUNNING','FAILED')
    """, t=t))
    if open_ops:
        blockers.append({
            "code": "operation_open",
            "message": "Ya existe una limpieza abierta para este laboratorio; reanúdala o cancélala primero.",
        })

    warnings: List[dict] = []
    if retain["previously_removed_orders_included"]:
        warnings.append({
            "code": "removed_orders_included",
            "message": "Se incluyen órdenes retiradas previamente como capturas erróneas.",
        })
    if objects.kept_shared:
        warnings.append({
            "code": "shared_assets_kept",
            "message": "Algunos objetos referenciados por contenido a eliminar se conservan porque otro registro los usa.",
        })

    revision = hashlib.sha256(
        json.dumps({"tenant": t, "manifest": manifest}, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return Scope(
        tenant_id=t,
        counts={"delete": counts_delete, "retain": retain, "preserve": preserve},
        revision=revision,
        objects=objects,
        blockers=blockers,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- deletion


def delete_graph(session: Session, scope: Scope, *, operation_id, now) -> Dict[str, int]:
    """The database phase's statements, in dependency order, inside the caller's transaction.
    Returns the counts actually affected. Raises on any FK violation (the caller rolls back)."""
    t = scope.tenant_id
    op = str(operation_id)
    done: Dict[str, int] = {}

    def run(label: str, sql: str, **params) -> int:
        result = session.execute(text(sql), {"t": t, "op": op, "now": now, **params})
        done[label] = int(result.rowcount or 0)
        return done[label]

    # 0) Evidence first, while the rows still exist.
    run("retired_codes_orders", f"""
        INSERT INTO laboratory_cleanup_retired_code (id, tenant_id, entity_kind, code, operation_id, created_at)
        SELECT gen_random_uuid(), :t, 'order', o.order_code, :op, :now FROM "order" o WHERE o.id IN ({ORD})
        ON CONFLICT (tenant_id, entity_kind, code) DO NOTHING
    """)
    run("retired_codes_samples", f"""
        INSERT INTO laboratory_cleanup_retired_code (id, tenant_id, entity_kind, code, operation_id, created_at)
        SELECT gen_random_uuid(), :t, 'sample', s.sample_code, :op, :now FROM sample s WHERE s.id IN ({SMP})
        ON CONFLICT (tenant_id, entity_kind, code) DO NOTHING
    """)
    # Code-retirement records keep the code but not the operator's free text (it may describe the content).
    run("code_corrections_scrubbed", f"UPDATE sample_code_correction SET reason = 'laboratory_cleanup' WHERE tenant_id = :t AND sample_id IN ({SMP})")
    # The historical audit of the removed records goes WITH them (owner decision: "que limpie todo"): rows and
    # payloads, no placeholder, no pointer. Re-selected here (fresh), while every scoped id still exists.
    run("audit_rows", f"DELETE FROM audit_log x WHERE {AUDIT_TABLE.where}")

    # 1) Resource-linked notifications and jobs, assignments, conversation, timeline, requester links, labels.
    run("notification_deliveries", f"DELETE FROM notification_delivery WHERE notification_id IN ({NOT})")
    run("notification_recipients", f"DELETE FROM notification_recipient WHERE notification_id IN ({NOT})")
    run("notifications", f"DELETE FROM notification WHERE id IN ({NOT})")
    run("assignments", f"DELETE FROM assignment x WHERE {_where('assignments')}")
    run("order_comment_mentions", f"DELETE FROM order_comment_mention WHERE comment_id IN ({COM})")
    run("order_comments", f"DELETE FROM order_comment WHERE id IN ({COM})")
    run("sample_labels", f"DELETE FROM sample_labels WHERE sample_id IN ({SMP})")
    run("order_labels", f"DELETE FROM order_labels WHERE order_id IN ({ORD})")
    run("order_events", f"DELETE FROM order_event WHERE order_id IN ({ORD})")
    run("order_requesting_physicians", f"DELETE FROM order_requesting_physician WHERE order_id IN ({ORD})")
    run("sample_image_renditions", f"DELETE FROM sample_image_rendition WHERE id IN ({REN})")
    run("sample_images", f"DELETE FROM sample_image WHERE id IN ({IMG})")

    # 2) Break the order<->report and order<->invoice cycles, then the report/amendment cycle.
    run("order_pointers_cleared", f'UPDATE "order" SET report_id = NULL, invoice_id = NULL WHERE id IN ({ORD})')
    run("report_reviews", f"DELETE FROM report_review WHERE id IN ({RVW})")
    session.execute(text(f"UPDATE report_version SET amendment_id = NULL WHERE id IN ({VER})"), {"t": t})
    session.execute(text(f"UPDATE report_amendment SET published_version_id = NULL WHERE id IN ({AMD})"), {"t": t})
    run("report_amendments", f"DELETE FROM report_amendment WHERE id IN ({AMD})")
    run("report_versions", f"DELETE FROM report_version WHERE id IN ({VER})")
    run("reports", f"DELETE FROM report WHERE id IN ({REP})")

    # 3) Order-linked billing — always.
    run("payments", f"DELETE FROM payment WHERE id IN ({PAY})")
    run("invoice_items", f"DELETE FROM invoice_item WHERE id IN ({ITM})")
    run("invoices", f"DELETE FROM invoice WHERE id IN ({INV})")

    # 4) Samples, then orders: nothing of the operational graph survives.
    run("samples", f"DELETE FROM sample WHERE id IN ({SMP})")
    run("orders", f'DELETE FROM "order" o WHERE o.id IN ({ORD})')
    return done


def _where(key: str) -> str:
    for st in CORE_TABLES + FINANCE_TABLES:
        if st.key == key:
            return st.where
    raise KeyError(key)
