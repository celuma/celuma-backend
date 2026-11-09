"""Usage: python -m scripts.backfill_report_section_titles --tenant-id UUID [--apply --actor-id UUID]."""

import argparse
from collections import Counter
import json
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session, select

import app.models  # noqa: F401  registers relationships before opening a standalone session
from app.core.db import engine
from app.core.rbac import has_permission
from app.models.report import Report, ReportTemplate
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.services.report_section_titles_backfill import NeedsReview, update_report, update_template
from app.services.s3 import S3Service


def _read_only(session: Session) -> None:
    session.execute(text("SET TRANSACTION READ ONLY"))


def _ids(model, tenant_id: UUID):
    cursor = None
    while True:
        with Session(engine) as session:
            _read_only(session)
            query = select(model.id).where(model.tenant_id == tenant_id).order_by(model.id).limit(200)
            if cursor is not None:
                query = query.where(model.id > cursor)
            batch = session.exec(query).all()
        if not batch:
            return
        yield from batch
        cursor = batch[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--apply", action="store_true", help="Write; default is a read-only inventory.")
    parser.add_argument("--actor-id", type=UUID, help="Active same-tenant user with reports:manage_templates.")
    parser.add_argument("--report-id", type=UUID, help="Inspect or apply to one report only, skipping templates.")
    args = parser.parse_args()
    if args.apply != (args.actor_id is not None):
        parser.error("--apply and --actor-id must be supplied together")
    if engine.dialect.name != "postgresql":
        parser.error("PostgreSQL is required")

    with Session(engine) as session:
        _read_only(session)
        if session.get(Tenant, args.tenant_id) is None:
            parser.error("Unknown laboratory")
        if args.apply:
            actor = session.get(AppUser, args.actor_id)
            if (
                actor is None or actor.tenant_id != args.tenant_id or not actor.is_active
                or not has_permission(actor.id, "reports:manage_templates", session)
            ):
                parser.error("The actor must be authorized in the selected laboratory")

    s3 = S3Service()
    counts = Counter()
    try:
        for template_id in (() if args.report_id else _ids(ReportTemplate, args.tenant_id)):
            try:
                with Session(engine) as session, session.begin():
                    if not args.apply:
                        _read_only(session)
                    query = select(ReportTemplate).where(
                        ReportTemplate.id == template_id, ReportTemplate.tenant_id == args.tenant_id,
                    )
                    template = session.exec(query.with_for_update() if args.apply else query).first()
                    if template is None:
                        continue
                    changed = update_template(session, template, args.actor_id)
                counts["templates_changed" if changed else "templates_unchanged"] += 1
            except NeedsReview as exc:
                counts[f"review:{exc}"] += 1

        report_ids = (args.report_id,) if args.report_id else _ids(Report, args.tenant_id)
        for report_id in report_ids:
            try:
                with Session(engine) as session, session.begin():
                    if not args.apply:
                        _read_only(session)
                    outcome, versions = update_report(session, s3, args.tenant_id, report_id, args.actor_id)
                counts[f"reports_{outcome}"] += 1
                counts["versions_changed"] += versions
            except NeedsReview as exc:
                counts[f"review:{exc}"] += 1
    except Exception as exc:
        # Service errors can contain S3 request details or clinical payloads.
        # Leave earlier committed reports intact and return a resumable failure.
        print(json.dumps({"mode": "apply" if args.apply else "dry-run", "counts": counts, "error": type(exc).__name__}))
        return 1

    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "counts": counts}, sort_keys=True))
    return 2 if counts["reports_busy"] or any(key.startswith("review:") for key in counts) else 0


if __name__ == "__main__":
    raise SystemExit(main())
