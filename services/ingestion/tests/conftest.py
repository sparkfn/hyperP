"""pytest configuration for the ingestion service test suite."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_INGESTION_ROOT = _REPO_ROOT / "services" / "ingestion"
if str(_INGESTION_ROOT) not in sys.path:
    sys.path.insert(0, str(_INGESTION_ROOT))
