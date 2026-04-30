"""pytest configuration for the API service test suite."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# conftest.py lives at <repo-root>/services/api/tests/conftest.py
#   parent[0] = tests/
#   parent[1] = api/
#   parent[2] = services/
#   parent[3] = repo-root/
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_API_ROOT = _REPO_ROOT / "services" / "api"
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

_MIN_ENV = {
    "NEO4J_URI": "bolt://localhost:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "fake-password",
    "GOOGLE_OAUTH_CLIENT_ID": "fake",
    "GOOGLE_OAUTH_CLIENT_SECRET": "fake",
    "GOOGLE_OAUTH_REDIRECT_URI": "http://localhost",
    "BOOTSTRAP_ADMIN_EMAILS": "",
    "REDIS_URL": "redis://localhost:6379",
    "ACCESS_TOKEN_EXPIRY_MINUTES": "60",
    "REFRESH_TOKEN_EXPIRY_MINUTES": "43200",
    "API_KEYS_ENABLED": "false",
    "API_KEY_SECRET": "",
    "API_KEY_HEADER_NAME": "X-Api-Key",
    "LLM_API_BASE_URL": "https://api.openai.com",
}
for key, val in _MIN_ENV.items():
    os.environ.setdefault(key, val)
