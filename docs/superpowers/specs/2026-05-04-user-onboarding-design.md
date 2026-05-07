# User Onboarding Design

## Goal

Allow admin users to pre-register users by email and assign their role/entity before those users first sign in. This removes the current dependency on desired users logging in once and landing in `first_time` before an admin can grant access.

## Decisions

- Keep the current open Google login flow. Any valid Google user can still sign in and be created as `first_time`.
- Use the existing `:User` node as the onboarding record. Do not introduce separate invite or onboarding nodes.
- Pre-registered users become active immediately on first login with their assigned role/entity.
- Support bulk in-page entry with per-row email, role, and entity values.
- Allow pre-registering both `employee` and `admin` users.
- Do not send invitation or notification email; this is an internal allowlist/access-preparation workflow.
- If an entered email already exists, treat it as a row-level error and offer an explicit update action instead of silently overwriting.

## Existing Context

The backend already has admin-only user management in `services/api/src/routes/users.py`. It can list known users and patch role/entity for users who already exist. Users are stored in Neo4j as `:User` nodes keyed by email via queries in `services/api/src/graph/queries/users.py`. The current Google login path creates missing users as `first_time` unless the email is in `BOOTSTRAP_ADMIN_EMAILS`.

The frontend already has a user-management page at `services/frontend/src/app/admin/users/page.tsx`. It loads users and entities, then lets admins update existing users one row at a time. The missing capability is creating users before first login.

## Backend Model and Login Behavior

Pre-registration will create a `:User {email}` node with:

- normalized lowercase email
- assigned `role`
- assigned `entity_key` when role is `employee`
- no `google_sub` until first login
- timestamps for creation/update

Because pre-registered users do not have a Google identity yet, the API/frontend user contract must allow `google_sub` to be null.

On first Google login, the existing upsert flow will match the pre-created `:User` by email, attach or refresh `google_sub`, refresh `display_name`, and update `last_login_at`. It must preserve an existing admin-assigned role/entity. If there is no existing user node, the current behavior remains: bootstrap admin emails become `admin`; all other users become `first_time` with no entity.

Role/entity invariants remain unchanged:

- `employee` requires a valid `entity_key`.
- `admin` must not have an entity assignment.
- `first_time` must not have an entity assignment.

## Admin API

Add an admin-only bulk creation endpoint under the existing users router, for example `POST /v1/users/bulk`.

The request accepts rows with:

- `email`
- `role`
- `entity_key`

The endpoint validates rows and returns per-row results. Validation includes:

- valid email shape
- duplicate emails within the request
- valid role
- employee rows include an entity
- admin and first-time rows do not carry an entity
- referenced entities exist
- email does not already correspond to an existing user

Existing users are row-level errors and are not updated by this endpoint. When an admin chooses to update an existing user, the UI should call the existing `PATCH /v1/users/{email}` route with the row’s explicit role/entity values.

The response should let the frontend map outcomes back to rows, including created rows and row-level validation failures.

## Frontend UX

Extend `/admin/users` with an “Add users” bulk editor near the existing user table.

The bulk editor allows admins to add multiple rows. Each row has:

- email field
- role selector
- entity selector
- row-level status/error display

Role selection controls entity behavior:

- `employee`: entity selector is enabled and required.
- `admin`: entity selector is cleared and disabled.
- `first_time`: entity selector is cleared and disabled.

On submit, the page sends all valid rows to the BFF route for bulk creation. The UI displays row-level outcomes. If a row fails because the user already exists, the row shows that message and offers an “Update existing user” action, which calls the existing PATCH BFF route for that email.

The existing users table remains for ongoing role/entity management after users are created or have logged in.

## Data Flow

1. Admin opens `/admin/users`.
2. Frontend loads current users and entities.
3. Admin adds one or more onboarding rows.
4. Frontend submits rows through the BFF.
5. BFF proxies to FastAPI with the admin’s auth token.
6. FastAPI validates rows and creates new `:User` nodes for non-existing valid users.
7. Later, the user signs in with Google.
8. Login upsert matches the pre-created `:User`, attaches Google identity fields, preserves assigned access, and the user enters the app immediately.

## Error Handling

Errors should be row-level wherever possible so one bad row does not hide the outcome of other rows. The frontend should surface at least:

- invalid email
- duplicate email in the submitted rows
- missing entity for employee role
- invalid/nonexistent entity
- existing user with explicit update option
- generic create/update failure

System-wide request failures can still be shown as page-level alerts.

## Testing

Backend tests should cover:

- bulk create succeeds for employee and admin rows
- employee rows require existing entities
- admin and first-time rows clear/reject entity assignment
- duplicate request emails return row errors
- existing users return row errors and are not overwritten by bulk create
- pre-registered user login preserves assigned role/entity while filling Google identity fields
- unregistered user login still creates `first_time`

Frontend tests or manual verification should cover:

- adding/removing multiple rows
- role selector enabling/disabling entity selector correctly
- row-level validation and result display
- existing-user error exposing explicit update action
- existing user table still updates via PATCH

Verification commands after implementation should include backend tests/lint/type checks and frontend typecheck/lint. Because this changes UI, the admin flow should also be exercised in a browser if the environment allows.
