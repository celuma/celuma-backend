"""fase1_rbac_schema

Revision ID: e7f8a9b0c1d2
Revises: a1b2c3d4e5f6
Create Date: 2026-03-27 00:00:00.000000

Changes:
1. Create RBAC tables: permission, role, role_permission, user_role
2. Drop app_user.role column and userrole enum type
3. Rename user_invitation.role -> role_code (VARCHAR)
4. Seed system permissions and roles
"""
from typing import Sequence, Union
import uuid as _uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

_PERMISSIONS = [
    # admin domain
    ("admin:manage_users",       "admin",   "Gestionar usuarios",        "Crear, editar y desactivar usuarios del tenant"),
    ("admin:manage_invitations", "admin",   "Gestionar invitaciones",    "Crear y enviar invitaciones a nuevos usuarios"),
    ("admin:manage_tenant",      "admin",   "Configurar tenant",         "Editar datos del laboratorio y logo"),
    ("admin:manage_branches",    "admin",   "Gestionar sucursales",      "Crear y configurar sucursales"),
    ("admin:manage_catalog",     "admin",   "Gestionar catálogo",        "CRUD tipos de estudio, precios y plantillas de reporte"),
    # lab domain
    ("lab:read",                 "lab",     "Ver laboratorio",           "Leer pacientes, órdenes, muestras y eventos"),
    ("lab:create_patient",       "lab",     "Registrar pacientes",       "Crear nuevos registros de pacientes"),
    ("lab:create_order",         "lab",     "Crear órdenes",             "Registrar nuevas órdenes de laboratorio"),
    ("lab:update_order",         "lab",     "Actualizar órdenes",        "Editar notas y cambiar estado de órdenes"),
    ("lab:create_sample",        "lab",     "Registrar muestras",        "Crear nuevas muestras"),
    ("lab:update_sample",        "lab",     "Actualizar muestras",       "Editar estado y notas de muestras"),
    ("lab:upload_images",        "lab",     "Subir imágenes",            "Cargar imágenes de muestras"),
    ("lab:delete_images",        "lab",     "Eliminar imágenes",         "Eliminar imágenes de muestras"),
    ("lab:manage_assignees",     "lab",     "Gestionar asignados",       "Asignar y remover responsables en órdenes y muestras"),
    ("lab:manage_reviewers",     "lab",     "Gestionar revisores",       "Asignar y remover revisores de informes"),
    ("lab:manage_labels",        "lab",     "Gestionar etiquetas",       "Crear y asignar etiquetas a órdenes y muestras"),
    ("lab:manage_comments",      "lab",     "Gestionar comentarios",     "Agregar comentarios a órdenes"),
    # reports domain
    ("reports:read",             "reports", "Ver reportes",              "Leer informes y plantillas"),
    ("reports:create",           "reports", "Crear reportes",            "Crear nuevos informes patológicos"),
    ("reports:edit",             "reports", "Editar reportes",           "Editar contenido y crear nuevas versiones"),
    ("reports:submit",           "reports", "Enviar a revisión",         "Enviar informe para revisión"),
    ("reports:approve",          "reports", "Aprobar / solicitar cambios", "Aprobar o solicitar cambios en revisión"),
    ("reports:sign",             "reports", "Firmar y publicar",         "Firmar y publicar informe aprobado"),
    ("reports:retract",          "reports", "Retractar informe",         "Retractar un informe publicado"),
    ("reports:manage_templates", "reports", "Gestionar plantillas",      "CRUD de plantillas y secciones de reporte"),
    # billing domain
    ("billing:read",             "billing", "Ver facturación",           "Ver facturas, pagos y saldos"),
    ("billing:create_invoice",   "billing", "Crear facturas",            "Generar nuevas facturas"),
    ("billing:edit_items",       "billing", "Editar ítems de factura",   "Agregar y modificar ítems de factura"),
    ("billing:register_payment", "billing", "Registrar pagos",           "Registrar pagos de facturas"),
    # audit domain
    ("audit:read_events",        "audit",   "Ver eventos",               "Ver línea de tiempo de órdenes y muestras"),
    ("audit:read_auditlog",      "audit",   "Ver audit log",             "Consultar registros de auditoría del sistema"),
    # portal domain
    ("portal:physician_access",  "portal",  "Acceso portal médico",      "Acceder al portal del médico solicitante"),
    ("portal:patient_access",    "portal",  "Acceso portal paciente",    "Acceder al portal del paciente por código"),
]

_ALL_PERMISSION_CODES = [p[0] for p in _PERMISSIONS]

_ROLES = [
    {
        "code": "superuser",
        "name": "Superadministrador",
        "description": "Acceso total al tenant. Uso excepcional.",
        "is_system": True,
        "is_protected": True,
        "permissions": _ALL_PERMISSION_CODES,
    },
    {
        "code": "admin",
        "name": "Administrador",
        "description": "Administración operativa del laboratorio sin privilegios clínicos de firma.",
        "is_system": True,
        "is_protected": True,
        "permissions": [
            "admin:manage_users", "admin:manage_invitations", "admin:manage_tenant",
            "admin:manage_branches", "admin:manage_catalog",
            "lab:read", "lab:create_patient", "lab:create_order", "lab:update_order",
            "lab:create_sample", "lab:update_sample", "lab:upload_images", "lab:delete_images",
            "lab:manage_assignees", "lab:manage_reviewers", "lab:manage_labels", "lab:manage_comments",
            "reports:read", "reports:manage_templates",
            "billing:read", "billing:create_invoice", "billing:edit_items", "billing:register_payment",
            "audit:read_events",
        ],
    },
    {
        "code": "pathologist",
        "name": "Patólogo",
        "description": "Diagnóstico y flujo clínico de informes, incluye firma y retractación.",
        "is_system": True,
        "is_protected": False,
        "permissions": [
            "lab:read", "lab:update_order", "lab:update_sample",
            "lab:manage_assignees", "lab:manage_reviewers", "lab:manage_labels", "lab:manage_comments",
            "reports:read", "reports:create", "reports:edit", "reports:submit",
            "reports:approve", "reports:sign", "reports:retract",
            "audit:read_events",
        ],
    },
    {
        "code": "lab_tech",
        "name": "Técnico de Laboratorio",
        "description": "Procesamiento de muestras y registro de pruebas.",
        "is_system": True,
        "is_protected": False,
        "permissions": [
            "lab:read", "lab:create_sample", "lab:update_sample",
            "lab:upload_images", "lab:delete_images",
            "lab:manage_assignees", "lab:manage_labels", "lab:manage_comments",
            "reports:read",
            "audit:read_events",
        ],
    },
    {
        "code": "assistant",
        "name": "Recepcionista / Asistente",
        "description": "Registro de pacientes, órdenes y recepción de muestras.",
        "is_system": True,
        "is_protected": False,
        "permissions": [
            "lab:read", "lab:create_patient", "lab:create_order", "lab:update_order",
            "lab:create_sample", "lab:manage_labels", "lab:manage_comments",
            "billing:read",
            "reports:read",
        ],
    },
    {
        "code": "billing",
        "name": "Facturación",
        "description": "Gestión de facturas y pagos del tenant.",
        "is_system": True,
        "is_protected": False,
        "permissions": [
            "lab:read",
            "billing:read", "billing:create_invoice", "billing:edit_items", "billing:register_payment",
            "reports:read",
        ],
    },
    {
        "code": "viewer",
        "name": "Visor",
        "description": "Acceso de solo lectura.",
        "is_system": True,
        "is_protected": False,
        "permissions": [
            "lab:read",
            "reports:read",
            "audit:read_events",
        ],
    },
    {
        "code": "physician",
        "name": "Médico Solicitante",
        "description": "Acceso al portal del médico para consultar resultados publicados.",
        "is_system": True,
        "is_protected": False,
        "permissions": [
            "portal:physician_access",
        ],
    },
]


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Create RBAC tables
    # ------------------------------------------------------------------
    op.create_table(
        "permission",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("domain", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_permission_code", "permission", ["code"], unique=True)
    op.create_index("ix_permission_domain", "permission", ["domain"])

    op.create_table(
        "role",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_protected", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_role_code", "role", ["code"], unique=True)
    op.create_index("ix_role_tenant_id", "role", ["tenant_id"])

    op.create_table(
        "role_permission",
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["permission_id"], ["permission.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["role.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_id", "permission_id"),
    )

    op.create_table(
        "user_role",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["role.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "role_id", name="uq_user_role"),
    )
    op.create_index("ix_user_role_user_id", "user_role", ["user_id"])
    op.create_index("ix_user_role_role_id", "user_role", ["role_id"])

    # ------------------------------------------------------------------
    # 2. Drop app_user.role column (does not block enum drop on its own)
    # ------------------------------------------------------------------
    op.drop_column("app_user", "role")

    # ------------------------------------------------------------------
    # 3. Rename user_invitation.role -> role_code
    #    Must happen BEFORE dropping the enum type so the cast succeeds.
    # ------------------------------------------------------------------
    op.execute(
        "ALTER TABLE user_invitation ADD COLUMN role_code VARCHAR(50)"
    )
    # Cast from enum to text so the value is preserved
    op.execute(
        "UPDATE user_invitation SET role_code = role::text"
    )
    op.execute(
        "ALTER TABLE user_invitation ALTER COLUMN role_code SET NOT NULL"
    )
    op.drop_column("user_invitation", "role")

    # ------------------------------------------------------------------
    # 4. Drop the userrole enum — now no columns depend on it
    # ------------------------------------------------------------------
    op.execute("DROP TYPE IF EXISTS userrole")

    # ------------------------------------------------------------------
    # 4. Seed permissions
    # ------------------------------------------------------------------
    permission_table = sa.table(
        "permission",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String),
        sa.column("domain", sa.String),
        sa.column("display_name", sa.String),
        sa.column("description", sa.String),
    )
    perm_id_map: dict[str, _uuid.UUID] = {}
    perm_rows = []
    for code, domain, display_name, description in _PERMISSIONS:
        uid = _uuid.uuid4()
        perm_id_map[code] = uid
        perm_rows.append({
            "id": uid,
            "code": code,
            "domain": domain,
            "display_name": display_name,
            "description": description,
        })
    op.bulk_insert(permission_table, perm_rows)

    # ------------------------------------------------------------------
    # 5. Seed roles + role_permission
    # ------------------------------------------------------------------
    from datetime import datetime
    role_table = sa.table(
        "role",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("created_at", sa.DateTime),
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
        sa.column("is_system", sa.Boolean),
        sa.column("is_protected", sa.Boolean),
        sa.column("tenant_id", postgresql.UUID(as_uuid=True)),
    )
    role_perm_table = sa.table(
        "role_permission",
        sa.column("role_id", postgresql.UUID(as_uuid=True)),
        sa.column("permission_id", postgresql.UUID(as_uuid=True)),
    )

    now = datetime.utcnow()
    for role_def in _ROLES:
        role_id = _uuid.uuid4()
        op.bulk_insert(role_table, [{
            "id": role_id,
            "created_at": now,
            "code": role_def["code"],
            "name": role_def["name"],
            "description": role_def["description"],
            "is_system": role_def["is_system"],
            "is_protected": role_def["is_protected"],
            "tenant_id": None,
        }])
        rp_rows = [
            {"role_id": role_id, "permission_id": perm_id_map[pcode]}
            for pcode in role_def["permissions"]
            if pcode in perm_id_map
        ]
        if rp_rows:
            op.bulk_insert(role_perm_table, rp_rows)


def downgrade() -> None:
    # ------------------------------------------------------------------
    # Reverse order
    # ------------------------------------------------------------------
    op.drop_table("user_role")
    op.drop_table("role_permission")
    op.drop_table("role")
    op.drop_table("permission")

    # Re-add app_user.role as text (enum type not recreated to keep downgrade simple)
    op.add_column(
        "app_user",
        sa.Column("role", sa.String(length=50), nullable=True),
    )
    op.execute("UPDATE app_user SET role = 'admin'")
    op.execute("ALTER TABLE app_user ALTER COLUMN role SET NOT NULL")

    # Restore user_invitation.role column
    op.add_column(
        "user_invitation",
        sa.Column("role", sa.String(length=50), nullable=True),
    )
    op.execute("UPDATE user_invitation SET role = role_code")
    op.execute("ALTER TABLE user_invitation ALTER COLUMN role SET NOT NULL")
    op.drop_column("user_invitation", "role_code")
