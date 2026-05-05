"""Tests for admin user onboarding and pre-registration."""

from __future__ import annotations

from typing import Literal

import pytest
from fastapi import Request
from neo4j.exceptions import ClientError
from src.auth.models import AuthUser, Role
from src.graph.queries.users import UPSERT_USER_ON_LOGIN
from src.routes import users


def _request() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/users/bulk",
        "headers": [],
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "scheme": "http",
    }
    return Request(scope)


def _admin() -> AuthUser:
    return AuthUser(
        email="admin@example.com",
        google_sub="admin-sub",
        role="admin",
        entity_key=None,
    )


def test_auth_user_allows_missing_google_sub_for_pre_registered_user() -> None:
    user = AuthUser(email="new@example.com", google_sub=None, role="employee", entity_key="ada")

    assert user.google_sub is None
    assert user.role == "employee"
    assert user.entity_key == "ada"


def test_user_response_allows_missing_google_sub_for_pre_registered_user() -> None:
    response = users.UserResponse(
        email="new@example.com",
        google_sub=None,
        role="employee",
        entity_key="ada",
    )

    assert response.google_sub is None


@pytest.mark.parametrize(
    ("role", "entity_key"),
    [
        ("admin", "ada"),
        ("first_time", "ada"),
    ],
)
def test_normalize_user_assignment_clears_entity_for_non_employee(
    role: Literal["admin", "first_time"], entity_key: str
) -> None:
    normalized_role, normalized_entity = users._normalise_assignment(role, entity_key)

    assert normalized_role == role
    assert normalized_entity is None


def test_normalize_user_assignment_keeps_employee_entity() -> None:
    normalized_role, normalized_entity = users._normalise_assignment("employee", " ada ")

    assert normalized_role == "employee"
    assert normalized_entity == "ada"


def test_normalize_user_assignment_rejects_employee_without_entity() -> None:
    with pytest.raises(users.UserAssignmentError) as exc:
        users._normalise_assignment("employee", "")

    assert exc.value.code == "invalid_request"
    assert exc.value.message == "An employee must be assigned an entity_key."


def test_login_upsert_query_preserves_existing_role_and_entity() -> None:
    on_match_clause = UPSERT_USER_ON_LOGIN.split("ON MATCH SET", maxsplit=1)[1]

    assert "u.role = coalesce(u.role" in on_match_clause
    assert "u.entity_key = u.entity_key" in on_match_clause
    assert "u.role = CASE WHEN $bootstrap_admin" not in on_match_clause


@pytest.mark.asyncio
async def test_bulk_pre_register_users_returns_existing_user_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.auth import store

    async def fake_existing_user_emails(emails: list[str]) -> set[str]:
        assert emails == ["existing@example.com"]
        return {"existing@example.com"}

    monkeypatch.setattr(store, "existing_user_emails", fake_existing_user_emails)

    result = await store.bulk_pre_register_users(
        [
            store.PreRegisterUserInput(
                email="Existing@Example.com",
                role="admin",
                entity_key=None,
            )
        ]
    )

    assert result == [
        store.PreRegisterUserResult(
            email="existing@example.com",
            status="error",
            code="user_exists",
            message="User already exists.",
            user=None,
        )
    ]


@pytest.mark.asyncio
async def test_bulk_pre_register_users_rejects_duplicate_batch_emails() -> None:
    from src.auth import store

    result = await store.bulk_pre_register_users(
        [
            store.PreRegisterUserInput(
                email="New@Example.com",
                role="admin",
                entity_key=None,
            ),
            store.PreRegisterUserInput(
                email=" new@example.com ",
                role="employee",
                entity_key="ada",
            ),
        ]
    )

    assert result == [
        store.PreRegisterUserResult(
            email="new@example.com",
            status="error",
            code="duplicate_email",
            message="Email appears more than once in this request.",
            user=None,
        ),
        store.PreRegisterUserResult(
            email="new@example.com",
            status="error",
            code="duplicate_email",
            message="Email appears more than once in this request.",
            user=None,
        ),
    ]


@pytest.mark.asyncio
async def test_create_pre_registered_user_normalizes_email(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.auth import store

    captured_email: str | None = None

    class FakeResult:
        async def single(self) -> dict[str, object]:
            return {
                "user": {
                    "email": captured_email,
                    "google_sub": None,
                    "role": "admin",
                    "entity_key": None,
                    "display_name": None,
                }
            }

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def run(self, query: str, **kwargs: object) -> FakeResult:
            nonlocal captured_email
            captured_email = str(kwargs["email"])
            return FakeResult()

    def fake_get_session(*, write: bool = False) -> FakeSession:
        assert write is True
        return FakeSession()

    monkeypatch.setattr(store, "get_session", fake_get_session)

    user = await store.create_pre_registered_user(
        store.PreRegisterUserInput(
            email=" New@Example.com ",
            role="admin",
            entity_key=None,
        )
    )

    assert captured_email == "new@example.com"
    assert user.email == "new@example.com"


@pytest.mark.asyncio
async def test_patch_user_rejects_empty_update() -> None:
    with pytest.raises(Exception) as exc:
        await users.patch_user(
            "employee@example.com",
            users.UserUpdateRequest(),
            _request(),
            _admin(),
        )

    assert "No user fields were provided to update." in str(exc.value)


@pytest.mark.asyncio
async def test_patch_user_preserves_employee_role_for_entity_only_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updated_args: tuple[str, object, object] | None = None

    async def fake_get_user_by_email(email: str) -> AuthUser | None:
        assert email == "employee@example.com"
        return AuthUser(
            email=email,
            google_sub="sub",
            role="employee",
            entity_key="old",
        )

    async def fake_entity_exists(entity_key: str) -> bool:
        assert entity_key == "new"
        return True

    async def fake_update_user(
        email: str,
        new_role: Role | None,
        entity_key: str | None,
    ) -> AuthUser | None:
        nonlocal updated_args
        updated_args = (email, new_role, entity_key)
        return AuthUser(
            email=email,
            google_sub="sub",
            role="employee",
            entity_key=entity_key,
        )

    monkeypatch.setattr(users, "get_user_by_email", fake_get_user_by_email)
    monkeypatch.setattr(users, "entity_exists", fake_entity_exists)
    monkeypatch.setattr(users, "update_user", fake_update_user)

    response = await users.patch_user(
        "employee@example.com",
        users.UserUpdateRequest(entity_key="new"),
        _request(),
        _admin(),
    )

    assert updated_args == ("employee@example.com", "employee", "new")
    assert response.data.entity_key == "new"


@pytest.mark.asyncio
async def test_patch_user_rejects_employee_entity_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_user_by_email(email: str) -> AuthUser | None:
        return AuthUser(
            email=email,
            google_sub="sub",
            role="employee",
            entity_key="old",
        )

    monkeypatch.setattr(users, "get_user_by_email", fake_get_user_by_email)

    with pytest.raises(Exception) as exc:
        await users.patch_user(
            "employee@example.com",
            users.UserUpdateRequest(entity_key=None),
            _request(),
            _admin(),
        )

    assert "An employee must be assigned an entity_key." in str(exc.value)


@pytest.mark.asyncio
async def test_patch_user_rejects_entity_only_update_for_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_user_by_email(email: str) -> AuthUser | None:
        return AuthUser(email=email, google_sub="sub", role="admin")

    monkeypatch.setattr(users, "get_user_by_email", fake_get_user_by_email)

    with pytest.raises(Exception) as exc:
        await users.patch_user(
            "admin-user@example.com",
            users.UserUpdateRequest(entity_key="ada"),
            _request(),
            _admin(),
        )

    assert "Only employees can be assigned an entity_key." in str(exc.value)


@pytest.mark.asyncio
async def test_patch_user_role_change_to_admin_clears_entity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updated_args: tuple[str, object, object] | None = None

    async def fake_update_user(
        email: str,
        new_role: Role | None,
        entity_key: str | None,
    ) -> AuthUser | None:
        nonlocal updated_args
        updated_args = (email, new_role, entity_key)
        return AuthUser(email=email, google_sub="sub", role="admin")

    monkeypatch.setattr(users, "update_user", fake_update_user)

    response = await users.patch_user(
        "employee@example.com",
        users.UserUpdateRequest(role="admin", entity_key="old"),
        _request(),
        _admin(),
    )

    assert updated_args == ("employee@example.com", "admin", None)
    assert response.data.role == "admin"


@pytest.mark.asyncio
async def test_bulk_create_users_returns_duplicate_email_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_entity_exists(entity_key: str) -> bool:
        raise AssertionError(f"entity_exists should not be called for {entity_key}")

    async def fail_bulk_pre_register_users(rows: object) -> object:
        raise AssertionError(f"bulk_pre_register_users should not be called for {rows}")

    monkeypatch.setattr(users, "entity_exists", fail_entity_exists)
    monkeypatch.setattr(users, "bulk_pre_register_users", fail_bulk_pre_register_users)

    response = await users.bulk_create_users(
        users.UserBulkCreateRequest(
            users=[
                users.UserBulkCreateRow(
                    email="Duplicate@Example.com",
                    role="admin",
                    entity_key=None,
                ),
                users.UserBulkCreateRow(
                    email=" duplicate@example.com ",
                    role="employee",
                    entity_key="ada",
                ),
            ]
        ),
        _request(),
        _admin(),
    )

    assert [result.model_dump() for result in response.data.results] == [
        {
            "email": "duplicate@example.com",
            "status": "error",
            "code": "duplicate_email",
            "message": "Email appears more than once in this request.",
            "user": None,
        },
        {
            "email": "duplicate@example.com",
            "status": "error",
            "code": "duplicate_email",
            "message": "Email appears more than once in this request.",
            "user": None,
        },
    ]


@pytest.mark.asyncio
async def test_bulk_create_users_creates_valid_employee_and_admin_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.auth import store

    captured_rows: list[store.PreRegisterUserInput] = []

    async def fake_entity_exists(entity_key: str) -> bool:
        assert entity_key == "ada"
        return True

    async def fake_bulk_pre_register_users(
        rows: list[store.PreRegisterUserInput],
    ) -> list[store.PreRegisterUserResult]:
        nonlocal captured_rows
        captured_rows = rows
        return [
            store.PreRegisterUserResult(
                email="employee@example.com",
                status="created",
                code=None,
                message=None,
                user=AuthUser(
                    email="employee@example.com",
                    google_sub=None,
                    role="employee",
                    entity_key="ada",
                ),
            ),
            store.PreRegisterUserResult(
                email="admin@example.com",
                status="created",
                code=None,
                message=None,
                user=AuthUser(
                    email="admin@example.com",
                    google_sub=None,
                    role="admin",
                    entity_key=None,
                ),
            ),
        ]

    monkeypatch.setattr(users, "entity_exists", fake_entity_exists)
    monkeypatch.setattr(users, "bulk_pre_register_users", fake_bulk_pre_register_users)

    response = await users.bulk_create_users(
        users.UserBulkCreateRequest(
            users=[
                users.UserBulkCreateRow(
                    email=" Employee@Example.com ",
                    role="employee",
                    entity_key=" ada ",
                ),
                users.UserBulkCreateRow(
                    email="Admin@Example.com",
                    role="admin",
                    entity_key="ada",
                ),
            ]
        ),
        _request(),
        _admin(),
    )

    assert captured_rows == [
        store.PreRegisterUserInput(
            email="employee@example.com",
            role="employee",
            entity_key="ada",
        ),
        store.PreRegisterUserInput(
            email="admin@example.com",
            role="admin",
            entity_key=None,
        ),
    ]
    assert [result.model_dump() for result in response.data.results] == [
        {
            "email": "employee@example.com",
            "status": "created",
            "code": None,
            "message": None,
            "user": {
                "email": "employee@example.com",
                "google_sub": None,
                "role": "employee",
                "entity_key": "ada",
                "display_name": None,
            },
        },
        {
            "email": "admin@example.com",
            "status": "created",
            "code": None,
            "message": None,
            "user": {
                "email": "admin@example.com",
                "google_sub": None,
                "role": "admin",
                "entity_key": None,
                "display_name": None,
            },
        },
    ]


@pytest.mark.asyncio
async def test_bulk_create_users_returns_row_error_for_invalid_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.auth import store

    async def fake_bulk_pre_register_users(
        rows: list[store.PreRegisterUserInput],
    ) -> list[store.PreRegisterUserResult]:
        assert rows == [
            store.PreRegisterUserInput(
                email="valid@example.com",
                role="admin",
                entity_key=None,
            )
        ]
        return [
            store.PreRegisterUserResult(
                email="valid@example.com",
                status="created",
                code=None,
                message=None,
                user=AuthUser(email="valid@example.com", google_sub=None, role="admin"),
            )
        ]

    monkeypatch.setattr(users, "bulk_pre_register_users", fake_bulk_pre_register_users)

    response = await users.bulk_create_users(
        users.UserBulkCreateRequest(
            users=[
                users.UserBulkCreateRow(email="bad@example.com", role="owner"),
                users.UserBulkCreateRow(email="valid@example.com", role="admin"),
            ]
        ),
        _request(),
        _admin(),
    )

    assert response.data.results[0].code == "invalid_role"
    assert response.data.results[1].status == "created"


@pytest.mark.parametrize(
    "email",
    ["@", "user@", "@example.com", "user@@example.com", "user example@example.com"],
)
@pytest.mark.asyncio
async def test_bulk_create_users_returns_row_error_for_invalid_email_shape(
    email: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_bulk_pre_register_users(rows: object) -> object:
        raise AssertionError(f"bulk_pre_register_users should not be called for {rows}")

    monkeypatch.setattr(users, "bulk_pre_register_users", fail_bulk_pre_register_users)

    response = await users.bulk_create_users(
        users.UserBulkCreateRequest(
            users=[users.UserBulkCreateRow(email=email, role="admin")]
        ),
        _request(),
        _admin(),
    )

    assert response.data.results[0].code == "invalid_email"


@pytest.mark.asyncio
async def test_create_pre_registered_user_maps_neo4j_email_constraint_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.auth import store

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def run(self, query: str, **kwargs: object) -> object:
            error = ClientError()
            error._neo4j_code = "Neo.ClientError.Schema.ConstraintValidationFailed"
            error._message = "Node already exists with label User and property email"
            raise error

    def fake_get_session(*, write: bool = False) -> FakeSession:
        assert write is True
        return FakeSession()

    monkeypatch.setattr(store, "get_session", fake_get_session)

    with pytest.raises(store.UserAlreadyExistsError) as exc:
        await store.create_pre_registered_user(
            store.PreRegisterUserInput(
                email="race@example.com",
                role="admin",
                entity_key=None,
            )
        )

    assert exc.value.email == "race@example.com"


@pytest.mark.asyncio
async def test_bulk_pre_register_users_returns_existing_error_on_create_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.auth import store

    async def fake_existing_user_emails(emails: list[str]) -> set[str]:
        assert emails == ["race@example.com"]
        return set()

    async def fake_create_pre_registered_user(row: store.PreRegisterUserInput) -> AuthUser:
        raise store.UserAlreadyExistsError(row.email)

    monkeypatch.setattr(store, "existing_user_emails", fake_existing_user_emails)
    monkeypatch.setattr(store, "create_pre_registered_user", fake_create_pre_registered_user)

    result = await store.bulk_pre_register_users(
        [store.PreRegisterUserInput(email="race@example.com", role="admin", entity_key=None)]
    )

    assert result == [
        store.PreRegisterUserResult(
            email="race@example.com",
            status="error",
            code="user_exists",
            message="User already exists.",
            user=None,
        )
    ]
