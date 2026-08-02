"""Export/import a `ReportLetterheadVersion` as a portable `.celuma` file —
post-Fase-2 remediation, R12/R13. See celuma-letterhead-file-format.md.
"""
import base64
import hashlib
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlmodel import Session

from app.models.report_letterhead import ReportLetterhead
from app.models.report_letterhead_version import (
    ReportLetterheadVersion,
    ReportLetterheadVersionStatus,
)
from app.models.storage import StorageObject
from app.schemas.report_letterhead import (
    CELUMA_FORMAT,
    CELUMA_FORMAT_VERSION,
    CELUMA_SUPPORTED_FORMAT_VERSIONS,
    MAX_CELUMA_LOGO_BYTES,
    CelumaLetterheadAsset,
    CelumaLetterheadEnvelope,
    CelumaLetterheadPayload,
    CelumaLetterheadSource,
)
from app.schemas.report_template_version import ReportPresentationSnapshotV2
from app.services.managed_tenant_image_service import (
    ImageRegistrationError,
    InvalidImageError,
    ManagedTenantImageService,
)
from app.services.s3 import S3Service


class CelumaPortabilityError(ValueError):
    """Raised for any `.celuma` validation failure — corrupt file, unknown
    format/version, hash mismatch, oversized asset. `message` is safe to
    surface directly to the client as an HTTP 400 body."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _export_asset(
    storage_id: Optional[str], session: Session, s3: S3Service, label: str
) -> Optional[CelumaLetterheadAsset]:
    """Embeds one referenced logo as base64 + sha256, or `None` if the
    version references no logo at that slot.

    Tercera remediación: si el `logo_storage_id` SÍ está puesto pero el
    objeto no existe (o sus bytes no están en el bucket), esto levanta en
    vez de devolver `None`. Antes fallaba en silencio y el `.cell` salía
    sin logo — el usuario solo lo descubría al importarlo en otro tenant y
    encontrarse el logo neutral, que es exactamente el síntoma del
    problema A del brief. Un export a medias es peor que un export que
    falla: el archivo parece bueno y se propaga.
    """
    if not storage_id:
        return None
    storage = session.get(StorageObject, storage_id)
    if storage is None:
        raise CelumaPortabilityError(
            f"El {label} de este membrete apunta a un archivo que ya no existe. "
            "Vuelve a subirlo antes de exportar."
        )
    try:
        logo_bytes = s3.download_bytes(storage.object_key)
    except Exception:
        raise CelumaPortabilityError(
            f"No se pudieron leer los bytes del {label} de este membrete. "
            "Vuelve a subirlo antes de exportar."
        ) from None
    return CelumaLetterheadAsset(
        media_type=storage.content_type or "image/png",
        sha256=hashlib.sha256(logo_bytes).hexdigest(),
        data_base64=base64.b64encode(logo_bytes).decode("ascii"),
    )


def export_letterhead_version(
    letterhead: ReportLetterhead,
    version: ReportLetterheadVersion,
    session: Session,
    s3: Optional[S3Service] = None,
) -> CelumaLetterheadEnvelope:
    """Builds a portable envelope (format_version 2) for a published
    letterhead version. Never includes tenant_id, StorageObject id,
    bucket/key, or any URL — header/footer logos (if any) are embedded as
    base64 bytes with a sha256 hash instead, under `assets.header_logo`/
    `assets.footer_logo`."""
    s3 = s3 or S3Service()
    presentation = ReportPresentationSnapshotV2.model_validate(version.configuration)

    assets: dict[str, CelumaLetterheadAsset] = {}
    header_asset = _export_asset(
        presentation.header.logo_storage_id, session, s3, "logo del encabezado"
    )
    if header_asset is not None:
        assets["header_logo"] = header_asset
    footer_asset = _export_asset(
        presentation.footer.logo_storage_id, session, s3, "logo del pie"
    )
    if footer_asset is not None:
        assets["footer_logo"] = footer_asset

    # `logo_storage_id` is a StorageObject id owned by the exporting tenant —
    # meaningless (and a data leak) to another tenant, so the exported
    # presentation never carries either one. Import regenerates fresh
    # StorageObjects and fresh ids.
    exported_presentation = presentation.model_copy(
        update={
            "header": presentation.header.model_copy(update={"logo_storage_id": None}),
            "footer": presentation.footer.model_copy(update={"logo_storage_id": None}),
        }
    )

    return CelumaLetterheadEnvelope(
        format=CELUMA_FORMAT,
        format_version=CELUMA_FORMAT_VERSION,
        exported_at=datetime.now(timezone.utc).isoformat(),
        source=CelumaLetterheadSource(),
        letterhead=CelumaLetterheadPayload(
            name=letterhead.name,
            description=letterhead.description,
            presentation=exported_presentation,
        ),
        assets=assets,
    )


def _import_asset(
    asset: Optional[CelumaLetterheadAsset],
    *,
    tenant_id: UUID,
    created_by: UUID,
    session: Session,
    s3: S3Service,
    label: str,
) -> Optional[str]:
    """Decodes/validates/uploads one embedded asset, returning the new
    StorageObject id, or None if `asset` is absent. Raises
    CelumaPortabilityError for any integrity/validation failure."""
    if asset is None:
        return None
    if len(asset.data_base64) > MAX_CELUMA_LOGO_BYTES * 2:
        # base64 inflates size ~4/3x; a generous pre-decode guard avoids
        # decoding an absurdly large payload just to reject it.
        raise CelumaPortabilityError(f"Embedded {label} asset is too large")
    try:
        asset_bytes = base64.b64decode(asset.data_base64, validate=True)
    except Exception:
        raise CelumaPortabilityError(f"Embedded {label} asset is not valid base64") from None

    actual_hash = hashlib.sha256(asset_bytes).hexdigest()
    if actual_hash != asset.sha256:
        raise CelumaPortabilityError(
            f"Embedded {label} asset failed integrity check (sha256 mismatch) — "
            "the file may be corrupted"
        )

    try:
        result = ManagedTenantImageService(s3).upload(
            file_bytes=asset_bytes,
            declared_content_type=asset.media_type,
            tenant_id=tenant_id,
            key_prefix="report-letterheads/imported/logos",
            created_by=created_by,
            session=session,
        )
    except InvalidImageError as exc:
        raise CelumaPortabilityError(f"Embedded {label} is invalid: {exc.message}") from None
    except ImageRegistrationError as exc:
        raise CelumaPortabilityError(str(exc)) from None
    return str(result.storage_object.id)


def import_letterhead_version(
    envelope: CelumaLetterheadEnvelope,
    *,
    tenant_id: UUID,
    created_by: UUID,
    session: Session,
    s3: Optional[S3Service] = None,
) -> ReportLetterheadVersion:
    """Validates and imports a `.cell`/`.clm`/`.celuma` envelope as a
    brand-new letterhead (never publishes into an existing one, never
    reuses ids from the source tenant). Re-validates any logo through the
    same ManagedTenantImageService path a fresh upload would use — the
    embedded content_type/hash in the file is never trusted blindly.

    Dispatches on `format_version`: 1 (`.celuma` legacy) maps the single
    `assets.logo` to `header.logo_storage_id` only, exactly as before this
    remediation. 2 (`.cell`/`.clm`, current) maps `assets.header_logo`/
    `assets.footer_logo` independently. Both converge on the same
    letterhead-creation path below.
    """
    if envelope.format != CELUMA_FORMAT:
        raise CelumaPortabilityError(f"Unrecognized file format: {envelope.format!r}")
    if envelope.format_version not in CELUMA_SUPPORTED_FORMAT_VERSIONS:
        raise CelumaPortabilityError(
            f"Unsupported .cell format version: {envelope.format_version}"
        )

    s3 = s3 or S3Service()
    presentation = envelope.letterhead.presentation

    if envelope.format_version == 1:
        header_logo_storage_id = _import_asset(
            envelope.assets.get("logo"),
            tenant_id=tenant_id,
            created_by=created_by,
            session=session,
            s3=s3,
            label="logo",
        )
        footer_logo_storage_id = None
    else:
        header_logo_storage_id = _import_asset(
            envelope.assets.get("header_logo"),
            tenant_id=tenant_id,
            created_by=created_by,
            session=session,
            s3=s3,
            label="header logo",
        )
        footer_logo_storage_id = _import_asset(
            envelope.assets.get("footer_logo"),
            tenant_id=tenant_id,
            created_by=created_by,
            session=session,
            s3=s3,
            label="footer logo",
        )

    # Los ÚNICOS campos que el import reescribe son los dos
    # `logo_storage_id` (los ids del tenant de origen no significan nada
    # aquí y se regeneran). Todo lo demás — colores, márgenes, layout,
    # tipografía, divisores, alturas, alineaciones, firmante — se persiste
    # tal cual venía en el archivo; nunca se "reconstruye con defaults".
    imported_presentation = presentation.model_copy(
        update={
            "header": presentation.header.model_copy(
                update={"logo_storage_id": header_logo_storage_id}
            ),
            "footer": presentation.footer.model_copy(
                update={"logo_storage_id": footer_logo_storage_id}
            ),
        }
    )

    new_letterhead = ReportLetterhead(
        tenant_id=tenant_id,
        name=envelope.letterhead.name,
        description=envelope.letterhead.description,
        created_by=created_by,
        is_active=True,
        # El import NUNCA marca predeterminado: eso sigue siendo una
        # decisión explícita del administrador.
        is_default=False,
    )
    session.add(new_letterhead)
    session.flush()

    new_version = ReportLetterheadVersion(
        tenant_id=tenant_id,
        report_letterhead_id=new_letterhead.id,
        version_number=1,
        schema_version=2,
        configuration=imported_presentation.model_dump(mode="json"),
        # Tercera remediación — CAUSA RAÍZ del problema A: esta versión
        # nacía PUBLISHED. Como el membrete recién importado es nuevo y no
        # tiene ninguna otra versión, se quedaba SIN versión ACTIVE, de modo
        # que `GET .../versions/active` respondía 404 y el editor arrancaba
        # desde BLANK_PRESENTATION — el usuario veía "se perdió el logo, el
        # color y el layout" cuando en realidad todo estaba correctamente
        # persistido en una versión que nadie leía. Un membrete importado
        # tiene que ser inmediatamente visible y editable; que sea el
        # predeterminado del tenant es otra decisión, y sigue siendo
        # explícita (`is_default=False` arriba).
        status=ReportLetterheadVersionStatus.ACTIVE,
        created_by=created_by,
        activated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.add(new_version)
    session.commit()
    session.refresh(new_version)
    return new_version
