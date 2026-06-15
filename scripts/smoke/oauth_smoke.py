"""Live end-to-end smoke test for the machine OAuth2 flow.

Runs INSIDE the `api` container (CWD `/app/services/api`, so `import src.*`
works) and is normally launched by `scripts/smoke/run-oauth-smoke.sh`, which
pipes it in via `docker compose exec -T api python -`.

Two surfaces are exercised:

* The genuinely-live HTTP path through nginx (`$SMOKE_BASE_URL`, default
  `http://web`): token issuance (`POST /api/oauth2/v1/token`) and a protected
  machine resource (`GET /api/oauth2/v1/persons`). This proves real routing,
  the `/oauth2/v1` mount, scope enforcement, the per-client token TTL, the
  `secret_id` kill-switch, manual revocation, and that `X-Forwarded-For`
  reaches the token registry as `last_used_ip`.
* The service layer directly, for the admin-only steps (create / rotate /
  list-tokens / revoke) that the HTTP API gates behind `require_human_admin`
  (a Google admin token a script cannot mint). These call the same functions
  the admin endpoints call, against the same shared Neo4j + Redis the live
  uvicorn process uses.

Exits non-zero on the first failed assertion so CI can gate on it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from src.auth.models import AuthUser
from src.auth.oauth_client_models import CreateOAuthClientRequest
from src.auth.oauth_clients import (
    create_oauth_client,
    delete_oauth_client,
    rotate_oauth_client_secret,
)
from src.auth.oauth_token_registry import (
    TokenRecord,
    list_client_tokens,
    revoke_token_entry,
)
from src.auth.revoke import decode_jwt_claims, revoke_token

BASE_URL = os.environ.get("SMOKE_BASE_URL", "http://web").rstrip("/")
TOKEN_PATH = "/api/oauth2/v1/token"
PERSONS_PATH = "/api/oauth2/v1/persons"
HEALTH_PATH = "/api/health"
CLIENT_TTL_SECONDS = 300  # the 5-minute floor — distinctive, proves per-client TTL drives exp

_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"

_failures: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> bool:
    """Record a named assertion and print its result."""
    marker = _PASS if ok else _FAIL
    suffix = f" — {detail}" if detail else ""
    print(f"  [{marker}] {name}{suffix}", flush=True)
    if not ok:
        _failures.append(name)
    return ok


def _http(
    method: str,
    path: str,
    *,
    form: dict[str, str] | None = None,
    bearer: str | None = None,
) -> tuple[int, bytes]:
    """Make an HTTP request through nginx; return (status, body). 4xx/5xx do not raise."""
    headers: dict[str, str] = {}
    data: bytes | None = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — internal nginx URL
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _mint_token(client_id: str, client_secret: str, scope: str) -> tuple[int, int | None, str | None]:
    """POST the token endpoint. Return (status, expires_in, access_token)."""
    status, body = _http(
        "POST",
        TOKEN_PATH,
        form={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
        },
    )
    if status != 200:
        return status, None, None
    parsed = json.loads(body)
    return status, parsed.get("expires_in"), parsed.get("access_token")


def _find_token(records: list[TokenRecord], jti: str) -> TokenRecord | None:
    return next((r for r in records if r.jti == jti), None)


async def main() -> int:
    print(f"OAuth smoke test against {BASE_URL}\n", flush=True)
    client_id: str | None = None

    try:
        # ── 1. Stack is alive ───────────────────────────────────────────────
        print("1. Health", flush=True)
        status, _ = _http("GET", HEALTH_PATH)
        _check("GET /api/health → 200", status == 200, f"got {status}")

        # ── 2. Provision a client (admin path, via service layer) ───────────
        print("2. Provision client (persons:read, ttl=300s)", flush=True)
        actor = AuthUser(email="smoke@local", google_sub="smoke", role="admin")
        created = await create_oauth_client(
            CreateOAuthClientRequest(
                name="smoke-test-client",
                scopes=["persons:read"],
                access_token_ttl_seconds=CLIENT_TTL_SECONDS,
            ),
            actor,
        )
        client_id = created.client_id
        secret_v1 = created.client_secret
        _check("client created", bool(client_id), client_id or "")

        # ── 3. Mint a token over HTTP; per-client TTL drives exp ────────────
        print("3. Mint access token (live, through nginx)", flush=True)
        status, expires_in, token_v1 = _mint_token(client_id, secret_v1, "persons:read")
        _check("POST /token → 200", status == 200, f"got {status}")
        _check("expires_in == client TTL (300)", expires_in == CLIENT_TTL_SECONDS, f"got {expires_in}")
        jti_v1, _exp_v1 = decode_jwt_claims(token_v1 or "")
        _check("token carries a jti", jti_v1 is not None)

        # ── 4. Token authorizes the protected machine resource ──────────────
        print("4. Use token on protected resource", flush=True)
        status, _ = _http("GET", PERSONS_PATH, bearer=token_v1)
        _check("GET /persons with token → 200", status == 200, f"got {status}")

        # ── 5. No token is rejected (confirms auth is actually enforced) ────
        print("5. Negative: no bearer", flush=True)
        status, _ = _http("GET", PERSONS_PATH)
        _check("GET /persons without token → 401", status == 401, f"got {status}")

        # ── 6. Registry tracks the token + records last-used IP ─────────────
        print("6. Token registry + last-used IP", flush=True)
        records = await list_client_tokens(client_id)
        rec_v1 = _find_token(records, jti_v1 or "")
        _check("token appears in registry", rec_v1 is not None)
        _check(
            "last_used_ip captured (X-Forwarded-For)",
            rec_v1 is not None and rec_v1.last_used_ip is not None,
            rec_v1.last_used_ip if rec_v1 is not None else "no record",
        )

        # ── 7. Rotation kills outstanding tokens (secret_id kill-switch) ────
        print("7. Rotate secret → old token dies", flush=True)
        rotated = await rotate_oauth_client_secret(client_id)
        _check("rotate returned a new secret", rotated is not None)
        secret_v2 = rotated.client_secret if rotated is not None else ""
        status, _ = _http("GET", PERSONS_PATH, bearer=token_v1)
        _check("old token after rotation → 401", status == 401, f"got {status}")

        # ── 8. Old secret can no longer mint tokens ─────────────────────────
        print("8. Old secret is revoked", flush=True)
        status, _, _ = _mint_token(client_id, secret_v1, "persons:read")
        _check("mint with revoked secret → 401", status == 401, f"got {status}")

        # ── 9. New secret mints a working token ─────────────────────────────
        print("9. New secret works", flush=True)
        status, _expires, token_v2 = _mint_token(client_id, secret_v2, "persons:read")
        _check("POST /token (new secret) → 200", status == 200, f"got {status}")
        jti_v2, exp_v2 = decode_jwt_claims(token_v2 or "")
        status, _ = _http("GET", PERSONS_PATH, bearer=token_v2)
        _check("new token authorizes resource → 200", status == 200, f"got {status}")

        # ── 10. Manual revocation invalidates a live token ──────────────────
        print("10. Manual token revoke", flush=True)
        if jti_v2 is not None and exp_v2 is not None:
            await revoke_token(jti_v2, exp_v2)
            await revoke_token_entry(client_id, jti_v2)
        status, _ = _http("GET", PERSONS_PATH, bearer=token_v2)
        _check("revoked token → 401", status == 401, f"got {status}")

    finally:
        # ── Cleanup: remove the smoke client + its registry entries ─────────
        if client_id is not None:
            print("\nCleanup", flush=True)
            for rec in await list_client_tokens(client_id):
                await revoke_token_entry(client_id, rec.jti)
            deleted = await delete_oauth_client(client_id)
            _check("smoke client deleted", deleted)

    print("", flush=True)
    if _failures:
        print(f"SMOKE FAILED — {len(_failures)} assertion(s): {', '.join(_failures)}", flush=True)
        return 1
    print("SMOKE PASSED — machine OAuth flow healthy.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
