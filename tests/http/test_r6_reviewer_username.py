"""Céluma 1.3.1 manual-validation remediation — R6, reviewer username display.

**The finding.** In the reviewer-assignment picker the `@handle` under each
person's display name was derived from their NAME rather than their username:

    Dr. María López
    @María López          instead of      @mlopez

**Root cause.** `GET /api/v1/users/reviewers` never exposed `username`, so the
frontend's `getReviewerUsers()` hardcoded `username: null` and the picker fell
through to the app's existing fallback, `email.split("@")[0]`. For a reviewer
whose address is built from their own name — `laishamelina@gmail.com`, the
shape the live data actually has — that fallback renders as the name, which is
what manual validation saw.

**Why the existing suite allowed it through.** Nothing asserted the shape of
this response beyond the fields it already had, and the test factory derived
`username` from the email, so a username and an email local part were
indistinguishable in every fixture. That equality is why a missing field
could not fail a test. `create_user` now takes `username` explicitly, and
these tests deliberately make the two differ.

**Scope.** A read-only projection of `AppUser.username`. No persistence
change, no new fallback, and the email is NOT repurposed as a username — the
nullable case keeps the frontend convention `UserPickerDropdown` already
applies to `getLabUsers()`.
"""
import pytest
from sqlmodel import Session

from .factories import (
    auth_headers,
    create_branch,
    create_tenant,
    create_user,
)

REVIEWERS = "/api/v1/users/reviewers"


@pytest.fixture
def lab(session: Session):
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
    return {"tenant": tenant, "branch": branch, "admin": admin}


def _reviewers(client, admin):
    resp = client.get(REVIEWERS, headers=auth_headers(admin))
    assert resp.status_code == 200, resp.text
    return {r["id"]: r for r in resp.json()["reviewers"]}


class TestTheEndpointExposesUsername:
    def test_the_handle_comes_from_the_username_not_the_name_or_email(
        self, client, session, lab
    ):
        reviewer = create_user(
            session,
            lab["tenant"],
            email="marialopez@t1.example",
            roles=("reviewer",),
            full_name="Dra. María López",
            username="mlopez",
        )

        row = _reviewers(client, lab["admin"])[str(reviewer.id)]

        # The display name stays the human name...
        assert row["full_name"] == "Dra. María López"
        # ...and the handle is the stored username — not the name, and not
        # the email's local part, which here would read as the name.
        assert row["username"] == "mlopez"
        assert row["username"] != row["full_name"]
        assert row["username"] != row["email"].split("@")[0]

    def test_an_accented_multi_word_name_never_leaks_into_the_handle(
        self, client, session, lab
    ):
        """The symptom as reported, asserted directly: a handle must never
        contain a space or the accented characters of a display name."""
        reviewer = create_user(
            session,
            lab["tenant"],
            email="a.villanueva@t1.example",
            roles=("reviewer",),
            full_name="Dra. Arisbeth Villanueva Pérez",
            username="avillanueva",
        )

        row = _reviewers(client, lab["admin"])[str(reviewer.id)]
        assert " " not in row["username"]
        assert "Pérez" not in row["username"]
        assert row["username"] == "avillanueva"

    def test_two_reviewers_with_similar_names_stay_distinguishable(
        self, client, session, lab
    ):
        first = create_user(
            session,
            lab["tenant"],
            email="jgarcia1@t1.example",
            roles=("reviewer",),
            full_name="Dr. Juan García",
            username="jgarcia",
        )
        second = create_user(
            session,
            lab["tenant"],
            email="jgarcia2@t1.example",
            roles=("reviewer",),
            full_name="Dr. Juan García",
            username="jgarcia2",
        )

        rows = _reviewers(client, lab["admin"])
        assert rows[str(first.id)]["full_name"] == rows[str(second.id)]["full_name"]
        assert rows[str(first.id)]["username"] != rows[str(second.id)]["username"]

    def test_a_reviewer_without_a_username_returns_null(
        self, client, session, lab
    ):
        """`app_user.username` is nullable, so the field must be too — the
        frontend keeps its existing fallback for this case rather than the API
        inventing one."""
        reviewer = create_user(
            session,
            lab["tenant"],
            email="nousername@t1.example",
            roles=("reviewer",),
            username=None,
        )

        row = _reviewers(client, lab["admin"])[str(reviewer.id)]
        assert row["username"] is None
        # And the endpoint still answers normally for that user.
        assert row["email"] == "nousername@t1.example"

    def test_the_rest_of_the_payload_is_unchanged(self, client, session, lab):
        """R6 adds a field; it must not have altered one."""
        reviewer = create_user(
            session,
            lab["tenant"],
            email="rev@t1.example",
            roles=("reviewer",),
            username="rev",
        )

        row = _reviewers(client, lab["admin"])[str(reviewer.id)]
        assert set(row) == {
            "id",
            "full_name",
            "email",
            "username",
            "has_signature",
            "avatar_url",
        }
        assert row["has_signature"] is False
        assert row["avatar_url"] is None
