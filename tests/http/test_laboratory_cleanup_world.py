"""The synthetic world itself must be rich enough to make the cleanup tests meaningful."""
from sqlalchemy import text

from .cleanup_world import build_world


def test_world_covers_the_dependency_graph(session, client, monkeypatch, stub_pdf_render):
    world = build_world(session, client, monkeypatch, stub_pdf_render)
    t = str(world.target.tenant.id)
    counts = {}
    for table in ("order", "sample", "sample_image", "sample_image_rendition", "order_comment", "order_comment_mention",
                  "order_event", "order_labels", "sample_labels", "order_requesting_physician", "report", "report_version",
                  "report_amendment", "report_review", "assignment", "notification", "notification_recipient",
                  "notification_delivery", "invoice", "invoice_item", "payment", "audit_log", "idempotency_receipt",
                  "numbering_allocation", "numbering_counter", "storage_object"):
        where = "tenant_id = :t" if table not in ("sample_image_rendition", "order_comment_mention", "order_labels",
                                                  "sample_labels", "report_version", "notification_recipient") else "true"
        counts[table] = session.execute(text(f'SELECT count(*) FROM "{table}" WHERE {where}'), {"t": t}).scalar()
    assert counts["report_amendment"] == 1
    assert counts["report_version"] >= 4
    assert all(v > 0 for k, v in counts.items()), counts
