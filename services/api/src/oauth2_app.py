"""Machine-facing OAuth2 FastAPI sub-application (mounted at /oauth2/v1).

Serves the canonical /token and /jwks flow (root /v1/oauth is deprecated) plus a read-only
person list/search/detail surface for server-to-server callers. The token and JWKS endpoints
are unauthenticated (they bootstrap auth); the person endpoints accept HyperP
OAuth2 client credentials only — human/Google principals are rejected.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.params import Depends as DependsMarker

from src.auth.deps import require_oauth_client_scope
from src.auth.oauth_client_models import OAuthClientScope
from src.error_handlers import register_error_handlers
from src.http_utils import http_error
from src.router_copy import copy_router_routes
from src.routes import identity_link_revisions, oauth, persons
from src.types import IdentifierType

# Only the person list (GET /v1/persons) and canonical detail
# (GET /v1/persons/{person_id}) are exposed on this machine surface.
_PERSON_ROUTE_PATHS: frozenset[str] = frozenset({"/v1/persons", "/v1/persons/{person_id}"})

# Exact-identifier search is copied separately, ahead of list/detail, so the
# static /persons/search path is never captured as a person_id.
_PERSON_SEARCH_ROUTE_PATHS: frozenset[str] = frozenset({"/v1/persons/search"})

_MACHINE_IDENTIFIER_TYPES: tuple[str, ...] = tuple(item.value for item in IdentifierType)

_MACHINE_PERSON_SEARCH_DESCRIPTION = (
    "Exact identifier lookup for OAuth2 machine clients. Supply `identifier_type` and "
    "`value` together with a nonblank `value`; a partial pair, a blank `value`, or an "
    "unsupported `identifier_type` is rejected with `invalid_request`. Supported "
    "`identifier_type` values: `phone`, `email`, `nric`, `external_customer_id`, "
    "`membership_id`, `crm_contact_id`, `loyalty_id`, `custom`. Matching compares the "
    "canonical normalized value stored by ingestion; for `nric`, input is canonicalized and "
    "keyed server-side against both keyed digests and legacy records. Exact identifier search "
    "results are unpaginated, ordered by `updated_at` descending, and exclude only Persons "
    "with status `merged`. A `q`-only request uses the existing paginated fuzzy search "
    "(`cursor`/`limit`, `meta.next_cursor`), and its empty result is not an authoritative "
    "identifier negative."
)


def _strip_oauth_prefix(path: str) -> str:
    """Map the oauth router's /v1/oauth/* paths to /* on the /oauth2/v1 mount.

    The mount path already conveys "oauth", so keeping /v1/oauth would make the
    segment redundant (…/oauth2/v1/oauth/token). This yields /token and /jwks.
    """
    return path.removeprefix("/v1/oauth")


async def _validate_machine_person_search(request: Request) -> None:
    """Reject incomplete or unsupported exact-identifier lookups on the machine mount.

    Requests without either exact parameter fall through to the shared handler's
    free-text/no-parameter behavior unchanged.
    """
    identifier_type = request.query_params.get("identifier_type")
    value = request.query_params.get("value")
    if identifier_type is None and value is None:
        return
    if identifier_type is None or value is None or not value.strip():
        raise http_error(
            400,
            "invalid_request",
            "Provide identifier_type and value together; value must be nonblank.",
            request,
        )
    if identifier_type not in _MACHINE_IDENTIFIER_TYPES:
        supported = ", ".join(_MACHINE_IDENTIFIER_TYPES)
        raise http_error(
            400,
            "invalid_request",
            f"Unsupported identifier_type. Supported values: {supported}.",
            request,
        )


def build_oauth2_app() -> FastAPI:
    """Build the machine-facing OAuth2 API contract mounted at /oauth2/v1."""
    app = FastAPI(
        title="HyperP OAuth2 API",
        version="0.1.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
        swagger_ui_parameters={"operationsSorter": "alpha"},
        openapi_tags=[
            {
                "name": "OAuth",
                "description": "Machine-to-machine OAuth2 client credentials token flow.",
            },
            {
                "name": "Persons",
                "description": "Read-only person profiles for OAuth2 machine clients.",
            },
        ],
    )

    # Token issuance + JWKS: unauthenticated. Exposed as /token and /jwks
    # (the /oauth2/v1 mount already conveys "oauth").
    copy_router_routes(app, oauth.router, dependencies=[], path_transform=_strip_oauth_prefix)

    app.include_router(identity_link_revisions.router)

    # Person search: OAuth2 client credentials with persons:read plus the
    # machine-only exact-pair validation. Copied before list/detail so the
    # static /persons/search path is not matched as a person ID.
    person_dependencies: list[DependsMarker] = [
        Depends(require_oauth_client_scope(OAuthClientScope.PERSONS_READ))
    ]
    copy_router_routes(
        app,
        persons.router,
        dependencies=[*person_dependencies, Depends(_validate_machine_person_search)],
        path_filter=_PERSON_SEARCH_ROUTE_PATHS.__contains__,
        description_override=_MACHINE_PERSON_SEARCH_DESCRIPTION,
    )

    # Person list + canonical detail: same OAuth2 client-credentials scope.
    copy_router_routes(
        app,
        persons.router,
        dependencies=person_dependencies,
        path_filter=_PERSON_ROUTE_PATHS.__contains__,
    )

    register_error_handlers(app)
    return app
