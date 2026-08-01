from pydantic import BaseModel
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
    # Céluma 1.3 Fase 2, Bloque B: caller may select a published
    # ReportTemplateVersion to create a V2 report. Only takes effect when
    # the tenant has reports_v2_enabled=true; the backend resolves,
    # validates, and freezes the definitive rendering snapshot server-side
    # — this id is a selection, never a trusted snapshot. See
    # phase-2-block-b-architecture-decision.md.
    template_version_id: Optional[str] = None
    # Post-Fase-2 remediation: caller may select a published/active
    # ReportLetterheadVersion to brand this V2 report. If omitted, the
    # backend resolves one server-side (template preference -> tenant
    # default) and, if none is resolvable, falls back to the template
    # version's own embedded `presentation` — never blocked, to avoid
    # silently breaking tenants that have not adopted the letterhead
    # domain yet. See template-letterhead-association-contract.md.
    letterhead_version_id: Optional[str] = None

class ReportResolvedResources(BaseModel):
    """Céluma 1.3 Fase 2, Bloque C, Historia C1.

    Ephemeral resources resolved server-side from a V2 report's
    `rendering_snapshot` (e.g. `presentation.header.logo_storage_id` -> a
    downloadable URL). Never persisted — recomputed on every read — and
    never written back into the snapshot stored in S3. Absent/empty for
    legacy reports and for V2 reports with nothing to resolve (e.g. no
    logo configured). See report-resource-resolution-contract.md.
    """
    header_logo_url: Optional[str] = None
    # Segunda remediación post-Fase 2 (UX): gemelo de header_logo_url para
    # presentation.footer.logo_storage_id — necesario para paridad Legacy
    # (el logo de Legacy vive en el pie, no en el header).
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
    # Céluma 1.3 Fase 2, Bloque B: V2 metadata, sourced from ReportVersion.
    # All null for legacy reports (schema_version absent/1).
    schema_version: Optional[int] = None
    template_version_id: Optional[str] = None
    # Post-Fase-2 remediation: administrative twin of `template_version_id`
    # — which ReportLetterheadVersion produced this version's `presentation`
    # block. None for legacy reports and for V2 reports created before this
    # remediation (never backfilled).
    letterhead_version_id: Optional[str] = None
    generated_by_renderer_version: Optional[str] = None
    # Céluma 1.3 Fase 2, Bloque C: ephemeral, request-scoped resources
    # resolved from `report.rendering_snapshot` (never part of the snapshot
    # itself). None for legacy reports and for V2 reports with nothing to
    # resolve.
    resolved_resources: Optional["ReportResolvedResources"] = None
    # Céluma 1.3 Fase 2, Bloque E: official PDF artifact status, so the
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
    """Segunda remediación post-Fase 2 (UX): respuesta de
    `POST /{report_id}/sign-and-publish` — el reporte publicado junto con
    los metadatos del PDF oficial recién generado (ya firmado), para que el
    frontend no necesite un segundo round-trip antes de ofrecer la
    descarga."""
    pdf_generation_status: Optional[str] = None
    pdf_sha256: Optional[str] = None
    pdf_size_bytes: Optional[int] = None
    pdf_page_count: Optional[int] = None
    pdf_generated_at: Optional[datetime] = None


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
    # Post-Fase-2 remediation: administrative preference only, not
    # ownership — see template-letterhead-association-contract.md. Omitting
    # a preference (or None means "no preference") falls back to the
    # tenant's default letterhead at report-creation time.
    # Segunda remediación UX: campo legado, de solo lectura para filas
    # antiguas — la app ya no lo escribe. Usar preferred_letterhead_id.
    preferred_letterhead_version_id: Optional[str] = None
    # Segunda remediación post-Fase 2 (UX): el membrete lógico preferido
    # (no una versión concreta) — ver template-simplification-contract.md.
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
    created_by: Optional[str] = None
    is_active: bool
    created_at: datetime
    preferred_letterhead_version_id: Optional[str] = None
    preferred_letterhead_id: Optional[str] = None


class ReportTemplatesListResponse(BaseModel):
    """Response schema for report templates list"""
    templates: List[ReportTemplateResponse]
