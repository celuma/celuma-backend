from pydantic import BaseModel, field_validator
from typing import Any, Dict, Optional, List
from datetime import datetime


class StudyTypeCreate(BaseModel):
    """Schema for creating a study type"""
    code: str
    name: str
    description: Optional[str] = None
    is_active: Optional[bool] = True
    default_report_template_id: Optional[str] = None
    
    @field_validator('code')
    @classmethod
    def validate_code(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError('Code cannot be empty')
        if len(v) > 50:
            raise ValueError('Code cannot exceed 50 characters')
        return v.upper()
    
    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError('Name cannot be empty')
        if len(v) > 255:
            raise ValueError('Name cannot exceed 255 characters')
        return v


class StudyTypeUpdate(BaseModel):
    """Schema for updating a study type"""
    code: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    default_report_template_id: Optional[str] = None
    
    @field_validator('code')
    @classmethod
    def validate_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError('Code cannot be empty')
        if len(v) > 50:
            raise ValueError('Code cannot exceed 50 characters')
        return v.upper()
    
    @field_validator('name')
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError('Name cannot be empty')
        if len(v) > 255:
            raise ValueError('Name cannot exceed 255 characters')
        return v


class TemplateRef(BaseModel):
    """Minimal template reference"""
    id: str
    name: str


class StudyTypeResponse(BaseModel):
    """Schema for study type response"""
    id: str
    tenant_id: str
    code: str
    name: str
    description: Optional[str] = None
    is_active: bool
    created_at: datetime
    default_report_template_id: Optional[str] = None
    default_template: Optional[TemplateRef] = None


class StudyTypeDetailResponse(BaseModel):
    """Schema for detailed study type response"""
    id: str
    tenant_id: str
    code: str
    name: str
    description: Optional[str] = None
    is_active: bool
    created_at: datetime
    default_report_template_id: Optional[str] = None
    default_template: Optional[TemplateRef] = None


class StudyTypesListResponse(BaseModel):
    """Response schema for study types list"""
    study_types: List[StudyTypeResponse]


class StudyTypeRef(BaseModel):
    """Minimal study type reference for use in order responses"""
    id: str
    code: str
    name: str


class StudyTypeReportDefaultsResponse(BaseModel):
    """Post-Fase-2 remediation: one round-trip resolving everything the
    report editor needs to bootstrap a brand-new V2 report — clinical
    template, its active version, and the letterhead that would be used if
    the user does not override it. Replaces the previous 3-sequential-fetch
    dance (study type -> template -> template versions), reducing the
    number of intermediate states the editor can render in (see
    report-editor-letterhead-selection-contract.md, "Preview inicial V2").

    All fields are None when nothing is resolvable (e.g. no default
    template, no ACTIVE version, no letterhead) — the caller decides how to
    react (mirrors today's `v2ConfigBlocked` behavior), this endpoint never
    raises for an unconfigured tenant.

    Tercera remediación post-Fase 2: además del id, devuelve el
    `letterhead_id` lógico, la `presentation` ya resuelta y de dónde salió
    (`letterhead_resolution_source`). Antes el editor tenía que encadenar
    listar-membretes -> listar-versiones -> leer-versión para reconstruir
    la presentación, y si CUALQUIER paso fallaba se quedaba sin
    `presentation` y montaba Legacy en silencio. Con la presentación
    incluida aquí ese camino desaparece: o hay membrete (V2), o
    `v2_blocked_reason` dice exactamente por qué no (estado bloqueado),
    nunca Legacy. Ver deterministic-letterhead-resolution-contract.md.
    """
    template_id: Optional[str] = None
    active_template_version_id: Optional[str] = None
    letterhead_version_id: Optional[str] = None
    letterhead_name: Optional[str] = None
    letterhead_id: Optional[str] = None
    # "EXPLICIT" | "TEMPLATE_PREFERRED" | "TENANT_DEFAULT"
    letterhead_resolution_source: Optional[str] = None
    letterhead_presentation: Optional[Dict[str, Any]] = None
    # Ephemeral logo URLs for the letterhead above (never persisted).
    letterhead_resolved_resources: Optional[Dict[str, Any]] = None
    # None = V2 puede proceder. Si no:
    #   "NO_TEMPLATE"            — el tipo de estudio no tiene plantilla.
    #   "NO_ACTIVE_TEMPLATE_VERSION" — la plantilla no tiene versión activa.
    #   "NO_LETTERHEAD"          — no hay membrete predeterminado resoluble.
    #   "LETTERHEAD_MISCONFIGURED" — datos inconsistentes; ver el mensaje.
    v2_blocked_reason: Optional[str] = None
    v2_blocked_detail: Optional[str] = None
