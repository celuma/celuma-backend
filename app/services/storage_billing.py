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
- Tenant logo vs. letterhead/template logo: both have `tenant_id`
  populated and no FK from any versioned entity, so category is
  determined by `object_key` prefix — the one place this module reads a
  key string rather than a relational owner. This is a deliberate,
  documented exception to "do not infer tenant from S3 key strings": no
  tenant is being inferred here (tenant_id is already on the row); only
  the *category* is, because Block A/B found no other identifying signal
  for JSON-only-referenced logo objects. See storage-tenant-attribution-
  contract.md §4 for the full limitation writeup.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.core.config import settings
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


def resolve_object_key_from_public_url(url: Optional[str]) -> Optional[str]:
    """Inverse of `S3Service.object_public_url()`. Returns the S3 object key
    the given public URL was built from, or `None` if the URL does not
    match either form `object_public_url()` produces (CDN-base form when
    `MEDIA_PUBLIC_BASE_URL` is configured, raw-S3 form otherwise).

    This is the one deterministic way to recover "which StorageObject is
    the tenant's current logo" from `Tenant.logo_url` (a plain string, not
    a FK — see storage-ownership-inventory.md §1.5). It is the exact
    inverse of an existing, already-used helper, not a guess at S3 key
    structure — see storage-tenant-attribution-contract.md for the
    documented limitation (a `MEDIA_PUBLIC_BASE_URL` value change between
    when a URL was stored and when it is resolved would break this; not
    observed, not expected, and out of Block C's scope to guard against).
    """
    if not url:
        return None
    candidates = []
    if settings.media_public_base_url:
        candidates.append(settings.media_public_base_url.rstrip("/") + "/")
    region = settings.aws_region or "mx-central-1"
    if settings.s3_bucket_name:
        candidates.append(f"https://{settings.s3_bucket_name}.s3.{region}.amazonaws.com/")
    for prefix in candidates:
        if url.startswith(prefix):
            return url[len(prefix):]
    return None


def resolve_current_tenant_logo_storage_object(
    session: Session, tenant: Tenant
) -> Optional[StorageObject]:
    """The `StorageObject` currently referenced by `tenant.logo_url`, or
    `None` if the tenant has no logo or it cannot be resolved. Used both by
    the billable-storage calculation (only the *current* logo counts — see
    §12 of the master spec) and by the tenant-logo upload flow (to find the
    previous logo to decrement on replacement).
    """
    key = resolve_object_key_from_public_url(tenant.logo_url)
    if key is None:
        return None
    return session.exec(
        select(StorageObject).where(
            StorageObject.tenant_id == tenant.id,
            StorageObject.object_key == key,
        )
    ).first()


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


class StorageBillingService:
    """Stateless — every method is a read-only aggregate query, scoped to
    one tenant. Safe to call from application code and from the Block C
    migration alike (a `Session` bound to the migration's own connection
    works identically to a request-scoped one)."""

    @staticmethod
    def _sum(session: Session, stmt) -> int:
        return int(session.exec(stmt).one() or 0)

    @staticmethod
    def _sample_images_bytes(session: Session, tenant_id: UUID) -> int:
        processed = StorageBillingService._sum(
            session,
            select(func.coalesce(func.sum(StorageObject.size_bytes), 0))
            .select_from(SampleImage)
            .join(StorageObject, StorageObject.id == SampleImage.storage_id)
            .where(SampleImage.tenant_id == tenant_id),
        )
        renditions = StorageBillingService._sum(
            session,
            select(func.coalesce(func.sum(StorageObject.size_bytes), 0))
            .select_from(SampleImageRendition)
            .join(SampleImage, SampleImage.id == SampleImageRendition.sample_image_id)
            .join(StorageObject, StorageObject.id == SampleImageRendition.storage_id)
            .where(SampleImage.tenant_id == tenant_id),
        )
        return processed + renditions

    @staticmethod
    def _official_pdf_bytes(session: Session, tenant_id: UUID) -> int:
        return StorageBillingService._sum(
            session,
            select(func.coalesce(func.sum(StorageObject.size_bytes), 0)).where(
                StorageObject.tenant_id == tenant_id,
                StorageObject.sha256_hex.isnot(None),
            ),
        )

    @staticmethod
    def _legacy_pdf_bytes(session: Session, tenant_id: UUID) -> int:
        return StorageBillingService._sum(
            session,
            select(func.coalesce(func.sum(StorageObject.size_bytes), 0))
            .select_from(ReportVersion)
            .join(Report, Report.id == ReportVersion.report_id)
            .join(StorageObject, StorageObject.id == ReportVersion.pdf_storage_id)
            .where(Report.tenant_id == tenant_id, StorageObject.sha256_hex.is_(None)),
        )

    @staticmethod
    def _report_json_bytes(session: Session, tenant_id: UUID) -> int:
        return StorageBillingService._sum(
            session,
            select(func.coalesce(func.sum(StorageObject.size_bytes), 0))
            .select_from(ReportVersion)
            .join(Report, Report.id == ReportVersion.report_id)
            .join(StorageObject, StorageObject.id == ReportVersion.json_storage_id)
            .where(Report.tenant_id == tenant_id),
        )

    @staticmethod
    def _tenant_logo_bytes(session: Session, tenant_id: UUID) -> int:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None or not tenant.logo_url:
            return 0
        obj = resolve_current_tenant_logo_storage_object(session, tenant)
        return obj.size_bytes if obj and obj.size_bytes else 0

    @staticmethod
    def _letterhead_asset_bytes(session: Session, tenant_id: UUID) -> int:
        return StorageBillingService._sum(
            session,
            select(func.coalesce(func.sum(StorageObject.size_bytes), 0)).where(
                StorageObject.tenant_id == tenant_id,
                or_(
                    *[
                        StorageObject.object_key.like(f"{prefix}%")
                        for prefix in LETTERHEAD_ASSET_KEY_PREFIXES
                    ]
                ),
            ),
        )

    @staticmethod
    def _signature_bytes(session: Session, tenant_id: UUID) -> int:
        return StorageBillingService._sum(
            session,
            select(func.coalesce(func.sum(StorageObject.size_bytes), 0))
            .select_from(AppUser)
            .join(StorageObject, StorageObject.id == AppUser.signature_storage_id)
            .where(AppUser.tenant_id == tenant_id),
        )

    @staticmethod
    def compute_breakdown(session: Session, tenant_id: UUID) -> BillableStorageBreakdown:
        """The full per-category breakdown for one tenant. Independent of
        whether `storage_object.tenant_id` has been backfilled — every
        category is resolved via its owning-parent join or (for the three
        categories that already had it) the column itself."""
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
    def compute_billable_storage_bytes(session: Session, tenant_id: UUID) -> int:
        """The single authoritative number: a tenant's total billable
        storage, summed across all seven categories. This is what Block C's
        initialization writes into `TenantUsage.billable_storage_bytes` and
        what any future reconciliation pass (Block D) re-derives to compare
        against the incrementally-maintained counter."""
        return StorageBillingService.compute_breakdown(session, tenant_id).total_bytes
