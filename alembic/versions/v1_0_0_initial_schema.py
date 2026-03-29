"""v1.0.0 - Initial unified schema (squash of all prior migrations)

Revision ID: v1_0_0
Revises:
Create Date: 2026-03-28

Single baseline migration that creates the full v1.0.0 schema, replacing
the 28 incremental migrations that existed during early development.
Downgrade intentionally raises NotImplementedError — recreate from scratch
instead.
"""
from typing import Sequence, Union
import uuid as _uuid
from datetime import datetime

from alembic import op
import sqlalchemy as sa

revision: str = "v1_0_0"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# RBAC seed data (copied from e7f8a9b0c1d2_fase1_rbac_schema.py)
# ---------------------------------------------------------------------------

_PERMISSIONS = [
    # admin domain
    ("admin:manage_users",       "admin",   "Gestionar usuarios",          "Crear, editar y desactivar usuarios del tenant"),
    ("admin:manage_invitations", "admin",   "Gestionar invitaciones",      "Crear y enviar invitaciones a nuevos usuarios"),
    ("admin:manage_tenant",      "admin",   "Configurar tenant",           "Editar datos del laboratorio y logo"),
    ("admin:manage_branches",    "admin",   "Gestionar sucursales",        "Crear y configurar sucursales"),
    ("admin:manage_catalog",     "admin",   "Gestionar catálogo",          "CRUD tipos de estudio, precios y plantillas de reporte"),
    # lab domain
    ("lab:read",                 "lab",     "Ver laboratorio",             "Leer pacientes, órdenes, muestras y eventos"),
    ("lab:create_patient",       "lab",     "Registrar pacientes",         "Crear nuevos registros de pacientes"),
    ("lab:create_order",         "lab",     "Crear órdenes",               "Registrar nuevas órdenes de laboratorio"),
    ("lab:update_order",         "lab",     "Actualizar órdenes",          "Editar notas y cambiar estado de órdenes"),
    ("lab:create_sample",        "lab",     "Registrar muestras",          "Crear nuevas muestras"),
    ("lab:update_sample",        "lab",     "Actualizar muestras",         "Editar estado y notas de muestras"),
    ("lab:upload_images",        "lab",     "Subir imágenes",              "Cargar imágenes de muestras"),
    ("lab:delete_images",        "lab",     "Eliminar imágenes",           "Eliminar imágenes de muestras"),
    ("lab:manage_assignees",     "lab",     "Gestionar asignados",         "Asignar y remover responsables en órdenes y muestras"),
    ("lab:manage_reviewers",     "lab",     "Gestionar revisores",         "Asignar y remover revisores de informes"),
    ("lab:manage_labels",        "lab",     "Gestionar etiquetas",         "Crear y asignar etiquetas a órdenes y muestras"),
    ("lab:manage_comments",      "lab",     "Gestionar comentarios",       "Agregar comentarios a órdenes"),
    # reports domain
    ("reports:read",             "reports", "Ver reportes",                "Leer informes y plantillas"),
    ("reports:create",           "reports", "Crear reportes",              "Crear nuevos informes patológicos"),
    ("reports:edit",             "reports", "Editar reportes",             "Editar contenido y crear nuevas versiones"),
    ("reports:submit",           "reports", "Enviar a revisión",           "Enviar informe para revisión"),
    ("reports:approve",          "reports", "Aprobar / solicitar cambios", "Aprobar o solicitar cambios en revisión"),
    ("reports:sign",             "reports", "Firmar y publicar",           "Firmar y publicar informe aprobado"),
    ("reports:retract",          "reports", "Retractar informe",           "Retractar un informe publicado"),
    ("reports:manage_templates", "reports", "Gestionar plantillas",        "CRUD de plantillas y secciones de reporte"),
    # billing domain
    ("billing:read",             "billing", "Ver facturación",             "Ver facturas, pagos y saldos"),
    ("billing:create_invoice",   "billing", "Crear facturas",              "Generar nuevas facturas"),
    ("billing:edit_items",       "billing", "Editar ítems de factura",     "Agregar y modificar ítems de factura"),
    ("billing:register_payment", "billing", "Registrar pagos",             "Registrar pagos de facturas"),
    # audit domain
    ("audit:read_events",        "audit",   "Ver eventos",                 "Ver línea de tiempo de órdenes y muestras"),
    ("audit:read_auditlog",      "audit",   "Ver audit log",               "Consultar registros de auditoría del sistema"),
    # portal domain
    ("portal:physician_access",  "portal",  "Acceso portal médico",        "Acceder al portal del médico solicitante"),
    ("portal:patient_access",    "portal",  "Acceso portal paciente",      "Acceder al portal del paciente por código"),
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
    # 1. Enum types
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TYPE public.assignmentitemtype AS ENUM (
            'lab_order', 'sample', 'report'
        )
    """)
    op.execute("""
        CREATE TYPE public.eventtype AS ENUM (
            'ORDER_CREATED', 'SAMPLE_RECEIVED', 'SAMPLE_PREPARED', 'IMAGE_UPLOADED',
            'REPORT_CREATED', 'REPORT_SUBMITTED', 'REPORT_APPROVED', 'REPORT_CHANGES_REQUESTED',
            'REPORT_SIGNED', 'INVOICE_CREATED', 'PAYMENT_RECEIVED', 'ORDER_DELIVERED',
            'ORDER_CANCELLED', 'STATUS_CHANGED', 'NOTE_ADDED', 'SAMPLE_CREATED',
            'SAMPLE_STATE_CHANGED', 'SAMPLE_NOTES_UPDATED', 'SAMPLE_DAMAGED', 'SAMPLE_CANCELLED',
            'IMAGE_DELETED', 'ORDER_STATUS_CHANGED', 'REPORT_PUBLISHED', 'COMMENT_ADDED',
            'REPORT_VERSION_CREATED', 'REPORT_RETRACTED', 'ORDER_NOTES_UPDATED',
            'ASSIGNEES_ADDED', 'ASSIGNEES_REMOVED', 'REVIEWERS_ADDED', 'REVIEWERS_REMOVED',
            'LABELS_ADDED', 'LABELS_REMOVED'
        )
    """)
    op.execute("""
        CREATE TYPE public.orderstatus AS ENUM (
            'RECEIVED', 'PROCESSING', 'DIAGNOSIS', 'REVIEW', 'RELEASED', 'CLOSED', 'CANCELLED'
        )
    """)
    op.execute("""
        CREATE TYPE public.paymentstatus AS ENUM (
            'PENDING', 'PAID', 'FAILED', 'REFUNDED', 'PARTIAL', 'VOID'
        )
    """)
    op.execute("""
        CREATE TYPE public.reportstatus AS ENUM (
            'DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'RETRACTED'
        )
    """)
    op.execute("""
        CREATE TYPE public.reviewstatus AS ENUM (
            'PENDING', 'APPROVED', 'REJECTED'
        )
    """)
    op.execute("""
        CREATE TYPE public.samplestate AS ENUM (
            'RECEIVED', 'PROCESSING', 'READY', 'DAMAGED', 'CANCELLED'
        )
    """)
    op.execute("""
        CREATE TYPE public.sampletype AS ENUM (
            'SANGRE', 'BIOPSIA', 'LAMINILLA', 'TEJIDO', 'OTRO'
        )
    """)

    # ------------------------------------------------------------------
    # 2. Tables (leaf-first order; circular order→report handled via
    #    deferred FK added after both tables exist)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE public.tenant (
            created_at  timestamp without time zone NOT NULL,
            id          uuid NOT NULL,
            name        character varying(255) NOT NULL,
            legal_name  character varying(500),
            tax_id      character varying(50),
            logo_url    character varying(500),
            is_active   boolean DEFAULT true NOT NULL,
            CONSTRAINT tenant_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.branch (
            created_at    timestamp without time zone NOT NULL,
            id            uuid NOT NULL,
            tenant_id     uuid NOT NULL,
            code          character varying(50) NOT NULL,
            name          character varying(255) NOT NULL,
            timezone      character varying(100) NOT NULL,
            address_line1 character varying(255),
            address_line2 character varying(255),
            city          character varying(100),
            state         character varying(100),
            postal_code   character varying(20),
            country       character varying(2) NOT NULL,
            is_active     boolean NOT NULL,
            CONSTRAINT branch_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.storage_object (
            created_at   timestamp without time zone NOT NULL,
            id           uuid NOT NULL,
            provider     character varying(50) NOT NULL,
            region       character varying(100) NOT NULL,
            bucket       character varying(255) NOT NULL,
            object_key   character varying(1000) NOT NULL,
            version_id   character varying(255),
            etag         character varying(255),
            sha256_hex   character varying(64),
            content_type character varying(255),
            size_bytes   integer,
            created_by   uuid,
            CONSTRAINT storage_object_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.app_user (
            created_at       timestamp without time zone NOT NULL,
            id               uuid NOT NULL,
            tenant_id        uuid NOT NULL,
            email            character varying(255) NOT NULL,
            full_name        character varying(255) NOT NULL,
            hashed_password  character varying(255) NOT NULL,
            is_active        boolean NOT NULL,
            username         character varying(50),
            avatar_url       character varying(500),
            first_name       character varying(255) DEFAULT ''::character varying NOT NULL,
            last_name        character varying(255) DEFAULT ''::character varying NOT NULL,
            CONSTRAINT app_user_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.blacklisted_token (
            id             uuid NOT NULL,
            token          character varying(1000) NOT NULL,
            user_id        uuid NOT NULL,
            expires_at     timestamp without time zone NOT NULL,
            blacklisted_at timestamp without time zone NOT NULL,
            created_at     timestamp without time zone NOT NULL,
            updated_at     timestamp without time zone NOT NULL,
            CONSTRAINT blacklisted_token_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.password_reset_token (
            created_at timestamp without time zone NOT NULL,
            id         uuid NOT NULL,
            user_id    uuid NOT NULL,
            token      character varying(255) NOT NULL,
            expires_at timestamp without time zone NOT NULL,
            is_used    boolean NOT NULL,
            used_at    timestamp without time zone,
            ip_address character varying(45),
            CONSTRAINT password_reset_token_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.patient (
            tenant_id    uuid NOT NULL,
            branch_id    uuid NOT NULL,
            created_at   timestamp without time zone NOT NULL,
            id           uuid NOT NULL,
            patient_code character varying(100) NOT NULL,
            first_name   character varying(255) NOT NULL,
            last_name    character varying(255) NOT NULL,
            dob          date,
            sex          character varying(10),
            phone        character varying(20),
            email        character varying(255),
            full_name    character varying(255),
            address      character varying(500),
            CONSTRAINT patient_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.report_template (
            created_at    timestamp without time zone NOT NULL,
            id            uuid NOT NULL,
            tenant_id     uuid NOT NULL,
            name          character varying(255) NOT NULL,
            description   character varying,
            template_json json NOT NULL,
            created_by    uuid,
            is_active     boolean NOT NULL,
            CONSTRAINT report_template_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.study_type (
            created_at                  timestamp without time zone NOT NULL,
            id                          uuid NOT NULL,
            tenant_id                   uuid NOT NULL,
            code                        character varying(50) NOT NULL,
            name                        character varying(255) NOT NULL,
            description                 character varying,
            is_active                   boolean NOT NULL,
            default_report_template_id  uuid,
            CONSTRAINT study_type_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.label (
            id         uuid NOT NULL,
            name       character varying(100) NOT NULL,
            color      character varying(7) NOT NULL,
            tenant_id  uuid NOT NULL,
            created_at timestamp without time zone NOT NULL,
            updated_at timestamp without time zone NOT NULL,
            CONSTRAINT label_pkey PRIMARY KEY (id)
        )
    """)

    # order and report are mutually FK-referencing; create both without
    # the cross-reference first, then add the deferred FKs below.
    op.execute("""
        CREATE TABLE public."order" (
            created_at   timestamp without time zone NOT NULL,
            id           uuid NOT NULL,
            tenant_id    uuid NOT NULL,
            branch_id    uuid NOT NULL,
            patient_id   uuid NOT NULL,
            order_code   character varying(100) NOT NULL,
            status       public.orderstatus NOT NULL,
            requested_by character varying(255),
            notes        character varying,
            billed_lock  boolean NOT NULL,
            created_by   uuid,
            report_id    uuid,
            study_type_id uuid,
            invoice_id   uuid,
            CONSTRAINT lab_order_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.report (
            created_at   timestamp without time zone NOT NULL,
            id           uuid NOT NULL,
            tenant_id    uuid NOT NULL,
            branch_id    uuid NOT NULL,
            order_id     uuid NOT NULL,
            status       public.reportstatus NOT NULL,
            title        character varying(500),
            published_at timestamp without time zone,
            created_by   uuid,
            template     json,
            CONSTRAINT report_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.report_version (
            created_at    timestamp without time zone NOT NULL,
            id            uuid NOT NULL,
            report_id     uuid NOT NULL,
            version_no    integer NOT NULL,
            pdf_storage_id  uuid,
            html_storage_id uuid,
            changelog     character varying,
            authored_by   uuid,
            authored_at   timestamp without time zone NOT NULL,
            is_current    boolean NOT NULL,
            json_storage_id uuid,
            signed_by     uuid,
            signed_at     timestamp without time zone,
            CONSTRAINT report_version_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.report_review (
            id                   uuid NOT NULL,
            tenant_id            uuid NOT NULL,
            order_id             uuid NOT NULL,
            reviewer_user_id     uuid NOT NULL,
            assigned_by_user_id  uuid,
            assigned_at          timestamp without time zone DEFAULT now() NOT NULL,
            decision_at          timestamp without time zone,
            status               public.reviewstatus DEFAULT 'PENDING'::public.reviewstatus NOT NULL,
            report_id            uuid,
            CONSTRAINT report_review_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.report_section (
            created_at       timestamp without time zone NOT NULL,
            id               uuid NOT NULL,
            tenant_id        uuid NOT NULL,
            created_by       uuid,
            section          character varying(255) NOT NULL,
            description      character varying,
            predefined_text  character varying,
            CONSTRAINT report_section_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.sample (
            id           uuid NOT NULL,
            tenant_id    uuid NOT NULL,
            branch_id    uuid NOT NULL,
            order_id     uuid NOT NULL,
            sample_code  character varying(100) NOT NULL,
            type         public.sampletype NOT NULL,
            state        public.samplestate NOT NULL,
            collected_at timestamp without time zone,
            received_at  timestamp without time zone,
            notes        character varying,
            CONSTRAINT sample_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.sample_image (
            created_at timestamp without time zone NOT NULL,
            id         uuid NOT NULL,
            tenant_id  uuid NOT NULL,
            branch_id  uuid NOT NULL,
            sample_id  uuid NOT NULL,
            storage_id uuid NOT NULL,
            label      character varying(255),
            is_primary boolean NOT NULL,
            created_by uuid,
            CONSTRAINT sample_image_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.sample_image_rendition (
            id              uuid NOT NULL,
            sample_image_id uuid NOT NULL,
            kind            character varying(50) NOT NULL,
            storage_id      uuid NOT NULL,
            CONSTRAINT sample_image_rendition_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.invoice (
            created_at     timestamp without time zone NOT NULL,
            id             uuid NOT NULL,
            tenant_id      uuid NOT NULL,
            branch_id      uuid NOT NULL,
            order_id       uuid NOT NULL,
            invoice_number character varying(100) NOT NULL,
            amount_total   numeric(12,2) NOT NULL,
            currency       character varying(3) NOT NULL,
            status         public.paymentstatus NOT NULL,
            issued_at      timestamp without time zone NOT NULL,
            subtotal       numeric(12,2) NOT NULL,
            discount_total numeric(12,2) NOT NULL,
            tax_total      numeric(12,2) NOT NULL,
            total          numeric(12,2) NOT NULL,
            amount_paid    numeric(12,2) NOT NULL,
            updated_at     timestamp without time zone,
            paid_at        timestamp without time zone,
            CONSTRAINT invoice_pkey PRIMARY KEY (id),
            CONSTRAINT uq_invoice_order_id UNIQUE (order_id)
        )
    """)

    op.execute("""
        CREATE TABLE public.invoice_item (
            created_at    timestamp without time zone NOT NULL,
            id            uuid NOT NULL,
            tenant_id     uuid NOT NULL,
            invoice_id    uuid NOT NULL,
            description   character varying(500) NOT NULL,
            quantity      integer NOT NULL,
            unit_price    numeric(12,2) NOT NULL,
            subtotal      numeric(12,2) NOT NULL,
            study_type_id uuid,
            CONSTRAINT invoice_item_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.payment (
            created_at  timestamp without time zone NOT NULL,
            id          uuid NOT NULL,
            tenant_id   uuid NOT NULL,
            invoice_id  uuid NOT NULL,
            method      character varying(100),
            currency    character varying(3) NOT NULL,
            reference   character varying(255),
            created_by  uuid,
            amount      numeric(12,2) NOT NULL,
            received_at timestamp without time zone NOT NULL,
            CONSTRAINT payment_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.price_catalog (
            created_at      timestamp without time zone NOT NULL,
            id              uuid NOT NULL,
            tenant_id       uuid NOT NULL,
            study_type_id   uuid NOT NULL,
            unit_price      numeric(12,2) NOT NULL,
            currency        character varying(3) NOT NULL,
            is_active       boolean NOT NULL,
            effective_from  timestamp without time zone,
            effective_to    timestamp without time zone,
            CONSTRAINT price_catalog_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.order_event (
            tenant_id      uuid NOT NULL,
            branch_id      uuid NOT NULL,
            created_at     timestamp without time zone NOT NULL,
            id             uuid NOT NULL,
            order_id       uuid NOT NULL,
            event_type     public.eventtype NOT NULL,
            description    character varying(500) NOT NULL,
            event_metadata json,
            created_by     uuid,
            sample_id      uuid,
            CONSTRAINT case_event_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.audit_log (
            created_at     timestamp without time zone NOT NULL,
            id             uuid NOT NULL,
            tenant_id      uuid NOT NULL,
            branch_id      uuid,
            actor_user_id  uuid,
            action         character varying(255) NOT NULL,
            entity_type    character varying(100) NOT NULL,
            entity_id      uuid NOT NULL,
            old_values     json,
            new_values     json,
            ip_address     character varying(45),
            order_id       uuid,
            sample_id      uuid,
            event_type     public.eventtype,
            description    character varying(500),
            event_metadata json,
            created_by     uuid,
            CONSTRAINT audit_log_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.order_comment (
            id               uuid NOT NULL,
            tenant_id        uuid NOT NULL,
            branch_id        uuid NOT NULL,
            order_id         uuid NOT NULL,
            created_at       timestamp with time zone DEFAULT now() NOT NULL,
            created_by       uuid NOT NULL,
            text             text NOT NULL,
            comment_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
            edited_at        timestamp with time zone,
            deleted_at       timestamp with time zone,
            CONSTRAINT order_comment_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.order_comment_mention (
            comment_id uuid NOT NULL,
            user_id    uuid NOT NULL,
            CONSTRAINT order_comment_mention_pkey PRIMARY KEY (comment_id, user_id)
        )
    """)

    op.execute("""
        CREATE TABLE public.order_labels (
            order_id uuid NOT NULL,
            label_id uuid NOT NULL,
            CONSTRAINT lab_order_labels_pkey PRIMARY KEY (order_id, label_id)
        )
    """)

    op.execute("""
        CREATE TABLE public.sample_labels (
            sample_id uuid NOT NULL,
            label_id  uuid NOT NULL,
            CONSTRAINT sample_labels_pkey PRIMARY KEY (sample_id, label_id)
        )
    """)

    op.execute("""
        CREATE TABLE public.assignment (
            id                   uuid NOT NULL,
            tenant_id            uuid NOT NULL,
            item_type            public.assignmentitemtype NOT NULL,
            item_id              uuid NOT NULL,
            assignee_user_id     uuid NOT NULL,
            assigned_by_user_id  uuid,
            assigned_at          timestamp without time zone DEFAULT now() NOT NULL,
            unassigned_at        timestamp without time zone,
            CONSTRAINT assignment_pkey PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE TABLE public.user_branch (
            user_id   uuid NOT NULL,
            branch_id uuid NOT NULL,
            CONSTRAINT user_branch_pkey PRIMARY KEY (user_id, branch_id)
        )
    """)

    op.execute("""
        CREATE TABLE public.user_invitation (
            created_at  timestamp without time zone NOT NULL,
            id          uuid NOT NULL,
            tenant_id   uuid NOT NULL,
            email       character varying(255) NOT NULL,
            full_name   character varying(255) NOT NULL,
            token       character varying(255) NOT NULL,
            expires_at  timestamp without time zone NOT NULL,
            accepted_at timestamp without time zone,
            is_used     boolean NOT NULL,
            invited_by  uuid,
            role_code   character varying(50) NOT NULL,
            CONSTRAINT user_invitation_pkey PRIMARY KEY (id)
        )
    """)

    # RBAC tables
    op.execute("""
        CREATE TABLE public.permission (
            id           uuid NOT NULL,
            code         character varying(100) NOT NULL,
            domain       character varying(50) NOT NULL,
            display_name character varying(255) NOT NULL,
            description  character varying(500),
            CONSTRAINT permission_pkey PRIMARY KEY (id),
            CONSTRAINT permission_code_key UNIQUE (code)
        )
    """)

    op.execute("""
        CREATE TABLE public.role (
            id           uuid NOT NULL,
            created_at   timestamp without time zone NOT NULL,
            code         character varying(50) NOT NULL,
            name         character varying(255) NOT NULL,
            description  character varying(500),
            is_system    boolean DEFAULT false NOT NULL,
            is_protected boolean DEFAULT false NOT NULL,
            tenant_id    uuid,
            CONSTRAINT role_pkey PRIMARY KEY (id),
            CONSTRAINT role_code_key UNIQUE (code)
        )
    """)

    op.execute("""
        CREATE TABLE public.role_permission (
            role_id       uuid NOT NULL,
            permission_id uuid NOT NULL,
            CONSTRAINT role_permission_pkey PRIMARY KEY (role_id, permission_id)
        )
    """)

    op.execute("""
        CREATE TABLE public.user_role (
            id         uuid NOT NULL,
            created_at timestamp without time zone NOT NULL,
            user_id    uuid NOT NULL,
            role_id    uuid NOT NULL,
            CONSTRAINT user_role_pkey PRIMARY KEY (id),
            CONSTRAINT uq_user_role UNIQUE (user_id, role_id)
        )
    """)

    # ------------------------------------------------------------------
    # 3. Unique constraints not inline above
    # ------------------------------------------------------------------
    op.execute('ALTER TABLE ONLY public."order" ADD CONSTRAINT uq_order_report_id UNIQUE (report_id)')

    # ------------------------------------------------------------------
    # 4. Indexes
    # ------------------------------------------------------------------
    op.execute("CREATE INDEX ix_app_user_email ON public.app_user USING btree (email)")
    op.execute("CREATE INDEX ix_app_user_username ON public.app_user USING btree (username)")

    op.execute("CREATE INDEX ix_assignment_assignee ON public.assignment USING btree (assignee_user_id)")
    op.execute("CREATE INDEX ix_assignment_item ON public.assignment USING btree (item_type, item_id)")
    op.execute("CREATE INDEX ix_assignment_tenant_assignee ON public.assignment USING btree (tenant_id, assignee_user_id, assigned_at DESC)")
    op.execute("CREATE INDEX ix_assignment_tenant_id ON public.assignment USING btree (tenant_id)")
    op.execute("CREATE INDEX ix_assignment_tenant_item ON public.assignment USING btree (tenant_id, item_type, item_id)")
    op.execute("CREATE UNIQUE INDEX ix_assignment_unique_active ON public.assignment USING btree (tenant_id, item_type, item_id, assignee_user_id) WHERE (unassigned_at IS NULL)")

    op.execute("CREATE INDEX ix_audit_log_event_type ON public.audit_log USING btree (event_type)")
    op.execute("CREATE INDEX ix_audit_log_order_id ON public.audit_log USING btree (order_id)")
    op.execute("CREATE INDEX ix_audit_log_sample_id ON public.audit_log USING btree (sample_id)")

    op.execute("CREATE UNIQUE INDEX ix_blacklisted_token_token ON public.blacklisted_token USING btree (token)")

    op.execute("CREATE INDEX ix_lab_order_labels_label_id ON public.order_labels USING btree (label_id)")
    op.execute("CREATE INDEX ix_lab_order_labels_order_id ON public.order_labels USING btree (order_id)")

    op.execute("CREATE INDEX ix_label_name ON public.label USING btree (name)")
    op.execute("CREATE INDEX ix_label_tenant_id ON public.label USING btree (tenant_id)")

    op.execute("CREATE INDEX idx_order_comment_created_by ON public.order_comment USING btree (tenant_id, created_by, created_at DESC)")
    op.execute("CREATE INDEX idx_order_comment_mention_user ON public.order_comment_mention USING btree (user_id)")
    op.execute("CREATE INDEX idx_order_comment_order_cursor ON public.order_comment USING btree (tenant_id, order_id, created_at DESC, id DESC)")

    op.execute("CREATE INDEX idx_order_study_type ON public.\"order\" USING btree (study_type_id)")
    op.execute("CREATE INDEX ix_order_event_sample_id ON public.order_event USING btree (sample_id)")

    op.execute("CREATE UNIQUE INDEX ix_password_reset_token_token ON public.password_reset_token USING btree (token)")

    op.execute("CREATE UNIQUE INDEX ix_permission_code ON public.permission USING btree (code)")
    op.execute("CREATE INDEX ix_permission_domain ON public.permission USING btree (domain)")

    op.execute("CREATE INDEX ix_price_catalog_study_type_id ON public.price_catalog USING btree (study_type_id)")
    op.execute("CREATE INDEX ix_price_catalog_tenant_id ON public.price_catalog USING btree (tenant_id)")

    op.execute("CREATE INDEX ix_report_review_order ON public.report_review USING btree (order_id)")
    op.execute("CREATE INDEX ix_report_review_report_id ON public.report_review USING btree (report_id)")
    op.execute("CREATE INDEX ix_report_review_reviewer ON public.report_review USING btree (reviewer_user_id)")
    op.execute("CREATE INDEX ix_report_review_status ON public.report_review USING btree (status)")
    op.execute("CREATE INDEX ix_report_review_tenant_id ON public.report_review USING btree (tenant_id)")
    op.execute("CREATE INDEX ix_report_review_tenant_order ON public.report_review USING btree (tenant_id, order_id)")
    op.execute("CREATE INDEX ix_report_review_tenant_reviewer ON public.report_review USING btree (tenant_id, reviewer_user_id, status, assigned_at DESC)")
    op.execute("CREATE UNIQUE INDEX ix_report_review_unique_pending ON public.report_review USING btree (tenant_id, order_id, reviewer_user_id) WHERE (status = 'PENDING'::public.reviewstatus)")

    op.execute("CREATE UNIQUE INDEX ix_role_code ON public.role USING btree (code)")
    op.execute("CREATE INDEX ix_role_tenant_id ON public.role USING btree (tenant_id)")

    op.execute("CREATE INDEX idx_report_section_tenant ON public.report_section USING btree (tenant_id)")
    op.execute("CREATE INDEX idx_report_template_tenant ON public.report_template USING btree (tenant_id)")
    op.execute("CREATE INDEX idx_report_template_tenant_active ON public.report_template USING btree (tenant_id, is_active)")

    op.execute("CREATE INDEX ix_sample_labels_label_id ON public.sample_labels USING btree (label_id)")
    op.execute("CREATE INDEX ix_sample_labels_sample_id ON public.sample_labels USING btree (sample_id)")

    op.execute("CREATE INDEX idx_study_type_code ON public.study_type USING btree (code)")
    op.execute("CREATE INDEX idx_study_type_template ON public.study_type USING btree (default_report_template_id)")
    op.execute("CREATE INDEX idx_study_type_tenant ON public.study_type USING btree (tenant_id)")
    op.execute("CREATE INDEX idx_study_type_tenant_active ON public.study_type USING btree (tenant_id, is_active)")

    op.execute("CREATE INDEX ix_user_invitation_email ON public.user_invitation USING btree (email)")
    op.execute("CREATE UNIQUE INDEX ix_user_invitation_token ON public.user_invitation USING btree (token)")

    op.execute("CREATE INDEX ix_user_role_role_id ON public.user_role USING btree (role_id)")
    op.execute("CREATE INDEX ix_user_role_user_id ON public.user_role USING btree (user_id)")

    # ------------------------------------------------------------------
    # 5. Foreign keys
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE ONLY public.app_user ADD CONSTRAINT app_user_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.storage_object ADD CONSTRAINT storage_object_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id)")

    op.execute("ALTER TABLE ONLY public.branch ADD CONSTRAINT branch_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.user_branch ADD CONSTRAINT user_branch_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id)")
    op.execute("ALTER TABLE ONLY public.user_branch ADD CONSTRAINT user_branch_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id)")

    op.execute("ALTER TABLE ONLY public.blacklisted_token ADD CONSTRAINT blacklisted_token_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id)")
    op.execute("ALTER TABLE ONLY public.password_reset_token ADD CONSTRAINT password_reset_token_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id)")

    op.execute("ALTER TABLE ONLY public.patient ADD CONSTRAINT patient_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.patient ADD CONSTRAINT patient_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id)")

    op.execute("ALTER TABLE ONLY public.report_template ADD CONSTRAINT report_template_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.report_template ADD CONSTRAINT report_template_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id)")

    op.execute("ALTER TABLE ONLY public.study_type ADD CONSTRAINT study_type_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.study_type ADD CONSTRAINT fk_study_type_report_template FOREIGN KEY (default_report_template_id) REFERENCES public.report_template(id)")

    op.execute("ALTER TABLE ONLY public.label ADD CONSTRAINT label_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")

    op.execute('ALTER TABLE ONLY public."order" ADD CONSTRAINT lab_order_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)')
    op.execute('ALTER TABLE ONLY public."order" ADD CONSTRAINT lab_order_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id)')
    op.execute('ALTER TABLE ONLY public."order" ADD CONSTRAINT lab_order_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient(id)')
    op.execute('ALTER TABLE ONLY public."order" ADD CONSTRAINT lab_order_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id)')
    op.execute('ALTER TABLE ONLY public."order" ADD CONSTRAINT fk_order_study_type FOREIGN KEY (study_type_id) REFERENCES public.study_type(id)')

    op.execute("ALTER TABLE ONLY public.report ADD CONSTRAINT report_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute('ALTER TABLE ONLY public.report ADD CONSTRAINT report_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id)')
    op.execute('ALTER TABLE ONLY public.report ADD CONSTRAINT report_order_id_fkey FOREIGN KEY (order_id) REFERENCES public."order"(id)')
    op.execute('ALTER TABLE ONLY public.report ADD CONSTRAINT report_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id)')

    # Circular: order.report_id → report.id  and  order.invoice_id → invoice.id (deferred)
    op.execute('ALTER TABLE ONLY public."order" ADD CONSTRAINT fk_order_report_id FOREIGN KEY (report_id) REFERENCES public.report(id)')

    op.execute("ALTER TABLE ONLY public.report_version ADD CONSTRAINT report_version_report_id_fkey FOREIGN KEY (report_id) REFERENCES public.report(id)")
    op.execute("ALTER TABLE ONLY public.report_version ADD CONSTRAINT report_version_authored_by_fkey FOREIGN KEY (authored_by) REFERENCES public.app_user(id)")
    op.execute("ALTER TABLE ONLY public.report_version ADD CONSTRAINT report_version_signed_by_fkey FOREIGN KEY (signed_by) REFERENCES public.app_user(id)")
    op.execute("ALTER TABLE ONLY public.report_version ADD CONSTRAINT report_version_pdf_storage_id_fkey FOREIGN KEY (pdf_storage_id) REFERENCES public.storage_object(id)")
    op.execute("ALTER TABLE ONLY public.report_version ADD CONSTRAINT report_version_html_storage_id_fkey FOREIGN KEY (html_storage_id) REFERENCES public.storage_object(id)")
    op.execute("ALTER TABLE ONLY public.report_version ADD CONSTRAINT report_version_json_storage_id_fkey FOREIGN KEY (json_storage_id) REFERENCES public.storage_object(id)")

    op.execute("ALTER TABLE ONLY public.report_review ADD CONSTRAINT report_review_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute('ALTER TABLE ONLY public.report_review ADD CONSTRAINT report_review_order_id_fkey FOREIGN KEY (order_id) REFERENCES public."order"(id)')
    op.execute("ALTER TABLE ONLY public.report_review ADD CONSTRAINT report_review_reviewer_user_id_fkey FOREIGN KEY (reviewer_user_id) REFERENCES public.app_user(id)")
    op.execute("ALTER TABLE ONLY public.report_review ADD CONSTRAINT report_review_assigned_by_user_id_fkey FOREIGN KEY (assigned_by_user_id) REFERENCES public.app_user(id)")
    op.execute("ALTER TABLE ONLY public.report_review ADD CONSTRAINT fk_report_review_report_id FOREIGN KEY (report_id) REFERENCES public.report(id)")

    op.execute("ALTER TABLE ONLY public.report_section ADD CONSTRAINT report_section_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.report_section ADD CONSTRAINT report_section_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id)")

    op.execute('ALTER TABLE ONLY public.sample ADD CONSTRAINT sample_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)')
    op.execute('ALTER TABLE ONLY public.sample ADD CONSTRAINT sample_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id)')
    op.execute('ALTER TABLE ONLY public.sample ADD CONSTRAINT sample_order_id_fkey FOREIGN KEY (order_id) REFERENCES public."order"(id)')

    op.execute("ALTER TABLE ONLY public.sample_image ADD CONSTRAINT sample_image_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.sample_image ADD CONSTRAINT sample_image_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id)")
    op.execute("ALTER TABLE ONLY public.sample_image ADD CONSTRAINT sample_image_sample_id_fkey FOREIGN KEY (sample_id) REFERENCES public.sample(id)")
    op.execute("ALTER TABLE ONLY public.sample_image ADD CONSTRAINT sample_image_storage_id_fkey FOREIGN KEY (storage_id) REFERENCES public.storage_object(id)")
    op.execute("ALTER TABLE ONLY public.sample_image ADD CONSTRAINT sample_image_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id)")

    op.execute("ALTER TABLE ONLY public.sample_image_rendition ADD CONSTRAINT sample_image_rendition_sample_image_id_fkey FOREIGN KEY (sample_image_id) REFERENCES public.sample_image(id)")
    op.execute("ALTER TABLE ONLY public.sample_image_rendition ADD CONSTRAINT sample_image_rendition_storage_id_fkey FOREIGN KEY (storage_id) REFERENCES public.storage_object(id)")

    op.execute("ALTER TABLE ONLY public.invoice ADD CONSTRAINT invoice_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.invoice ADD CONSTRAINT invoice_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id)")
    op.execute('ALTER TABLE ONLY public.invoice ADD CONSTRAINT invoice_order_id_fkey FOREIGN KEY (order_id) REFERENCES public."order"(id)')

    op.execute('ALTER TABLE ONLY public."order" ADD CONSTRAINT order_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoice(id)')

    op.execute("ALTER TABLE ONLY public.invoice_item ADD CONSTRAINT invoice_item_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.invoice_item ADD CONSTRAINT invoice_item_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoice(id)")
    op.execute("ALTER TABLE ONLY public.invoice_item ADD CONSTRAINT invoice_item_study_type_id_fkey FOREIGN KEY (study_type_id) REFERENCES public.study_type(id)")

    op.execute("ALTER TABLE ONLY public.payment ADD CONSTRAINT payment_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.payment ADD CONSTRAINT payment_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoice(id)")
    op.execute("ALTER TABLE ONLY public.payment ADD CONSTRAINT payment_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id)")

    op.execute("ALTER TABLE ONLY public.price_catalog ADD CONSTRAINT price_catalog_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.price_catalog ADD CONSTRAINT price_catalog_study_type_id_fkey FOREIGN KEY (study_type_id) REFERENCES public.study_type(id)")

    op.execute('ALTER TABLE ONLY public.order_event ADD CONSTRAINT case_event_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)')
    op.execute('ALTER TABLE ONLY public.order_event ADD CONSTRAINT case_event_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id)')
    op.execute('ALTER TABLE ONLY public.order_event ADD CONSTRAINT case_event_order_id_fkey FOREIGN KEY (order_id) REFERENCES public."order"(id)')
    op.execute('ALTER TABLE ONLY public.order_event ADD CONSTRAINT case_event_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id)')
    op.execute('ALTER TABLE ONLY public.order_event ADD CONSTRAINT fk_case_event_sample_id FOREIGN KEY (sample_id) REFERENCES public.sample(id)')

    op.execute("ALTER TABLE ONLY public.audit_log ADD CONSTRAINT audit_log_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.audit_log ADD CONSTRAINT audit_log_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id)")
    op.execute("ALTER TABLE ONLY public.audit_log ADD CONSTRAINT audit_log_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.app_user(id)")
    op.execute('ALTER TABLE ONLY public.audit_log ADD CONSTRAINT fk_audit_log_order_id FOREIGN KEY (order_id) REFERENCES public."order"(id)')
    op.execute("ALTER TABLE ONLY public.audit_log ADD CONSTRAINT fk_audit_log_sample_id FOREIGN KEY (sample_id) REFERENCES public.sample(id)")
    op.execute("ALTER TABLE ONLY public.audit_log ADD CONSTRAINT fk_audit_log_created_by FOREIGN KEY (created_by) REFERENCES public.app_user(id)")

    op.execute("ALTER TABLE ONLY public.order_comment ADD CONSTRAINT order_comment_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.order_comment ADD CONSTRAINT order_comment_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.branch(id)")
    op.execute('ALTER TABLE ONLY public.order_comment ADD CONSTRAINT order_comment_order_id_fkey FOREIGN KEY (order_id) REFERENCES public."order"(id)')
    op.execute("ALTER TABLE ONLY public.order_comment ADD CONSTRAINT order_comment_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id)")
    op.execute("ALTER TABLE ONLY public.order_comment_mention ADD CONSTRAINT order_comment_mention_comment_id_fkey FOREIGN KEY (comment_id) REFERENCES public.order_comment(id) ON DELETE CASCADE")
    op.execute("ALTER TABLE ONLY public.order_comment_mention ADD CONSTRAINT order_comment_mention_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id)")

    op.execute('ALTER TABLE ONLY public.order_labels ADD CONSTRAINT lab_order_labels_order_id_fkey FOREIGN KEY (order_id) REFERENCES public."order"(id) ON DELETE CASCADE')
    op.execute("ALTER TABLE ONLY public.order_labels ADD CONSTRAINT lab_order_labels_label_id_fkey FOREIGN KEY (label_id) REFERENCES public.label(id) ON DELETE CASCADE")

    op.execute("ALTER TABLE ONLY public.sample_labels ADD CONSTRAINT sample_labels_sample_id_fkey FOREIGN KEY (sample_id) REFERENCES public.sample(id) ON DELETE CASCADE")
    op.execute("ALTER TABLE ONLY public.sample_labels ADD CONSTRAINT sample_labels_label_id_fkey FOREIGN KEY (label_id) REFERENCES public.label(id) ON DELETE CASCADE")

    op.execute("ALTER TABLE ONLY public.assignment ADD CONSTRAINT assignment_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.assignment ADD CONSTRAINT assignment_assignee_user_id_fkey FOREIGN KEY (assignee_user_id) REFERENCES public.app_user(id)")
    op.execute("ALTER TABLE ONLY public.assignment ADD CONSTRAINT assignment_assigned_by_user_id_fkey FOREIGN KEY (assigned_by_user_id) REFERENCES public.app_user(id)")

    op.execute("ALTER TABLE ONLY public.user_invitation ADD CONSTRAINT user_invitation_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.user_invitation ADD CONSTRAINT user_invitation_invited_by_fkey FOREIGN KEY (invited_by) REFERENCES public.app_user(id)")

    op.execute("ALTER TABLE ONLY public.role ADD CONSTRAINT role_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenant(id)")
    op.execute("ALTER TABLE ONLY public.role_permission ADD CONSTRAINT role_permission_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.role(id) ON DELETE CASCADE")
    op.execute("ALTER TABLE ONLY public.role_permission ADD CONSTRAINT role_permission_permission_id_fkey FOREIGN KEY (permission_id) REFERENCES public.permission(id) ON DELETE CASCADE")
    op.execute("ALTER TABLE ONLY public.user_role ADD CONSTRAINT user_role_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id)")
    op.execute("ALTER TABLE ONLY public.user_role ADD CONSTRAINT user_role_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.role(id)")

    # ------------------------------------------------------------------
    # 6. Seed RBAC permissions and roles
    # ------------------------------------------------------------------
    from sqlalchemy.dialects import postgresql

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
    raise NotImplementedError(
        "v1.0.0 is the baseline migration. Downgrade by dropping and recreating the database."
    )
