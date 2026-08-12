"""StorageBillingService — the authoritative billable-storage baseline
calculator (Céluma 1.3, Phase 4, Block C).

This is the ONE place the seven-category billable-storage contract
(docs/celuma-1.3/phase-4-block-c/billable-storage-calculation-contract.md)
is implemented. Both the Block C migration's historical initialization and
any future re-derivation (Block D reconciliation) must call this — never
duplicate the per-category logic elsewhere.

Every method takes an explicit `tenant_id` and scopes every query to it —
same tenant-isolation discipline as `UsageService` (Block B).

Why joins, not `storage_object.tenant_id`, for four of the categories
------------------------------------------------------------------------
Sample images, legacy/manual PDFs, and report JSON bodies are computed by
joining through their owning parent row (`SampleImage`/`SampleImage
Rendition`, `ReportVersion` -> `Report`), not by trusting `storage_object.
tenant_id` directly — this function is correct whether or not the Block C
backfill has run, which matters because the migration that performs the
backfill and the migration that calls this function for initialization are
the same revision, and this function must not depend on statement
ordering within it. Live signatures join through `AppUser`. Official PDFs,
tenant logos, and letterhead/template assets already had `tenant_id`
populated at write time since before Block C (see storage-metadata-gap-
analysis.md), so those three are scoped by the column directly.

Category-disambiguating signals (no `resource_type`/category column
exists on `storage_object` — see storage-metadata-gap-analysis.md §2):

- Official report PDF: `sha256_hex IS NOT NULL` — an existing, standing
  application invariant (`sha256_hex` is only ever populated by
  `ReportPdfGenerationService._persist`; no other write path sets it).
- Legacy/manual report PDF: reached via `ReportVersion.pdf_storage_id`
  AND `sha256_hex IS NULL` (excludes the official-PDF case sharing the
  same FK column).
- Tenant logo: `Tenant.logo_storage_id`, a real FK, since Céluma 1.3
  Phase 4, Block D. Before Block D this category was resolved by parsing
  `Tenant.logo_url` back into an object key with the *currently
  configured* `MEDIA_PUBLIC_BASE_URL` — which meant changing that setting
  silently zeroed a tenant's logo bytes even though a perfectly valid
  logo object existed (block-d-dependencies.md §6). Nothing in this module
  reads `logo_url` any more.

- Letterhead/report-template logo: still identified by `object_key`
  prefix — those objects have `tenant_id` populated and no FK from any
  versioned entity (they are referenced only from configuration JSON), so
  the key prefix remains the one available category signal. This is a
  deliberate, documented exception to "do not infer tenant from S3 key
  strings": no tenant is inferred (tenant_id is already on the row), only
  the *category*. See storage-tenant-attribution-contract.md §4.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional
from uuid import UUID

from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.models.laboratory import SampleImage
from app.models.report import Report, ReportVersion
from app.models.storage import SampleImageRendition, StorageObject
from app.models.tenant import Tenant
from app.models.user import AppUser

#: Object-key prefixes used by ManagedTenantImageService for letterhead and
#: report-template logo assets (app/api/v1/report_letterheads.py:922,
#: app/api/v1/reports.py:1716, plus the `.celuma` import path,
#: app/services/letterhead_portability.py). Deliberately distinct from the
#: tenant-logo prefix (`tenants/{tenant_id}/logo/`) so there is no overlap
#: between the two billable categories.
LETTERHEAD_ASSET_KEY_PREFIXES = ("report-letterheads/", "report-templates/")

#: Category labels attached to each billable object descriptor. Operational
#: metadata (logs, reconciliation findings) — never a billing input.
CATEGORY_SAMPLE_IMAGE = "sample_image"
CATEGORY_SAMPLE_IMAGE_RENDITION = "sample_image_rendition"
CATEGORY_OFFICIAL_PDF = "official_pdf"
CATEGORY_LEGACY_PDF = "legacy_pdf"
CATEGORY_REPORT_JSON = "report_json"
CATEGORY_TENANT_LOGO = "tenant_logo"
CATEGORY_LETTERHEAD_ASSET = "letterhead_asset"
CATEGORY_SIGNATURE = "signature"


def tenant_logo_key_prefix(tenant_id: UUID) -> str:
    """The object-key prefix every tenant-logo upload writes under
    (`app/api/v1/tenants.py::upload_tenant_logo`).

    Used to *verify* that a `Tenant.logo_storage_id` really points at a
    tenant-logo object (Block D's integrity check), never to discover which
    object is current — that is the FK's job, and a key prefix could not
    answer it anyway once a tenant has replaced its logo.
    """
    return f"tenants/{tenant_id}/logo/"


def resolve_current_tenant_logo_storage_object(
    session: Session, tenant: Tenant
) -> Optional[StorageObject]:
    """The `StorageObject` that is this tenant's current logo, or `None`.

    A direct FK lookup (`Tenant.logo_storage_id`) plus an ownership check,
    since Céluma 1.3 Phase 4, Block D — no URL parsing, and therefore no
    dependency on `MEDIA_PUBLIC_BASE_URL` having the same value it had when
    the logo was uploaded.

    Ownership is verified rather than assumed: a foreign key guarantees the
    referenced row exists, not that it belongs to this tenant. A
    cross-tenant reference is never valid application state, so it resolves
    to `None` here (and is separately reported by reconciliation as
    `tenant_logo_integrity_error` — this function's job is to be safe, not
    to alert).

    Used by the billable calculation (only the *current* logo counts) and by
    the tenant-logo upload flow (to find the outgoing logo to decrement).
    """
    if tenant is None or tenant.logo_storage_id is None:
        return None
    obj = session.get(StorageObject, tenant.logo_storage_id)
    if obj is None or obj.tenant_id != tenant.id:
        return None
    return obj


@dataclass(frozen=True)
class BillableStorageBreakdown:
    """Per-category byte totals for one tenant. See billable-storage-
    calculation-contract.md for what is (and is not) included in each
    field."""

    sample_images_bytes: int
    official_pdf_bytes: int
    legacy_pdf_bytes: int
    report_json_bytes: int
    tenant_logo_bytes: int
    letterhead_asset_bytes: int
    signature_bytes: int

    @property
    def total_bytes(self) -> int:
        return (
            self.sample_images_bytes
            + self.official_pdf_bytes
            + self.legacy_pdf_bytes
            + self.report_json_bytes
            + self.tenant_logo_bytes
            + self.letterhead_asset_bytes
            + self.signature_bytes
        )


@dataclass(frozen=True)
class BillableStorageObjectRef:
    """One billable object, as a plain frozen value.

    Céluma 1.3 Phase 4, Block D. Reconciliation needs the *set* of billable
    objects, not only their byte total, so it can verify each one against
    S3 — and it needs them as detached values, because that verification
    happens deliberately outside any database transaction (see
    `usage_reconciliation.py`). No ORM instance survives that boundary.
    """

    storage_object_id: UUID
    object_key: str
    size_bytes: Optional[int]
    etag: Optional[str]
    category: str


class StorageBillingService:
    """Stateless — every method is a read-only query, scoped to one tenant.
    Safe to call from application code and from a migration alike (a
    `Session` bound to the migration's own connection works identically to
    a request-scoped one).

    Each category is defined exactly once, as a *row* selection
    (`_*_rows()`). Byte totals are sums over those same rows, and the
    billable-object list is those same rows materialized — so there is one
    definition of "which objects are billable" and no possibility of the
    total and the object list disagreeing about it.
    """

    #: The four columns every category selection returns, in order.
    #: Everything downstream (sums, `BillableStorageObjectRef`) reads them
    #: positionally, so the order is part of this module's internal
    #: contract.
    @staticmethod
    def _row_columns():
        return (
            StorageObject.id,
            StorageObject.object_key,
            StorageObject.size_bytes,
            StorageObject.etag,
        )

    # -- per-category row selections ---------------------------------------

    @staticmethod
    def _sample_image_rows(tenant_id: UUID):
        return (
            select(*StorageBillingService._row_columns())
            .select_from(SampleImage)
            .join(StorageObject, StorageObject.id == SampleImage.storage_id)
            .where(SampleImage.tenant_id == tenant_id)
        )

    @staticmethod
    def _sample_image_rendition_rows(tenant_id: UUID):
        return (
            select(*StorageBillingService._row_columns())
            .select_from(SampleImageRendition)
            .join(SampleImage, SampleImage.id == SampleImageRendition.sample_image_id)
            .join(StorageObject, StorageObject.id == SampleImageRendition.storage_id)
            .where(SampleImage.tenant_id == tenant_id)
        )

    @staticmethod
    def _official_pdf_rows(tenant_id: UUID):
        return select(*StorageBillingService._row_columns()).where(
            StorageObject.tenant_id == tenant_id,
            StorageObject.sha256_hex.isnot(None),
        )

    @staticmethod
    def _legacy_pdf_rows(tenant_id: UUID):
        return (
            select(*StorageBillingService._row_columns())
            .select_from(ReportVersion)
            .join(Report, Report.id == ReportVersion.report_id)
            .join(StorageObject, StorageObject.id == ReportVersion.pdf_storage_id)
            .where(Report.tenant_id == tenant_id, StorageObject.sha256_hex.is_(None))
        )

    @staticmethod
    def _report_json_rows(tenant_id: UUID):
        return (
            select(*StorageBillingService._row_columns())
            .select_from(ReportVersion)
            .join(Report, Report.id == ReportVersion.report_id)
            .join(StorageObject, StorageObject.id == ReportVersion.json_storage_id)
            .where(Report.tenant_id == tenant_id)
        )

    @staticmethod
    def _tenant_logo_rows(tenant_id: UUID):
        """The current logo, via the `Tenant.logo_storage_id` FK.

        The `StorageObject.tenant_id == tenant_id` predicate is the SQL form
        of the ownership check `resolve_current_tenant_logo_storage_object`
        performs: a FK pointing at another tenant's object is never valid
        state and must not be billed to either tenant.
        """
        return (
            select(*StorageBillingService._row_columns())
            .select_from(Tenant)
            .join(StorageObject, StorageObject.id == Tenant.logo_storage_id)
            .where(Tenant.id == tenant_id, StorageObject.tenant_id == tenant_id)
        )

    @staticmethod
    def _letterhead_asset_rows(tenant_id: UUID):
        return select(*StorageBillingService._row_columns()).where(
            StorageObject.tenant_id == tenant_id,
            or_(
                *[
                    StorageObject.object_key.like(f"{prefix}%")
                    for prefix in LETTERHEAD_ASSET_KEY_PREFIXES
                ]
            ),
        )

    @staticmethod
    def _signature_rows(tenant_id: UUID):
        return (
            select(*StorageBillingService._row_columns())
            .select_from(AppUser)
            .join(StorageObject, StorageObject.id == AppUser.signature_storage_id)
            .where(AppUser.tenant_id == tenant_id)
        )

    @staticmethod
    def _categories(tenant_id: UUID) -> List[tuple]:
        """(category label, row selection) for all seven billable
        categories — the single enumeration both `compute_breakdown` and
        `get_billable_storage_objects` walk."""
        return [
            (CATEGORY_SAMPLE_IMAGE, StorageBillingService._sample_image_rows(tenant_id)),
            (
                CATEGORY_SAMPLE_IMAGE_RENDITION,
                StorageBillingService._sample_image_rendition_rows(tenant_id),
            ),
            (CATEGORY_OFFICIAL_PDF, StorageBillingService._official_pdf_rows(tenant_id)),
            (CATEGORY_LEGACY_PDF, StorageBillingService._legacy_pdf_rows(tenant_id)),
            (CATEGORY_REPORT_JSON, StorageBillingService._report_json_rows(tenant_id)),
            (CATEGORY_TENANT_LOGO, StorageBillingService._tenant_logo_rows(tenant_id)),
            (
                CATEGORY_LETTERHEAD_ASSET,
                StorageBillingService._letterhead_asset_rows(tenant_id),
            ),
            (CATEGORY_SIGNATURE, StorageBillingService._signature_rows(tenant_id)),
        ]

    # -- aggregation --------------------------------------------------------

    @staticmethod
    def _sum_rows(session: Session, rows_stmt) -> int:
        """Total `size_bytes` over a category's rows.

        Summed over the selection as a subquery rather than by rewriting the
        query with an aggregate, so the total is by construction the sum of
        exactly the rows `get_billable_storage_objects` would return.
        """
        sub = rows_stmt.subquery()
        return int(
            session.exec(select(func.coalesce(func.sum(sub.c.size_bytes), 0))).one() or 0
        )

    @staticmethod
    def _sample_images_bytes(session: Session, tenant_id: UUID) -> int:
        return StorageBillingService._sum_rows(
            session, StorageBillingService._sample_image_rows(tenant_id)
        ) + StorageBillingService._sum_rows(
            session, StorageBillingService._sample_image_rendition_rows(tenant_id)
        )

    @staticmethod
    def _official_pdf_bytes(session: Session, tenant_id: UUID) -> int:
        return StorageBillingService._sum_rows(
            session, StorageBillingService._official_pdf_rows(tenant_id)
        )

    @staticmethod
    def _legacy_pdf_bytes(session: Session, tenant_id: UUID) -> int:
        return StorageBillingService._sum_rows(
            session, StorageBillingService._legacy_pdf_rows(tenant_id)
        )

    @staticmethod
    def _report_json_bytes(session: Session, tenant_id: UUID) -> int:
        return StorageBillingService._sum_rows(
            session, StorageBillingService._report_json_rows(tenant_id)
        )

    @staticmethod
    def _tenant_logo_bytes(session: Session, tenant_id: UUID) -> int:
        return StorageBillingService._sum_rows(
            session, StorageBillingService._tenant_logo_rows(tenant_id)
        )

    @staticmethod
    def _letterhead_asset_bytes(session: Session, tenant_id: UUID) -> int:
        return StorageBillingService._sum_rows(
            session, StorageBillingService._letterhead_asset_rows(tenant_id)
        )

    @staticmethod
    def _signature_bytes(session: Session, tenant_id: UUID) -> int:
        return StorageBillingService._sum_rows(
            session, StorageBillingService._signature_rows(tenant_id)
        )

    @staticmethod
    def compute_breakdown(session: Session, tenant_id: UUID) -> BillableStorageBreakdown:
        """The full per-category breakdown for one tenant. Independent of
        whether `storage_object.tenant_id` has been backfilled — every
        category is resolved via its owning-parent join, its FK, or (for the
        categories that always had it) the column itself."""
        return BillableStorageBreakdown(
            sample_images_bytes=StorageBillingService._sample_images_bytes(session, tenant_id),
            official_pdf_bytes=StorageBillingService._official_pdf_bytes(session, tenant_id),
            legacy_pdf_bytes=StorageBillingService._legacy_pdf_bytes(session, tenant_id),
            report_json_bytes=StorageBillingService._report_json_bytes(session, tenant_id),
            tenant_logo_bytes=StorageBillingService._tenant_logo_bytes(session, tenant_id),
            letterhead_asset_bytes=StorageBillingService._letterhead_asset_bytes(session, tenant_id),
            signature_bytes=StorageBillingService._signature_bytes(session, tenant_id),
        )

    @staticmethod
    def get_billable_storage_objects(
        session: Session, tenant_id: UUID
    ) -> List[BillableStorageObjectRef]:
        """Every billable object for one tenant, as detached descriptors.

        Céluma 1.3 Phase 4, Block D — the read-only entry point
        reconciliation uses to verify billable objects against S3, so the
        billable-selection rules stay defined here and are never
        re-derived by the reconciliation engine.

        The same `StorageObject` can legitimately appear under more than one
        category (nothing forbids it), so callers that act per physical
        object should de-duplicate by key; this method reports selection,
        not physical uniqueness.
        """
        refs: List[BillableStorageObjectRef] = []
        for category, rows_stmt in StorageBillingService._categories(tenant_id):
            for storage_id, object_key, size_bytes, etag in session.exec(rows_stmt).all():
                refs.append(
                    BillableStorageObjectRef(
                        storage_object_id=storage_id,
                        object_key=object_key,
                        size_bytes=size_bytes,
                        etag=etag,
                        category=category,
                    )
                )
        return refs

    @staticmethod
    def compute_billable_storage_bytes(session: Session, tenant_id: UUID) -> int:
        """The single authoritative number: a tenant's total billable
        storage, summed across all seven categories. This is what Block C's
        initialization writes into `TenantUsage.billable_storage_bytes` and
        what any future reconciliation pass (Block D) re-derives to compare
        against the incrementally-maintained counter."""
        return StorageBillingService.compute_breakdown(session, tenant_id).total_bytes
