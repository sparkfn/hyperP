from __future__ import annotations

from dataclasses import dataclass, field

from fastapi.testclient import TestClient
from src.app import build_app
from src.auth.deps import require_active_user, require_admin
from src.auth.models import AuthUser
from src.repositories.deps import get_merge_repo
from src.repositories.protocols.merge import GoldenProfileSelection, MergeOutcome


@dataclass
class _MergeRepo:
    merge_outcome: MergeOutcome = field(
        default_factory=lambda: MergeOutcome(merge_event_id="merge-1")
    )
    unmerge_result: tuple[str, str] | None = ("person-a", "person-b")
    create_lock_result: tuple[str, str | None] = ("ok", "lock-1")
    delete_lock_result: bool = True
    manual_merge_calls: list[tuple[str, str, str, str, list[GoldenProfileSelection]]] | None = None
    unmerge_calls: list[tuple[str, str, str]] | None = None
    create_lock_calls: list[tuple[str, str, str, str, str | None, str]] | None = None
    delete_lock_calls: list[str] | None = None

    async def manual_merge(
        self,
        from_id: str,
        to_id: str,
        reason: str,
        actor_id: str,
        golden_profile_selections: list[GoldenProfileSelection],
    ) -> MergeOutcome:
        if self.manual_merge_calls is None:
            self.manual_merge_calls = []
        self.manual_merge_calls.append(
            (from_id, to_id, reason, actor_id, golden_profile_selections)
        )
        return self.merge_outcome

    async def unmerge(
        self, merge_event_id: str, reason: str, actor_id: str
    ) -> tuple[str, str] | None:
        if self.unmerge_calls is None:
            self.unmerge_calls = []
        self.unmerge_calls.append((merge_event_id, reason, actor_id))
        return self.unmerge_result

    async def create_lock(
        self,
        left: str,
        right: str,
        lock_type: str,
        reason: str,
        expires_at: str | None,
        actor_id: str,
    ) -> tuple[str, str | None]:
        if self.create_lock_calls is None:
            self.create_lock_calls = []
        self.create_lock_calls.append(
            (left, right, lock_type, reason, expires_at, actor_id)
        )
        return self.create_lock_result

    async def delete_lock(self, lock_id: str) -> bool:
        if self.delete_lock_calls is None:
            self.delete_lock_calls = []
        self.delete_lock_calls.append(lock_id)
        return self.delete_lock_result


async def _admin_user() -> AuthUser:
    return AuthUser(
        email="admin@example.com",
        google_sub="admin-sub",
        role="admin",
    )


def _client(repo: _MergeRepo) -> TestClient:
    app = build_app()
    app.dependency_overrides[require_active_user] = _admin_user
    app.dependency_overrides[require_admin] = _admin_user
    app.dependency_overrides[get_merge_repo] = lambda: repo
    return TestClient(app)


def test_manual_merge_endpoint_returns_enveloped_success() -> None:
    repo = _MergeRepo()
    client = _client(repo)

    res = client.post(
        "/v1/persons/manual-merge",
        json={
            "from_person_id": "person-a",
            "to_person_id": "person-b",
            "reason": "same customer",
        },
    )

    assert res.status_code == 200
    body = res.json()
    assert body["data"] == {
        "merge_event_id": "merge-1",
        "from_person_id": "person-a",
        "to_person_id": "person-b",
        "status": "completed",
    }
    assert repo.manual_merge_calls == [
        ("person-a", "person-b", "same customer", "admin@example.com", [])
    ]


def test_manual_merge_endpoint_passes_golden_profile_selections() -> None:
    repo = _MergeRepo()
    client = _client(repo)

    res = client.post(
        "/v1/persons/manual-merge",
        json={
            "from_person_id": "person-a",
            "to_person_id": "person-b",
            "reason": "same customer",
            "golden_profile_selections": [
                {
                    "field_name": "preferred_email",
                    "source_kind": "identifier",
                    "selected_value": "customer@example.com",
                    "source_record_pk": "sr-1",
                    "identifier_type": "email",
                },
                {
                    "field_name": "preferred_full_name",
                    "source_kind": "source_record_fact",
                    "selected_value": "Jane Customer",
                    "source_record_pk": "sr-2",
                    "identifier_type": None,
                },
            ],
        },
    )

    assert res.status_code == 200
    assert repo.manual_merge_calls == [
        (
            "person-a",
            "person-b",
            "same customer",
            "admin@example.com",
            [
                {
                    "field_name": "preferred_email",
                    "source_kind": "identifier",
                    "selected_value": "customer@example.com",
                    "source_record_pk": "sr-1",
                    "identifier_type": "email",
                },
                {
                    "field_name": "preferred_full_name",
                    "source_kind": "source_record_fact",
                    "selected_value": "Jane Customer",
                    "source_record_pk": "sr-2",
                    "identifier_type": None,
                },
            ],
        )
    ]

    repo = _MergeRepo()
    client = _client(repo)

    res = client.post(
        "/v1/persons/manual-merge",
        json={
            "from_person_id": "person-a",
            "to_person_id": "person-a",
            "reason": "same customer",
        },
    )

    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_person_pair"
    assert repo.manual_merge_calls is None

    repo = _MergeRepo(merge_outcome=MergeOutcome(blocked=True))
    client = _client(repo)

    res = client.post(
        "/v1/persons/manual-merge",
        json={
            "from_person_id": "person-a",
            "to_person_id": "person-b",
            "reason": "same customer",
        },
    )

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "merge_blocked"


def test_unmerge_endpoint_returns_conservative_unmerge_result() -> None:
    repo = _MergeRepo()
    client = _client(repo)

    res = client.post(
        "/v1/persons/unmerge",
        json={"merge_event_id": "merge-1", "reason": "false merge"},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["data"] == {
        "merge_event_id": "merge-1",
        "absorbed_person_id": "person-a",
        "survivor_person_id": "person-b",
        "status": "unmerged",
    }
    assert repo.unmerge_calls == [("merge-1", "false merge", "admin@example.com")]


def test_create_person_pair_lock_endpoint_orders_pair_before_repo_call() -> None:
    repo = _MergeRepo()
    client = _client(repo)

    res = client.post(
        "/v1/locks/person-pair",
        json={
            "left_person_id": "person-b",
            "right_person_id": "person-a",
            "lock_type": "manual_no_match",
            "reason": "not same person",
        },
    )

    assert res.status_code == 201
    assert res.json()["data"] == {
        "lock_id": "lock-1",
        "left_person_id": "person-a",
        "right_person_id": "person-b",
        "lock_type": "manual_no_match",
    }
    assert repo.create_lock_calls == [
        (
            "person-a",
            "person-b",
            "manual_no_match",
            "not same person",
            None,
            "admin@example.com",
        )
    ]


def test_create_person_pair_lock_endpoint_rejects_same_person_request() -> None:
    repo = _MergeRepo()
    client = _client(repo)

    res = client.post(
        "/v1/locks/person-pair",
        json={
            "left_person_id": "person-a",
            "right_person_id": "person-a",
            "lock_type": "manual_no_match",
            "reason": "not same person",
        },
    )

    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_person_pair"
    assert repo.create_lock_calls is None

    repo = _MergeRepo()
    client = _client(repo)

    res = client.delete("/v1/locks/lock-1")

    assert res.status_code == 200
    assert res.json()["data"] == {"lock_id": "lock-1", "status": "deleted"}
    assert repo.delete_lock_calls == ["lock-1"]
