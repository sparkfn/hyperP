"""Machine-facing OAuth2 FastAPI sub-application (mounted at /oauth2/v1).

Re-exposes the OAuth2 client-credentials token flow plus a read-only person
list/detail surface for server-to-server callers. The token and JWKS endpoints
are unauthenticated (they bootstrap auth); the person endpoints accept HyperP
OAuth2 client credentials only — human/Google principals are rejected.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.params import Depends as DependsMarker

from src.auth.deps import require_oauth_client_scope
from src.auth.oauth_client_models import OAuthClientScope
from src.error_handlers import register_error_handlers
from src.router_copy import copy_router_routes
from src.routes import identity_link_revisions, oauth, persons

# Only the person list (GET /v1/persons) and canonical detail
# (GET /v1/persons/{person_id}) are exposed on this machine surface.
_PERSON_ROUTE_PATHS: frozenset[str] = frozenset({"/v1/persons", "/v1/persons/{person_id}"})


def _strip_oauth_prefix(path: str) -> str:
    """Map the oauth router's /v1/oauth/* paths to /* on the /oauth2/v1 mount.

    The mount path already conveys "oauth", so keeping /v1/oauth would make the
    segment redundant (…/oauth2/v1/oauth/token). This yields /token and /jwks.
    """
    return path.removeprefix("/v1/oauth")


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

    # Person list + canonical detail: OAuth2 client credentials with persons:read only.
    person_dependencies: list[DependsMarker] = [
        Depends(require_oauth_client_scope(OAuthClientScope.PERSONS_READ))
    ]
    copy_router_routes(
        app,
        persons.router,
        dependencies=person_dependencies,
        path_filter=_PERSON_ROUTE_PATHS.__contains__,
    )

    register_error_handlers(app)
    return app
