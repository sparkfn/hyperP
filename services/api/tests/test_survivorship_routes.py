from __future__ import annotations

from dataclasses import dataclass, field

from fastapi.testclient import TestClient
from src.auth.deps import require_active_user, require_admin
from src.auth.models import AuthUser
from src.frontend_app import build_frontend_app
from src.repositories.deps import get_survivorship_repo
from src.repositories.protocols.survivorship import (
    BatchOverrideResult,
    FieldOptionRow,
    FieldOptionsData,
)


def _sample_options(person_id: str) -> FieldOptionsData:
    return FieldOptionsData(
        person_id=person_id,
        preferred_full_name="ALICE TAN",
        preferred_dob="1990-04-02T00:00:00Z",
        preferred_phone="+6591234567",
        preferred_email="alice@example.com",
        preferred_nric="S9012345A",
        preferred_address_id="addr-1",
        preferred_address_value="1 Orchard Rd, Singapore",
        overrides={"preferred_nric": {"source_record_pk": "sr-2"}},
        options=[
            FieldOptionRow(
                field_name="preferred_full_name",
                source_kind="source_record_fact",
                identifier_type=None,
                value="ALICE TAN",
                address_id=None,
                source_record_pk="sr-1",
                source_system="fundbox_consumer_backend",
                entity_display_name="Fundbox",
                observed_at="2026-01-01T00:00:00Z",
            ),
            FieldOptionRow(
                field_name="preferred_dob",
                source_kind="source_record_fact",
                identifier_type=None,
                value="1990-04-02T00:00:00Z",
                address_id=None,
                source_record_pk="sr-1",
                source_system="fundbox_consumer_backend",
                entity_display_name="Fundbox",
                observed_at="2026-01-01T00:00:00Z",
            ),
            FieldOptionRow(
                field_name="preferred_nric",
                source_kind="identifier",
                identifier_type="nric",
                value="S9012345A",
                address_id=None,
                source_record_pk="sr-2",
                source_system="eko_phppos",
                entity_display_name="Eko",
                observed_at="2026-02-01T00:00:00Z",
            ),
            FieldOptionRow(
                field_name="preferred_address",
                source_kind="address",
                identifier_type=None,
                value="1 Orchard Rd, Singapore",
                address_id="addr-1",
                source_record_pk="sr-1",
                source_system="fundbox_consumer_backend",
                entity_display_name="Fundbox",
                observed_at="2026-01-01T00:00:00Z",
            ),
        ],
    )


@dataclass
class _SurvRepo:
    options: FieldOptionsData | None
    override_outcome: str = "ok"
    batch_outcome: BatchOverrideResult = field(
        default_factory=lambda: BatchOverrideResult(outcome="ok")
    )
    override_calls: list[tuple[str, str, str, str, str]] | None = None

    async def recompute_golden_profile(self, person_id: str) -> float | None:
        return 0.8

    async def get_field_options(self, person_id: str) -> FieldOptionsData | None:
        return self.options

    async def create_override(
        self,
        person_id: str,
        field_name: str,
        source_record_pk: str,
        reason: str,
        actor_id: str,
    ) -> str:
        if self.override_calls is None:
            self.override_calls = []
        self.override_calls.append((person_id, field_name, source_record_pk, reason, actor_id))
        return self.override_outcome

    async def create_batch_overrides(
        self,
        person_id: str,
        items: list[tuple[str, str]],
        reason: str,
        actor_id: str,
    ) -> BatchOverrideResult:
        return self.batch_outcome


async def _admin_user() -> AuthUser:
    return AuthUser(email="admin@example.com", google_sub="admin-sub", role="admin")


def _client(repo: _SurvRepo) -> TestClient:
    app = build_frontend_app()
    app.dependency_overrides[require_active_user] = _admin_user
    app.dependency_overrides[require_admin] = _admin_user
    app.dependency_overrides[get_survivorship_repo] = lambda: repo
    return TestClient(app)


def test_field_options_maps_all_fields_with_display_and_current_flags() -> None:
    repo = _SurvRepo(options=_sample_options("person-1"))
    client = _client(repo)

    res = client.get("/persons/person-1/field-options")

    assert res.status_code == 200
    fields = {f["field_name"]: f for f in res.json()["data"]["fields"]}
    # All six editable fields are always present, in display order.
    assert list(fields) == [
        "preferred_full_name",
        "preferred_phone",
        "preferred_email",
        "preferred_dob",
        "preferred_nric",
        "preferred_address",
    ]
    # DOB option value is formatted server-side.
    dob = fields["preferred_dob"]
    assert dob["options"][0]["value_display"] == "02 Apr 1990"
    assert dob["options"][0]["is_current"] is True
    # NRIC is flagged overridden; the matching value is current.
    assert fields["preferred_nric"]["is_overridden"] is True
    assert fields["preferred_nric"]["options"][0]["is_current"] is True
    # Address option matches by address_id, not by string value.
    assert fields["preferred_address"]["options"][0]["is_current"] is True
    # Phone has no source options here but the field still appears.
    assert fields["preferred_phone"]["options"] == []


def test_field_options_404_when_person_missing() -> None:
    repo = _SurvRepo(options=None)
    client = _client(repo)
    res = client.get("/persons/ghost/field-options")
    assert res.status_code == 404


def test_create_override_uses_field_name_contract() -> None:
    repo = _SurvRepo(options=_sample_options("person-1"))
    client = _client(repo)

    res = client.post(
        "/persons/person-1/survivorship-overrides",
        json={"field_name": "preferred_nric", "source_record_pk": "sr-2", "reason": "verified"},
    )

    assert res.status_code == 200
    assert res.json()["data"] == {
        "person_id": "person-1",
        "field_name": "preferred_nric",
        "source_record_pk": "sr-2",
        "status": "applied",
    }
    assert repo.override_calls == [
        ("person-1", "preferred_nric", "sr-2", "verified", "admin@example.com")
    ]


def test_create_override_value_not_found_returns_422() -> None:
    repo = _SurvRepo(options=_sample_options("person-1"), override_outcome="value_not_found")
    client = _client(repo)

    res = client.post(
        "/persons/person-1/survivorship-overrides",
        json={"field_name": "preferred_dob", "source_record_pk": "sr-9", "reason": "x"},
    )

    assert res.status_code == 422


def test_create_override_rejects_unknown_field() -> None:
    repo = _SurvRepo(options=_sample_options("person-1"))
    client = _client(repo)

    res = client.post(
        "/persons/person-1/survivorship-overrides",
        json={"field_name": "preferred_unknown", "source_record_pk": "sr-1", "reason": "x"},
    )

    # Rejected by request-body validation (GoldenFieldName literal).
    assert res.status_code == 400
    assert repo.override_calls is None
