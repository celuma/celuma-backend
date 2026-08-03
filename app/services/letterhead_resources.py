"""Resolution of ephemeral letterhead resources (logo URLs) —
third post-Phase-2 remediation.

Single logo contract (see letterhead-logo-persistence-contract.md):

    persisted    -> `presentation.header.logo_storage_id`
                    `presentation.footer.logo_storage_id`
    resolved     -> `resolved_resources.header_logo_url`
                    `resolved_resources.footer_logo_url`

The URL is NEVER persisted: it is ephemeral and recomputed on every read.
Before this remediation this only existed for already-saved reports
(`_resolve_report_resources` in app/api/v1/reports.py); the letterhead
editor had no way to obtain the URL of an already-persisted logo, so
reopening always showed Céluma's neutral logo even when `logo_storage_id`
was correctly saved — the root cause of problems B and C in the brief.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlmodel import Session

from app.models.storage import StorageObject
from app.schemas.report import ReportResolvedResources
from app.services.s3 import S3Service


def _resolve_one(
    storage_id: Optional[str], tenant_id: str, session: Session, s3: S3Service
) -> Optional[str]:
    if not storage_id:
        return None
    try:
        logo_object = session.get(StorageObject, storage_id)
    except (ValueError, TypeError):
        return None
    if logo_object is None:
        return None
    # Defense in depth: the object was already validated as belonging to the
    # tenant when the version was saved; re-checking here guarantees that a
    # future bug in that validation, or a historical row from before it,
    # never leaks another tenant's logo on a read.
    if str(logo_object.tenant_id) != str(tenant_id):
        return None
    return s3.object_public_url(logo_object.object_key)


def resolve_letterhead_resources(
    configuration: Dict[str, Any],
    tenant_id: str,
    session: Session,
    s3: Optional[S3Service] = None,
) -> Optional[ReportResolvedResources]:
    """URLs for the logos in a `ReportLetterheadVersion.configuration`.

    Returns `None` when nothing is resolvable — same contract as
    `_resolve_report_resources`, so the frontend can treat both origins
    with the same code.
    """
    if not isinstance(configuration, dict):
        return None
    header = configuration.get("header")
    footer = configuration.get("footer")
    header_logo_storage_id = header.get("logo_storage_id") if isinstance(header, dict) else None
    footer_logo_storage_id = footer.get("logo_storage_id") if isinstance(footer, dict) else None
    if not header_logo_storage_id and not footer_logo_storage_id:
        return None

    s3 = s3 or S3Service()
    header_logo_url = _resolve_one(header_logo_storage_id, tenant_id, session, s3)
    footer_logo_url = _resolve_one(footer_logo_storage_id, tenant_id, session, s3)
    if header_logo_url is None and footer_logo_url is None:
        return None
    return ReportResolvedResources(
        header_logo_url=header_logo_url, footer_logo_url=footer_logo_url
    )
