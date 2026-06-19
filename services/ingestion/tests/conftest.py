"""pytest configuration for the ingestion service test suite."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_INGESTION_ROOT = _REPO_ROOT / "services" / "ingestion"
if str(_INGESTION_ROOT) not in sys.path:
    sys.path.insert(0, str(_INGESTION_ROOT))

# src.celery_app imports config at module load time; provide a minimal env so
# modules that import tasks can be collected without a real Neo4j connection.
os.environ.setdefault("NEO4J_PASSWORD", "test-password")
os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
