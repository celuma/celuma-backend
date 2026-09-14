from pydantic import BaseModel, model_validator
from typing import Optional, Dict, Any, List
from datetime import datetime




class SignatureMetadata(BaseModel):
    """Metadata that controls the digital signature block of a report.

    Persisted inside the JSON body stored in the bucket (alongside `base` and
    `sections`), not in columns of the `report` table. Templates carry the
    defaults; reports can override them until publication.
    """
    show_signature_section: bool = False
    require_digital_signature: bool = False
    signature_url: Optional[str] = None


class ReportPresentationUpdate(BaseModel):
    """Céluma 1.3.1 Block A / A4 — the reviewer-only presentation allowlist.

    This is the ENTIRE mutable surface of `PATCH /reports/{id}/presentation`.
    It is a closed schema on purpose: the reviewer role deliberately has no
    `reports:edit`, so anything that is not one of these three fields must be
    unreachable through this route. Do not add clinical fields here — a
    reviewer who needs a content change requests changes instead.

    Every field is optional so one toggle can be changed without restating
    the others; `None` means "not submitted", not "set to null".
    """
    show_signature_section: Optional[bool] = None
    require_digital_signature: Optional[bool] = None
    letterhead_version_id: Optional[str] = None


class ReportPresentationResponse(BaseModel):
    """The effective presentation settings after the change."""
    id: str
    status: str
    show_signature_section: bool
    require_digital_signature: bool
    letterhead_version_id: Optional[str] = None


# Import ReviewerWithStatus from worklist schema
class ReviewerWithStatus(BaseModel):
    """User with review status"""
    id: str
    name: str
    email: str
    avatar_url: Optional[str] = None
    status: str  # pending, approved, rejected
    review_id: Optional[str] = None

class ReportCreate(BaseModel):
    """Schema for creating a report"""
    tenant_id: str
    branch_id: str
    order_id: str
    title: Optional[str] = None
    template: Optional[Dict[str, Any]] = None  # Snapshot of the template JSON used for this report
    created_by: Optional[str] = None
    published_at: Optional[datetime] = None
    report: Optional[Dict[str, Any]] = None  # JSON body to be uploaded to S3
    # Céluma 1.3.1 Block C (CEL-131-05): the CURRENT V2 selector — the
    # clinical `ReportTemplate` this report is built from. Its live
    # `template_json` is the creation-time source of truth for clinical
    # structure; presentation comes from
    # `resolve_effective_letterhead_version`. Neither needs a
    # `ReportTemplateVersion` to exist, so a laboratory whose template was
    # saved before its letterhead was configured can still author V2 reports.
    # Only takes effect when the tenant has reports_v2_enabled=true, and the
    # backend still builds and freezes the definitive snapshot itself — this
    # id is a selection, never a trusted snapshot. See
    # docs/celuma-1.3.1/block-c/legacy-path-inventory.md.
    template_id: Optional[str] = None
    # Céluma 1.3.1 Block C (C-8): the optimistic-concurrency token for
    # `template_id`, taken verbatim from the `template_hash` that came back with
    # the `template_json` the editor bootstrapped from.
    #
    # REQUIRED whenever `template_id` is the effective selector (see the
    # validator below). `ReportTemplate.template_json` is a mutable column and
    # `create_report` re-reads it at save time, so without this token an
    # administrator editing the template mid-session would make the report
    # freeze a clinical structure the author never worked against — and the V2
    # renderer would then silently drop the author's text. The old
    # `template_version_id` flow was immune because a published version is
    # immutable; this restores that guarantee without restoring the dependency.
    template_hash: Optional[str] = None
    # Céluma 1.3 Phase 2, Block B: caller may select a published
    # ReportTemplateVersion to create a V2 report. Only takes effect when
    # the tenant has reports_v2_enabled=true; the backend resolves,
    # validates, and freezes the definitive rendering snapshot server-side
    # — this id is a selection, never a trusted snapshot. See
    # phase-2-block-b-architecture-decision.md.
    #
    # Céluma 1.3.1 Block C: RETAINED, not removed. It is still the correct
    # way to say "build this report from exactly this historical published
    # version", it is what pre-1.3.1 clients send, and when supplied it is
    # recorded on the new `ReportVersion` as real provenance. It is no longer
    # REQUIRED: `template_id` above is the selector the editor uses now, and
    # a report created that way honestly persists
    # `template_version_id = NULL`. If both are sent, this one wins — an
    # explicit historical selection is more specific than a template-level
    # one.
    template_version_id: Optional[str] = None
    # Post-Phase-2 remediation: caller may select a published/active
    # ReportLetterheadVersion to brand this V2 report. If omitted, the
    # backend resolves one server-side (template preference -> tenant
    # default) and, if none is resolvable, falls back to the template
    # version's own embedded `presentation` — never blocked, to avoid
    # silently breaking tenants that have not adopted the letterhead
    # domain yet. See template-letterhead-association-contract.md.
    letterhead_version_id: Optional[str] = None

    @model_validator(mode="after")
    def _require_template_hash_for_the_template_id_selector(self) -> "ReportCreate":
        """Céluma 1.3.1 Block C (C-8).

        `template_hash` is mandatory for the `template_id` selector and must NOT
        be demanded of anything else:

            template_version_id present  -> immutable historical selector;
                                            no hash needed or used
            template_id, no version      -> hash REQUIRED
            neither                       -> Legacy; untouched

        Expressed as request-schema validation (422) rather than a check inside
        the route, because a `template_id` with no token is an incomplete
        request rather than a conflict — there is nothing to compare yet. The
        mismatch case is the 409, and it lives in the route where the current
        template is read.

        `template_id` is new in Céluma 1.3.1 and no released client sends it, so
        requiring the token breaks no caller. Making it optional would leave an
        unsafe no-hash variant of exactly the flow this guard exists to protect.
        """
        if self.template_id is not None and self.template_version_id is None:
            if not (self.template_hash or "").strip():
                raise ValueError(
                    "template_hash is required when creating a report from "
                    "template_id; send back the template_hash returned with the "
                    "template_json used to build the report"
                )
        return self

class ReportResolvedResources(BaseModel):
    """Céluma 1.3 Phase 2, Block C, Story C1.

    Ephemeral resources resolved server-side from a V2 report's
    `rendering_snapshot` (e.g. `presentation.header.logo_storage_id` -> a
    downloadable URL). Never persisted — recomputed on every read — and
    never written back into the snapshot stored in S3. Absent/empty for
    legacy reports and for V2 reports with nothing to resolve (e.g. no
    logo configured). See report-resource-resolution-contract.md.
    """
    header_logo_url: Optional[str] = None
    # Second post-Phase-2 remediation (UX): twin of header_logo_url for
    # presentation.footer.logo_storage_id — needed for Legacy parity
    # (Legacy's logo lives in the footer, not the header).
    footer_logo_url: Optional[str] = None


class ReportResponse(BaseModel):
    """Schema for report response"""
    id: str
    status: str
    order_id: str
    tenant_id: str
    branch_id: str

class ReportDetailResponse(BaseModel):
    """Schema for detailed report response"""
    id: str
    version_no: int | None = None
    status: str
    order_id: str
    tenant_id: str
    branch_id: str
    title: Optional[str] = None
    published_at: Optional[datetime] = None
    created_by: Optional[str] = None
    signed_by: Optional[str] = None
    signed_at: Optional[datetime] = None
    report: Optional[Dict[str, Any]] = None  # reconstructed JSON from S3
    template: Optional[Dict[str, Any]] = None  # Snapshot of the template used at creation time
    # Céluma 1.3 Phase 2, Block B: V2 metadata, sourced from ReportVersion.
    # All null for legacy reports (schema_version absent/1).
    schema_version: Optional[int] = None
    template_version_id: Optional[str] = None
    # Post-Phase-2 remediation: administrative twin of `template_version_id`
    # — which ReportLetterheadVersion produced this version's `presentation`
    # block. None for legacy reports and for V2 reports created before this
    # remediation (never backfilled).
    letterhead_version_id: Optional[str] = None
    generated_by_renderer_version: Optional[str] = None
    # Céluma 1.3 Phase 2, Block C: ephemeral, request-scoped resources
    # resolved from `report.rendering_snapshot` (never part of the snapshot
    # itself). None for legacy reports and for V2 reports with nothing to
    # resolve.
    resolved_resources: Optional["ReportResolvedResources"] = None
    # Céluma 1.3 Phase 2, Block E: official PDF artifact status, so the
    # editor/detail UI can show "Sin generar / Generando / Listo / Falló"
    # without a separate round trip. None (pdf_generation_status) means no
    # generation attempt has ever run for this version — including every
    # historical version from before this block existed.
    pdf_generation_status: Optional[str] = None
    pdf_generated_at: Optional[datetime] = None
    pdf_sha256: Optional[str] = None
    pdf_size_bytes: Optional[int] = None
    pdf_page_count: Optional[int] = None
    pdf_error_code: Optional[str] = None
    pdf_error_message: Optional[str] = None

class ReportVersionCreate(BaseModel):
    """Schema for creating a report version"""
    report_id: str
    version_no: int
    pdf_storage_id: str
    html_storage_id: Optional[str] = None
    changelog: Optional[str] = None
    authored_by: Optional[str] = None
    authored_at: Optional[datetime] = None

class ReportVersionResponse(BaseModel):
    """Schema for report version response"""
    id: str
    version_no: int
    report_id: str
    is_current: bool
    schema_version: Optional[int] = None
    template_version_id: Optional[str] = None
    letterhead_version_id: Optional[str] = None
    generated_by_renderer_version: Optional[str] = None


class ReportMetaResponse(BaseModel):
    """Lightweight report metadata for case listings."""
    id: str
    status: str
    title: Optional[str] = None
    published_at: Optional[datetime] = None
    version_no: Optional[int] = None
    has_pdf: bool = False

# Schemas for enriched list responses
class BranchRef(BaseModel):
    """Reference to a branch with basic info"""
    id: str
    name: str
    code: Optional[str] = None

class PatientRef(BaseModel):
    """Reference to a patient with basic info"""
    id: str
    full_name: str
    patient_code: str

class OrderRef(BaseModel):
    """Reference to an order with basic info"""
    id: str
    order_code: str
    status: str
    requested_by: Optional[str] = None
    patient: Optional[PatientRef] = None

class ReportListItem(BaseModel):
    """Enriched report item for list view"""
    id: str
    status: str
    tenant_id: str
    branch: BranchRef
    order: OrderRef
    title: Optional[str] = None
    published_at: Optional[datetime] = None
    created_at: Optional[str] = None
    created_by: Optional[str] = None
    signed_by: Optional[str] = None
    signed_at: Optional[datetime] = None
    version_no: Optional[int] = None
    has_pdf: bool = False
    reviewers: Optional[List[ReviewerWithStatus]] = None

class ReportsListResponse(BaseModel):
    """Response schema for reports list"""
    reports: List[ReportListItem]

# Schemas for report state transitions
class ReportStatusUpdate(BaseModel):
    """Schema for updating report status"""
    changelog: Optional[str] = None

class ReportSignRequest(BaseModel):
    """Schema for signing a report"""
    changelog: Optional[str] = None

class ReportReviewComment(BaseModel):
    """Schema for review comments"""
    comment: str
    request_changes: bool = False

class ReportActionResponse(BaseModel):
    """Generic response for report actions"""
    id: str
    status: str
    message: str


class ReportSignAndPublishResponse(ReportActionResponse):
    """Second post-Phase-2 remediation (UX): response of
    `POST /{report_id}/sign-and-publish` — the published report together
    with metadata of the just-generated official PDF (already signed), so
    the frontend does not need a second round-trip before offering the
    download."""
    pdf_generation_status: Optional[str] = None
    pdf_sha256: Optional[str] = None
    pdf_size_bytes: Optional[int] = None
    pdf_page_count: Optional[int] = None
    pdf_generated_at: Optional[datetime] = None
    # Fifth post-Phase-2 remediation: the UI must no longer *guess* which
    # version to download. Previously it read `envelope.version_no` from a
    # refreshed `/full` after publishing — a potentially stale value and,
    # in any case, indirect. Now the publish response itself says which
    # version the just-generated official PDF belongs to. See
    # sign-and-publish-response-contract.md.
    report_version_id: Optional[str] = None
    version_no: Optional[int] = None
    # True only when a downloadable PDF artifact exists for that version
    # (`pdf_storage_id` present and generation READY). The signal the UI
    # uses to show "Descargar PDF oficial" without reloading.
    official_pdf_available: bool = False


# Report Template Schemas
class ReportTemplateCreate(BaseModel):
    """Schema for creating a report template"""
    name: str
    description: Optional[str] = None
    template_json: Dict[str, Any]


class ReportTemplateUpdate(BaseModel):
    """Schema for updating a report template"""
    name: Optional[str] = None
    description: Optional[str] = None
    template_json: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    # Post-Phase-2 remediation: administrative preference only, not
    # ownership — see template-letterhead-association-contract.md. Omitting
    # a preference (or None means "no preference") falls back to the
    # tenant's default letterhead at report-creation time.
    # Second remediation UX: legacy field, read-only for old rows — the
    # app no longer writes it. Use preferred_letterhead_id.
    preferred_letterhead_version_id: Optional[str] = None
    # Second post-Phase-2 remediation (UX): the preferred logical
    # letterhead (not a concrete version) — see
    # template-simplification-contract.md.
    preferred_letterhead_id: Optional[str] = None


class ReportTemplateResponse(BaseModel):
    """Schema for basic report template response"""
    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    is_active: bool
    created_at: datetime
    preferred_letterhead_version_id: Optional[str] = None
    preferred_letterhead_id: Optional[str] = None


class ReportTemplateDetailResponse(BaseModel):
    """Schema for detailed report template response with full JSON"""
    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    template_json: Dict[str, Any]
    # Céluma 1.3.1 Block C (C-8): the optimistic-concurrency fingerprint of
    # `template_json` ABOVE — the two always describe each other, because they
    # are serialized from the same read in the same response. That is the whole
    # point of returning it here rather than from `report-defaults`: a hash
    # fetched in a different request could describe a structure the editor never
    # loaded, which is a smaller version of the race C-8 exists to close.
    #
    # Opaque to the client: echo it back unchanged on `POST /reports/` alongside
    # `template_id`. Never recompute it client-side. See
    # `app/services/report_template_hash.py`.
    template_hash: str
    created_by: Optional[str] = None
    is_active: bool
    created_at: datetime
    preferred_letterhead_version_id: Optional[str] = None
    preferred_letterhead_id: Optional[str] = None


class ReportTemplatesListResponse(BaseModel):
    """Response schema for report templates list"""
    templates: List[ReportTemplateResponse]
