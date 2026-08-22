"""Production refuses to start when static AWS credentials are configured.

Céluma 1.3, Phase 5, Block G-B — **F-018**.

`boto3` resolves credentials in a fixed precedence order, and explicit
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` win over the container
credential provider. Setting either one in production therefore makes
`SesEmailProvider` and `S3Service` stop using the ECS task role — the
least-privilege, auto-rotating identity the infrastructure grants is quietly
replaced by a long-lived key whose scope nobody reviewed.

The failure is silent by construction: nothing logs "you are no longer using
the task role". Block F found it the hard way, when a mounted `.env` made
every SES send return `provider_access_denied` — an error that reads like a
missing SES permission rather than a credential-precedence problem.

The production task definition sets neither variable today, so this is a guard
against regression rather than a live defect. It is enforced at `Settings`
construction, before any AWS client exists.

Two behaviours below are the ones worth protecting from a future "cleanup":

* **Empty strings are absence, not configuration.** `docker-compose.yml`
  forwards `AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}` unconditionally, so an
  unexported variable arrives as `""`. Blanking the values is exactly how
  Block F restored SES, and that must remain a way to run.
* **Only production is guarded.** Local development and the test suites
  legitimately authenticate with static keys against MinIO or a developer's
  own account, so `dev`, `test` and `stg` are untouched.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings

ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def build_settings(**overrides) -> Settings:
    """A `Settings` built from explicit values only.

    `_env_file=None` keeps a developer's `.env` out of the assertions, and the
    two credentials are pinned to `None` because `BaseSettings` still reads the
    real process environment. The dev container legitimately exports
    `AWS_ACCESS_KEY_ID` (docker-compose forwards it for MinIO), so without
    these explicit defaults the machine running the suite would decide the
    result — which is exactly the failure mode this module is meant to detect.
    """
    values = {
        "database_url": "postgresql+psycopg2://u:p@localhost:5432/celuma",
        "jwt_secret": "irrelevant-in-tests",
        "aws_access_key_id": None,
        "aws_secret_access_key": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# --------------------------------------------------------------------------
# Production: refused
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"aws_access_key_id": ACCESS_KEY}, ["AWS_ACCESS_KEY_ID"]),
        ({"aws_secret_access_key": SECRET_KEY}, ["AWS_SECRET_ACCESS_KEY"]),
        (
            {"aws_access_key_id": ACCESS_KEY, "aws_secret_access_key": SECRET_KEY},
            ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
        ),
    ],
    ids=["access-key-only", "secret-key-only", "both"],
)
def test_prod_refuses_explicit_aws_credentials(overrides, expected):
    with pytest.raises(ValueError) as exc:
        build_settings(env="prod", **overrides)

    message = str(exc.value)
    for name in expected:
        assert name in message
    assert "ENV=prod" in message


def test_prod_starts_when_no_explicit_credentials_are_set():
    settings = build_settings(env="prod")

    assert settings.env == "prod"
    assert settings.aws_access_key_id is None
    assert settings.aws_secret_access_key is None


def test_prod_treats_empty_strings_as_unset():
    # docker-compose forwards the variables unconditionally; an unexported
    # value arrives as "". That is absence, and must remain startable.
    settings = build_settings(
        env="prod", aws_access_key_id="", aws_secret_access_key="   "
    )

    assert settings.env == "prod"


def test_the_error_never_echoes_a_credential_value():
    with pytest.raises(ValueError) as exc:
        build_settings(
            env="prod", aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY
        )

    message = str(exc.value)
    assert ACCESS_KEY not in message
    assert SECRET_KEY not in message
    # not even a prefix — a partial key is still a leaked identifier
    assert ACCESS_KEY[:6] not in message
    assert SECRET_KEY[:6] not in message


@pytest.mark.parametrize("env_value", ["PROD", "prod", " Prod "])
def test_the_guard_is_case_and_whitespace_insensitive(env_value):
    # ENV arrives from a task definition; casing must not be a bypass.
    with pytest.raises(ValueError):
        build_settings(env=env_value, aws_access_key_id=ACCESS_KEY)


# --------------------------------------------------------------------------
# Every other environment: allowed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("env_value", ["dev", "test", "stg"])
def test_non_production_environments_may_use_explicit_credentials(env_value):
    settings = build_settings(
        env=env_value, aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY
    )

    assert settings.aws_access_key_id == ACCESS_KEY
    assert settings.aws_secret_access_key == SECRET_KEY


def test_the_default_environment_is_not_guarded():
    # `env` defaults to "dev"; local development must not require the change.
    settings = build_settings(aws_access_key_id=ACCESS_KEY)

    assert settings.env == "dev"
    assert settings.aws_access_key_id == ACCESS_KEY
