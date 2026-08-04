"""Model tests for `ReportTemplateVersion` (Céluma 1.3, Phase 2, Block B, Story B2)."""
import uuid

from app.models.report_template_version import ReportTemplateVersion, ReportTemplateVersionStatus


def _configuration() -> dict:
    return {
        "schema_version": 2,
        "template": {"base": {}, "sections": {}},
        "presentation": {
            "paper": {
                "size": "LETTER",
                "orientation": "PORTRAIT",
                "margins_cm": {"top": 2.0, "right": 2.0, "bottom": 2.0, "left": 2.0},
            },
            "header": {"enabled": True},
            "footer": {"enabled": True},
        },
    }


class TestReportTemplateVersionModel:
    def test_creation_defaults(self):
        tenant_id = uuid.uuid4()
        template_id = uuid.uuid4()
        version = ReportTemplateVersion(
            tenant_id=tenant_id,
            report_template_id=template_id,
            version_number=1,
            configuration=_configuration(),
        )
        assert version.id is not None
        assert version.tenant_id == tenant_id
        assert version.report_template_id == template_id
        assert version.version_number == 1
        assert version.schema_version == 2
        assert version.status == ReportTemplateVersionStatus.PUBLISHED
        assert version.activated_at is None
        assert version.archived_at is None

    def test_explicit_status_can_be_set(self):
        version = ReportTemplateVersion(
            tenant_id=uuid.uuid4(),
            report_template_id=uuid.uuid4(),
            version_number=2,
            configuration=_configuration(),
            status=ReportTemplateVersionStatus.ACTIVE,
        )
        assert version.status == ReportTemplateVersionStatus.ACTIVE

    def test_status_enum_values_match_contract(self):
        assert {s.value for s in ReportTemplateVersionStatus} == {
            "PUBLISHED",
            "ACTIVE",
            "ARCHIVED",
        }

    def test_configuration_round_trips_as_dict(self):
        config = _configuration()
        version = ReportTemplateVersion(
            tenant_id=uuid.uuid4(),
            report_template_id=uuid.uuid4(),
            version_number=1,
            configuration=config,
        )
        assert version.configuration["presentation"]["paper"]["size"] == "LETTER"
