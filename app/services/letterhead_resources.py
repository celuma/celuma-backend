"""Resolución de los recursos efímeros de un membrete (URLs de logo) —
tercera remediación post-Fase 2.

Contrato único de logos (ver letterhead-logo-persistence-contract.md):

    se persiste   -> `presentation.header.logo_storage_id`
                     `presentation.footer.logo_storage_id`
    se resuelve   -> `resolved_resources.header_logo_url`
                     `resolved_resources.footer_logo_url`

La URL NUNCA se persiste: es efímera y se recalcula en cada lectura. Antes
de esta remediación esto solo existía para los reportes ya guardados
(`_resolve_report_resources` en app/api/v1/reports.py); el editor de
membretes no tenía ninguna forma de obtener la URL de un logo ya
persistido, así que al reabrirlo siempre mostraba el logo neutral de
Céluma aunque el `logo_storage_id` estuviera correctamente guardado — la
causa raíz de los problemas B y C del brief.
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
    # Defensa en profundidad: el objeto ya se validó como propio del tenant
    # al guardar la versión; volver a comprobarlo aquí garantiza que un bug
    # futuro en aquella validación, o una fila histórica anterior a ella,
    # nunca filtre un logo de otro tenant en una lectura.
    if str(logo_object.tenant_id) != str(tenant_id):
        return None
    return s3.object_public_url(logo_object.object_key)


def resolve_letterhead_resources(
    configuration: Dict[str, Any],
    tenant_id: str,
    session: Session,
    s3: Optional[S3Service] = None,
) -> Optional[ReportResolvedResources]:
    """URLs de los logos de una `ReportLetterheadVersion.configuration`.

    Devuelve `None` cuando no hay nada resoluble — mismo contrato que
    `_resolve_report_resources`, para que el frontend trate ambos orígenes
    con el mismo código.
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
