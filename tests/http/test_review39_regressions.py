"""Independent coordinator regressions: synthetic/disposable databases only."""
import json
from datetime import datetime,timedelta
from sqlalchemy import text
from sqlmodel import Session
from app.services import laboratory_cleanup as service
from app.services import laboratory_cleanup_scope as scope_mod
from app.services import notification_delivery
from .test_laboratory_cleanup_recovery import world, flow, queue_operation, expire_lease
from .numbering_support import fresh_client


def test_cancelled_operation_cannot_be_resumed_by_stale_executor(session,world,flow):
    op=queue_operation(session,world)
    bind=session.get_bind()
    owner="paused-old-worker"
    stale=service._claim(bind,op.id,owner)
    assert stale is not None
    service._phase_quiesce(bind,stale)
    expire_lease(session,op.id)
    cancelled=flow.cancel(str(op.id))
    assert cancelled.status_code==200 and cancelled.json()["status"]=="CANCELLED"
    before=session.execute(text('SELECT count(*) FROM "order" WHERE tenant_id=:t'),{"t":str(world.target.tenant.id)}).scalar()
    session.rollback()
    service._phase_database(bind,stale,owner)
    after=session.execute(text('SELECT count(*) FROM "order" WHERE tenant_id=:t'),{"t":str(world.target.tenant.id)}).scalar()
    assert after==before, f"cancelled stale worker deleted orders: {before} -> {after}"


def test_asset_referenced_by_other_laboratory_configuration_is_not_deleted(session,world):
    key=next(k for k in sorted(world.target.scoped_keys) if "/processed/" in k)
    value={"sections":[],"presentation":{"header":{"logo_key":key}}}
    session.execute(text("UPDATE report_template SET template_json=CAST(:v AS json) WHERE tenant_id=:t"),
                    {"v":json.dumps(value),"t":str(world.other.tenant.id)})
    session.commit()
    scope=scope_mod.compute_scope(session,world.target.tenant.id,True)
    scheduled={o.object_key for o in scope.objects.physical}
    assert key not in scheduled, "other laboratory preserved template references an object scheduled for physical deletion"


def test_delivery_claim_cannot_cross_cleanup_gate(session,world,monkeypatch):
    bind=session.get_bind()
    tenant=str(world.target.tenant.id)
    real_select=notification_delivery.select_due_delivery_ids
    def selected_then_cleanup(s,**kw):
        ids=real_select(s,**kw)
        assert ids
        with Session(bind) as coordinator:
            op=queue_operation(coordinator,world)
        # This must wait for an already-started claim transaction, or make its commit refuse the target.
        # Current implementation does neither: notifications do not take the shared maintenance lock.
        stale=service._claim(bind,op.id,"cleanup-worker")
        service._phase_quiesce(bind,stale)
        return ids
    monkeypatch.setattr(notification_delivery,"select_due_delivery_ids",selected_then_cleanup)
    with Session(bind) as worker:
        claimed=notification_delivery.claim_pending_deliveries(worker,now=datetime.utcnow()+timedelta(days=1),limit=100)
        mine=[d for d in claimed if str(d.tenant_id)==tenant]
        assert not mine, f"claimed {len(mine)} target deliveries after cleanup barrier completed"
