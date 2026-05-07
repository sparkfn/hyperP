# User Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build admin bulk user pre-registration so admins can create access-ready `:User` records before first Google login.

**Architecture:** Extend the existing Neo4j-backed user store and admin-only `/v1/users` router. Add a bulk creation route plus a BFF route, then extend the existing `/admin/users` client page with an in-page editable bulk editor that surfaces row-level results and explicit update actions for existing users.

**Tech Stack:** FastAPI, Pydantic, Neo4j Cypher, pytest, Next.js App Router, TypeScript, MUI, npm.

---

## File Structure

- Modify `services/api/src/auth/models.py`
  - Make `AuthUser.google_sub` nullable so pre-registered users without first login can be represented.
- Modify `services/api/src/graph/queries/users.py`
  - Preserve assigned role/entity on login for existing users.
  - Add create/list helpers for pre-registration.
  - Return nullable `google_sub` consistently.
- Modify `services/api/src/auth/store.py`
  - Add typed bulk pre-registration store functions and row result models.
  - Normalize emails once at the store boundary.
- Modify `services/api/src/routes/users.py`
  - Add request/response Pydantic models for bulk create.
  - Add `POST /v1/users/bulk` with admin dependency.
  - Make `UserResponse.google_sub` nullable.
- Create `services/api/tests/test_user_onboarding.py`
  - Unit-test route validation/store orchestration and login query semantics with monkeypatched store functions.
- Modify `services/frontend/src/lib/api-types-ops.ts`
  - Make `UserResponse.google_sub` nullable.
  - Add bulk onboarding request/result types.
- Create `services/frontend/src/app/bff/users/bulk/route.ts`
  - Thin `POST` proxy to `/users/bulk`.
- Modify `services/frontend/src/app/admin/users/page.tsx`
  - Add bulk editable onboarding rows and row-level result handling.
  - Keep existing user table behavior.

---

### Task 1: Backend auth/user contracts support pre-registered users

**Files:**
- Modify: `services/api/src/auth/models.py`
- Modify: `services/api/src/routes/users.py`
- Modify: `services/frontend/src/lib/api-types-ops.ts`
- Test: `services/api/tests/test_user_onboarding.py`

- [ ] **Step 1: Create failing tests for nullable Google identity and role/entity validation helpers**

Create `services/api/tests/test_user_onboarding.py` with:

```python
"""Tests for admin user onboarding and pre-registration."""

from __future__ import annotations

from typing import Literal

import pytest
from fastapi import Request

from src.auth.models import AuthUser
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


def test_auth_user_allows_missing_google_sub_for_pre_registered_user() -> None:
    user = AuthUser(email="new@example.com", google_sub=None, role="employee", entity_key="ada")

    assert user.google_sub is None
    assert user.role == "employee"
    assert user.entity_key == "ada"


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


def test_normalize_user_assignment_rejects_employee_without_entity() -> None:
    with pytest.raises(users.UserAssignmentError) as exc:
        users._normalise_assignment("employee", "")

    assert exc.value.code == "invalid_request"
    assert exc.value.message == "An employee must be assigned an entity_key."
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest services/api/tests/test_user_onboarding.py -v
```

Expected: FAIL because `AuthUser.google_sub` does not accept `None`, and `_normalise_assignment` / `UserAssignmentError` do not exist.

- [ ] **Step 3: Make `google_sub` nullable in backend and frontend contracts**

In `services/api/src/auth/models.py`, change `AuthUser` to:

```python
class AuthUser(BaseModel):
    """Represents an authenticated principal resolved from a Google ID token."""

    email: str
    google_sub: str | None
    role: Role
    entity_key: str | None = None
    display_name: str | None = None
```

In `services/api/src/routes/users.py`, change `UserResponse.google_sub` to:

```python
class UserResponse(BaseModel):
    email: str
    google_sub: str | None
    role: Role
    entity_key: str | None = None
    display_name: str | None = None
```

In `services/frontend/src/lib/api-types-ops.ts`, change `UserResponse.google_sub` to:

```typescript
export interface UserResponse {
  email: string;
  google_sub: string | null;
  role: Role;
  entity_key: string | null;
  display_name: string | null;
}
```

- [ ] **Step 4: Extract route-level assignment normalization**

In `services/api/src/routes/users.py`, add this class and helper above route handlers:

```python
class UserAssignmentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _normalise_assignment(role: Role, entity_key: str | None) -> tuple[Role, str | None]:
    target_entity = entity_key.strip() if entity_key else None
    if role == "employee" and not target_entity:
        raise UserAssignmentError("invalid_request", "An employee must be assigned an entity_key.")
    if role in {"admin", "first_time"}:
        return role, None
    return role, target_entity
```

Then replace the manual role/entity block in `patch_user()` with:

```python
    effective_role = body.role
    try:
        effective_role, target_entity = _normalise_assignment(effective_role, body.entity_key)
    except UserAssignmentError as exc:
        raise http_error(400, exc.code, exc.message, request) from exc
```

Keep the existing `entity_exists()` and `update_user()` logic after that block.

- [ ] **Step 5: Run tests to verify Task 1 passes**

Run:

```bash
uv run pytest services/api/tests/test_user_onboarding.py -v
```

Expected: PASS for the three tests in the file.

---

### Task 2: User store supports preserving pre-assigned access and bulk create results

**Files:**
- Modify: `services/api/src/graph/queries/users.py`
- Modify: `services/api/src/auth/store.py`
- Test: `services/api/tests/test_user_onboarding.py`

- [ ] **Step 1: Add failing tests for login preservation and bulk store orchestration**

Append to `services/api/tests/test_user_onboarding.py`:

```python
from src.graph.queries.users import UPSERT_USER_ON_LOGIN


def test_login_upsert_query_preserves_existing_role_and_entity() -> None:
    assert "u.role = coalesce(u.role" in UPSERT_USER_ON_LOGIN
    assert "u.entity_key = u.entity_key" in UPSERT_USER_ON_LOGIN
    assert "u.role = CASE WHEN $bootstrap_admin" not in UPSERT_USER_ON_LOGIN.split("ON MATCH SET", maxsplit=1)[1]


@pytest.mark.asyncio
async def test_bulk_pre_register_users_returns_existing_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.auth import store

    async def fake_existing_user_emails(emails: list[str]) -> set[str]:
        assert emails == ["existing@example.com"]
        return {"existing@example.com"}

    monkeypatch.setattr(store, "existing_user_emails", fake_existing_user_emails)

    result = await store.bulk_pre_register_users(
        [store.PreRegisterUserInput(email="Existing@Example.com", role="admin", entity_key=None)]
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest services/api/tests/test_user_onboarding.py -v
```

Expected: FAIL because the login query does not yet contain preservation assignments and store dataclasses/functions do not exist.

- [ ] **Step 3: Update login upsert query and add bulk create queries**

In `services/api/src/graph/queries/users.py`, replace `UPSERT_USER_ON_LOGIN` with:

```python
UPSERT_USER_ON_LOGIN = """
MERGE (u:User {email: $email})
ON CREATE SET
  u.google_sub = $google_sub,
  u.display_name = $display_name,
  u.role = CASE WHEN $bootstrap_admin THEN 'admin' ELSE 'first_time' END,
  u.entity_key = null,
  u.created_at = datetime(),
  u.last_login_at = datetime()
ON MATCH SET
  u.google_sub = $google_sub,
  u.display_name = coalesce($display_name, u.display_name),
  u.role = coalesce(u.role, CASE WHEN $bootstrap_admin THEN 'admin' ELSE 'first_time' END),
  u.entity_key = u.entity_key,
  u.last_login_at = datetime()
RETURN u {
  .email, .google_sub, .role, .entity_key, .display_name
} AS user
"""
```

Add these constants after `LIST_USERS`:

```python
EXISTING_USER_EMAILS = """
MATCH (u:User)
WHERE u.email IN $emails
RETURN collect(u.email) AS emails
"""

CREATE_PRE_REGISTERED_USER = """
CREATE (u:User {
  email: $email,
  google_sub: null,
  display_name: null,
  role: $role,
  entity_key: $entity_key,
  created_at: datetime(),
  updated_at: datetime()
})
WITH u, $entity_key AS ek
OPTIONAL MATCH (e:Entity {entity_key: ek})
FOREACH (_ IN CASE WHEN e IS NOT NULL THEN [1] ELSE [] END |
  MERGE (u)-[:EMPLOYEE_OF]->(e)
)
RETURN u {.email, .google_sub, .role, .entity_key, .display_name} AS user
"""
```

- [ ] **Step 4: Add typed store dataclasses and imports**

In `services/api/src/auth/store.py`, update imports:

```python
from dataclasses import dataclass
from typing import Literal, cast
```

Also import new queries:

```python
    CREATE_PRE_REGISTERED_USER,
    EXISTING_USER_EMAILS,
```

Add these dataclasses below `_VALID_ROLES`:

```python
@dataclass(frozen=True)
class PreRegisterUserInput:
    email: str
    role: Role
    entity_key: str | None


PreRegisterStatus = Literal["created", "error"]


@dataclass(frozen=True)
class PreRegisterUserResult:
    email: str
    status: PreRegisterStatus
    code: str | None
    message: str | None
    user: AuthUser | None
```

- [ ] **Step 5: Add store helpers**

In `services/api/src/auth/store.py`, add these functions after `list_users()`:

```python
def normalize_email(email: str) -> str:
    """Normalize a user email for storage and lookups."""
    return email.strip().lower()


async def existing_user_emails(emails: list[str]) -> set[str]:
    """Return emails already present as :User nodes."""
    if not emails:
        return set()
    async with get_session() as session:
        result = await session.run(EXISTING_USER_EMAILS, emails=emails)
        record = await result.single()
    if record is None:
        return set()
    raw = record["emails"]
    if not isinstance(raw, list):
        return set()
    return {to_str(email) for email in raw if email is not None}


async def create_pre_registered_user(row: PreRegisterUserInput) -> AuthUser:
    """Create a pre-registered :User node without Google identity fields."""
    async with get_session(write=True) as session:
        result = await session.run(
            CREATE_PRE_REGISTERED_USER,
            email=row.email,
            role=row.role,
            entity_key=row.entity_key,
        )
        record = await result.single()
    if record is None:
        raise RuntimeError("Failed to create pre-registered user")
    return _user_from_record(record["user"])


async def bulk_pre_register_users(rows: list[PreRegisterUserInput]) -> list[PreRegisterUserResult]:
    """Create pre-registered users and return per-row outcomes."""
    normalized_rows = [
        PreRegisterUserInput(
            email=normalize_email(row.email), role=row.role, entity_key=row.entity_key
        )
        for row in rows
    ]
    existing = await existing_user_emails([row.email for row in normalized_rows])
    results: list[PreRegisterUserResult] = []
    for row in normalized_rows:
        if row.email in existing:
            results.append(
                PreRegisterUserResult(
                    email=row.email,
                    status="error",
                    code="user_exists",
                    message="User already exists.",
                    user=None,
                )
            )
            continue
        created = await create_pre_registered_user(row)
        results.append(
            PreRegisterUserResult(
                email=row.email,
                status="created",
                code=None,
                message=None,
                user=created,
            )
        )
    return results
```

- [ ] **Step 6: Run tests to verify Task 2 passes**

Run:

```bash
uv run pytest services/api/tests/test_user_onboarding.py -v
```

Expected: PASS for all tests in the file.

---

### Task 3: Backend bulk admin endpoint validates rows and returns row-level results

**Files:**
- Modify: `services/api/src/routes/users.py`
- Test: `services/api/tests/test_user_onboarding.py`

- [ ] **Step 1: Add failing endpoint tests with monkeypatched dependencies**

Append to `services/api/tests/test_user_onboarding.py`:

```python
@pytest.mark.asyncio
async def test_bulk_create_users_rejects_duplicate_request_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_entity_exists(entity_key: str) -> bool:
        assert entity_key == "ada"
        return True

    monkeypatch.setattr(users, "entity_exists", fake_entity_exists)

    body = users.UserBulkCreateRequest(
        users=[
            users.UserBulkCreateRow(email="new@example.com", role="employee", entity_key="ada"),
            users.UserBulkCreateRow(email="NEW@example.com", role="employee", entity_key="ada"),
        ]
    )

    response = await users.bulk_create_users(body, _request(), AuthUser(email="admin@example.com", google_sub="sub", role="admin"))

    assert response.data.results[0].status == "error"
    assert response.data.results[0].code == "duplicate_email"
    assert response.data.results[1].status == "error"
    assert response.data.results[1].code == "duplicate_email"


@pytest.mark.asyncio
async def test_bulk_create_users_creates_valid_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.auth import store

    async def fake_entity_exists(entity_key: str) -> bool:
        assert entity_key == "ada"
        return True

    async def fake_bulk_pre_register_users(
        rows: list[store.PreRegisterUserInput],
    ) -> list[store.PreRegisterUserResult]:
        assert rows == [
            store.PreRegisterUserInput(email="new@example.com", role="employee", entity_key="ada"),
            store.PreRegisterUserInput(email="boss@example.com", role="admin", entity_key=None),
        ]
        return [
            store.PreRegisterUserResult(
                email="new@example.com",
                status="created",
                code=None,
                message=None,
                user=AuthUser(email="new@example.com", google_sub=None, role="employee", entity_key="ada"),
            ),
            store.PreRegisterUserResult(
                email="boss@example.com",
                status="created",
                code=None,
                message=None,
                user=AuthUser(email="boss@example.com", google_sub=None, role="admin"),
            ),
        ]

    monkeypatch.setattr(users, "entity_exists", fake_entity_exists)
    monkeypatch.setattr(users, "bulk_pre_register_users", fake_bulk_pre_register_users)

    body = users.UserBulkCreateRequest(
        users=[
            users.UserBulkCreateRow(email=" New@Example.com ", role="employee", entity_key="ada"),
            users.UserBulkCreateRow(email="Boss@Example.com", role="admin", entity_key="ada"),
        ]
    )

    response = await users.bulk_create_users(body, _request(), AuthUser(email="admin@example.com", google_sub="sub", role="admin"))

    assert [result.status for result in response.data.results] == ["created", "created"]
    assert response.data.results[0].user is not None
    assert response.data.results[0].user.email == "new@example.com"
    assert response.data.results[1].user is not None
    assert response.data.results[1].user.entity_key is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest services/api/tests/test_user_onboarding.py -v
```

Expected: FAIL because bulk route models and handler do not exist.

- [ ] **Step 3: Import bulk store functions and add models**

In `services/api/src/routes/users.py`, change the store import to include bulk types/functions:

```python
from src.auth.store import (
    PreRegisterUserInput,
    PreRegisterUserResult,
    bulk_pre_register_users,
    entity_exists,
    list_users,
    normalize_email,
    update_user,
)
```

Add these Pydantic models after `UserUpdateRequest`:

```python
class UserBulkCreateRow(BaseModel):
    email: str
    role: Role
    entity_key: str | None = None


class UserBulkCreateResult(BaseModel):
    email: str
    status: Literal["created", "error"]
    code: str | None = None
    message: str | None = None
    user: UserResponse | None = None


class UserBulkCreateResponse(BaseModel):
    results: list[UserBulkCreateResult]


class UserBulkCreateRequest(BaseModel):
    users: list[UserBulkCreateRow]
```

- [ ] **Step 4: Add conversion and row-error helpers**

In `services/api/src/routes/users.py`, add below `_normalise_assignment()`:

```python
def _result_to_response(result: PreRegisterUserResult) -> UserBulkCreateResult:
    return UserBulkCreateResult(
        email=result.email,
        status=result.status,
        code=result.code,
        message=result.message,
        user=_to_response(result.user) if result.user is not None else None,
    )


def _row_error(email: str, code: str, message: str) -> UserBulkCreateResult:
    return UserBulkCreateResult(
        email=email,
        status="error",
        code=code,
        message=message,
        user=None,
    )
```

- [ ] **Step 5: Add bulk endpoint**

In `services/api/src/routes/users.py`, add this handler between `get_users()` and `patch_user()`:

```python
@router.post("/bulk")
async def bulk_create_users(
    body: UserBulkCreateRequest,
    request: Request,
    _admin: AuthUser = Depends(require_admin),
) -> ApiResponse[UserBulkCreateResponse]:
    normalized_emails = [normalize_email(row.email) for row in body.users]
    duplicate_emails = {
        email for email in normalized_emails if normalized_emails.count(email) > 1
    }
    results: list[UserBulkCreateResult | None] = [None for _ in body.users]
    valid_rows: list[PreRegisterUserInput] = []
    valid_indexes: list[int] = []

    for index, row in enumerate(body.users):
        email = normalized_emails[index]
        if not email or "@" not in email:
            results[index] = _row_error(email, "invalid_email", "Enter a valid email address.")
            continue
        if email in duplicate_emails:
            results[index] = _row_error(email, "duplicate_email", "Email appears more than once in this request.")
            continue
        try:
            role, entity_key = _normalise_assignment(row.role, row.entity_key)
        except UserAssignmentError as exc:
            results[index] = _row_error(email, exc.code, exc.message)
            continue
        if entity_key is not None and not await entity_exists(entity_key):
            results[index] = _row_error(
                email, "not_found", f"Entity '{entity_key}' does not exist."
            )
            continue
        valid_rows.append(PreRegisterUserInput(email=email, role=role, entity_key=entity_key))
        valid_indexes.append(index)

    created_results = await bulk_pre_register_users(valid_rows)
    for index, result in zip(valid_indexes, created_results, strict=True):
        results[index] = _result_to_response(result)

    final_results = [
        result
        for result in results
        if result is not None
    ]
    return envelope(UserBulkCreateResponse(results=final_results), request)
```

- [ ] **Step 6: Run tests to verify Task 3 passes**

Run:

```bash
uv run pytest services/api/tests/test_user_onboarding.py -v
```

Expected: PASS for all onboarding tests.

---

### Task 4: Frontend types and BFF route for bulk creation

**Files:**
- Modify: `services/frontend/src/lib/api-types-ops.ts`
- Create: `services/frontend/src/app/bff/users/bulk/route.ts`

- [ ] **Step 1: Add frontend bulk types**

In `services/frontend/src/lib/api-types-ops.ts`, add below `UserResponse`:

```typescript
export interface UserBulkCreateRow {
  email: string;
  role: Role;
  entity_key: string | null;
}

export type UserBulkCreateStatus = "created" | "error";

export interface UserBulkCreateResult {
  email: string;
  status: UserBulkCreateStatus;
  code: string | null;
  message: string | null;
  user: UserResponse | null;
}

export interface UserBulkCreateResponse {
  results: UserBulkCreateResult[];
}

export interface UserBulkCreateRequest {
  users: UserBulkCreateRow[];
}
```

- [ ] **Step 2: Create the BFF bulk route**

Create `services/frontend/src/app/bff/users/bulk/route.ts`:

```typescript
import type { NextResponse } from "next/server";

import type { UserBulkCreateResponse } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<NextResponse> {
  const body: unknown = await request.json();
  return proxyToApi<UserBulkCreateResponse>("/users/bulk", {
    method: "POST",
    body,
  });
}
```

- [ ] **Step 3: Run frontend typecheck for BFF/types**

Run:

```bash
cd services/frontend && npm run typecheck
```

Expected: PASS or only pre-existing unrelated failures. Any failure in `api-types-ops.ts` or `app/bff/users/bulk/route.ts` must be fixed before continuing.

---

### Task 5: Admin users page bulk editor

**Files:**
- Modify: `services/frontend/src/app/admin/users/page.tsx`

- [ ] **Step 1: Update imports and local row types**

In `services/frontend/src/app/admin/users/page.tsx`, change the API type import to:

```typescript
import type {
  UserBulkCreateRequest,
  UserBulkCreateResult,
  UserResponse,
} from "@/lib/api-types-ops";
```

Add these interfaces near `EntityRow`:

```typescript
interface DraftUserRow {
  id: number;
  email: string;
  role: Role;
  entityKey: string;
  result: UserBulkCreateResult | null;
}

interface BulkEditorProps {
  entities: readonly EntityRow[];
  onComplete: () => Promise<void>;
  onUpdateExisting: (email: string, updates: { role?: Role; entity_key?: string | null }) => Promise<void>;
}
```

- [ ] **Step 2: Add state and render the bulk editor**

Inside `UsersAdminPage`, add state after existing `busy` state:

```typescript
  const [bulkBusy, setBulkBusy] = useState<boolean>(false);
```

In the returned JSX, insert this before the existing users table `<Paper>`:

```tsx
      <BulkUserEditor
        entities={entityOptions}
        onComplete={loadUsers}
        onUpdateExisting={patchUser}
      />
```

If `bulkBusy` is unused after later steps, remove it before final verification.

- [ ] **Step 3: Add the bulk editor component**

Add this component above `UserRowEditor`:

```tsx
function BulkUserEditor(props: BulkEditorProps): ReactElement {
  const [rows, setRows] = useState<DraftUserRow[]>([
    { id: 1, email: "", role: "employee", entityKey: "", result: null },
  ]);
  const [nextId, setNextId] = useState<number>(2);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  function updateRow(id: number, patch: Partial<Omit<DraftUserRow, "id">>): void {
    setRows((current) =>
      current.map((row) => {
        if (row.id !== id) return row;
        const nextRole = patch.role ?? row.role;
        return {
          ...row,
          ...patch,
          entityKey: nextRole === "employee" ? patch.entityKey ?? row.entityKey : "",
          result: null,
        };
      }),
    );
  }

  function addRow(): void {
    setRows((current) => [
      ...current,
      { id: nextId, email: "", role: "employee", entityKey: "", result: null },
    ]);
    setNextId((current) => current + 1);
  }

  function removeRow(id: number): void {
    setRows((current) => current.filter((row) => row.id !== id));
  }

  async function submit(): Promise<void> {
    setSubmitting(true);
    setError(null);
    try {
      const body: UserBulkCreateRequest = {
        users: rows.map((row) => ({
          email: row.email,
          role: row.role,
          entity_key: row.role === "employee" ? row.entityKey || null : null,
        })),
      };
      const response = await bffFetch<{ results: UserBulkCreateResult[] }>("/bff/users/bulk", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      setRows((current) =>
        current.map((row, index) => ({
          ...row,
          result: response.results[index] ?? null,
        })),
      );
      await props.onComplete();
    } catch (e: unknown) {
      setError(e instanceof BffError ? e.message : "Failed to add users");
    } finally {
      setSubmitting(false);
    }
  }

  async function updateExisting(row: DraftUserRow): Promise<void> {
    await props.onUpdateExisting(row.email, {
      role: row.role,
      entity_key: row.role === "employee" ? row.entityKey || null : null,
    });
    updateRow(row.id, {
      result: {
        email: row.email,
        status: "created",
        code: null,
        message: "Existing user updated.",
        user: null,
      },
    });
  }

  return (
    <Paper sx={{ p: 2 }}>
      <Stack spacing={2}>
        <Box>
          <Typography variant="h6" fontWeight={700}>
            Add users
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Pre-register users by email so their access is ready before first sign-in.
          </Typography>
        </Box>
        {error ? <Alert severity="error">{error}</Alert> : null}
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Email</TableCell>
              <TableCell>Role</TableCell>
              <TableCell>Entity</TableCell>
              <TableCell>Status</TableCell>
              <TableCell align="right">Actions</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.id}>
                <TableCell>
                  <TextField
                    size="small"
                    value={row.email}
                    onChange={(e) => updateRow(row.id, { email: e.target.value })}
                    placeholder="user@example.com"
                    sx={{ minWidth: 240 }}
                  />
                </TableCell>
                <TableCell>
                  <TextField
                    size="small"
                    select
                    value={row.role}
                    onChange={(e) => {
                      const nextRole: string = e.target.value;
                      if (isRole(nextRole)) updateRow(row.id, { role: nextRole });
                    }}
                    sx={{ minWidth: 130 }}
                  >
                    {ROLE_OPTIONS.map((role) => (
                      <MenuItem key={role} value={role}>
                        {role}
                      </MenuItem>
                    ))}
                  </TextField>
                </TableCell>
                <TableCell>
                  <TextField
                    size="small"
                    select
                    value={row.entityKey}
                    onChange={(e) => updateRow(row.id, { entityKey: e.target.value })}
                    disabled={row.role !== "employee"}
                    sx={{ minWidth: 200 }}
                  >
                    <MenuItem value="">
                      <em>None</em>
                    </MenuItem>
                    {props.entities.map((entity) => (
                      <MenuItem key={entity.entity_key} value={entity.entity_key}>
                        {entity.display_name ?? entity.entity_key}
                      </MenuItem>
                    ))}
                  </TextField>
                </TableCell>
                <TableCell>{renderBulkStatus(row.result)}</TableCell>
                <TableCell align="right">
                  <Stack direction="row" spacing={1} justifyContent="flex-end">
                    {row.result?.code === "user_exists" ? (
                      <Button size="small" onClick={() => void updateExisting(row)}>
                        Update existing user
                      </Button>
                    ) : null}
                    <Button size="small" color="inherit" onClick={() => removeRow(row.id)} disabled={rows.length === 1}>
                      Remove
                    </Button>
                  </Stack>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <Stack direction="row" spacing={1}>
          <Button size="small" variant="outlined" onClick={addRow}>
            Add row
          </Button>
          <Button size="small" variant="contained" disabled={submitting} onClick={() => void submit()}>
            {submitting ? "Adding…" : "Add users"}
          </Button>
        </Stack>
      </Stack>
    </Paper>
  );
}
```

- [ ] **Step 4: Add status renderer helper**

Add this helper above `BulkUserEditor`:

```tsx
function renderBulkStatus(result: UserBulkCreateResult | null): ReactElement | string {
  if (result === null) return "—";
  if (result.status === "created") {
    return <Chip size="small" color="success" label={result.message ?? "Created"} />;
  }
  return <Chip size="small" color="error" label={result.message ?? "Error"} />;
}
```

- [ ] **Step 5: Run frontend typecheck**

Run:

```bash
cd services/frontend && npm run typecheck
```

Expected: PASS. Fix any strict TypeScript errors in `page.tsx` before continuing.

- [ ] **Step 6: Run frontend lint**

Run:

```bash
cd services/frontend && npm run lint
```

Expected: PASS with no new warnings over the existing warning budget. If ESLint reports a new `react-hooks/set-state-in-effect` warning from this page, add the required inline disable only on the triggering line.

---

### Task 6: Verification and manual UI check

**Files:**
- Verify all changed files

- [ ] **Step 1: Run API onboarding tests**

Run:

```bash
uv run pytest services/api/tests/test_user_onboarding.py -v
```

Expected: PASS.

- [ ] **Step 2: Run API lint**

Run:

```bash
uv run --package profile-unifier-api ruff check services/api/src services/api/tests/test_user_onboarding.py
```

Expected: PASS.

- [ ] **Step 3: Run API typecheck**

Run:

```bash
uv run --package profile-unifier-api mypy --strict services/api/src
```

Expected: PASS or only documented pre-existing failures in `types_sales.py` and `types_requests.py`. Any new errors in `auth/models.py`, `auth/store.py`, `graph/queries/users.py`, or `routes/users.py` must be fixed.

- [ ] **Step 4: Run frontend typecheck and lint**

Run:

```bash
cd services/frontend && npm run typecheck && npm run lint
```

Expected: PASS.

- [ ] **Step 5: Rebuild and start changed Docker services**

Run:

```bash
docker compose build --no-cache api frontend && docker compose up -d api frontend
```

Expected: API and frontend containers rebuild and start successfully.

- [ ] **Step 6: Exercise the admin UI in a browser**

Open the app through the configured web proxy, sign in as an admin, and visit `/admin/users`.

Manual checks:

1. Add an employee row with a valid entity and submit. Expected: row shows Created; user appears in the users table with assigned entity and blank name/Google identity.
2. Add an admin row while an entity is selected, then submit. Expected: entity is cleared/ignored; row shows Created; user appears as admin with no entity.
3. Add the same email again and submit. Expected: row shows User already exists and displays Update existing user.
4. Click Update existing user after changing role/entity. Expected: existing row updates through PATCH and the users table refreshes.
5. Confirm open login is unchanged by signing in with a different unregistered Google email if available. Expected: user is created as `first_time`.

- [ ] **Step 7: Inspect git diff**

Run:

```bash
git diff -- services/api/src/auth/models.py services/api/src/auth/store.py services/api/src/graph/queries/users.py services/api/src/routes/users.py services/api/tests/test_user_onboarding.py services/frontend/src/lib/api-types-ops.ts services/frontend/src/app/bff/users/bulk/route.ts services/frontend/src/app/admin/users/page.tsx docs/superpowers/specs/2026-05-04-user-onboarding-design.md docs/superpowers/plans/2026-05-04-user-onboarding.md
```

Expected: diff contains only the planned onboarding feature and spec/plan docs. Do not commit unless the user explicitly asks.

---

## Self-Review

- Spec coverage: all design requirements map to tasks: same `:User` node, open login preservation, active-on-first-login, bulk per-row role/entity, existing-user row errors plus explicit update action, no email notifications, frontend/BFF/backend changes, and verification.
- Placeholder scan: no TBD/TODO/implement-later placeholders remain.
- Type consistency: backend uses `Role`, `PreRegisterUserInput`, `PreRegisterUserResult`, and frontend uses `UserBulkCreate*` names consistently.
