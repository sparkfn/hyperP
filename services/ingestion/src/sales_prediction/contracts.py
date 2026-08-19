"""Fixed contract identifiers for the #125 CRM win MVP dataset.

Every identifier below was either selected by the #149 corrective Gate 1 rerun
(recorded on issue #125, 2026-08-19) or is a #125 artifact schema version.
They are constants, not configuration: re-running the pipeline against a
different release must fail closed instead of silently rebinding.
"""

from __future__ import annotations

import re

# --- #149 accepted-release binding -------------------------------------------------

SELECTOR_VERSION = "retrospective-source-availability-v1"
ELIGIBILITY_VERSION = "crm-won-retrospective-eligibility-v1"
AVAILABILITY_SEMANTICS = "retrospective_source_native"
RESTATEMENT_VERSION = "authority-head-v1"

DEFAULT_EXPECTED_MAPPING_VERSION = "crm-stage-map-2026-08-18-v1"
DEFAULT_EXPECTED_POLICY_VERSION = "crm-stage-lifecycle-policy-2026-08-18-v1"

HORIZON_DAYS = 30

# --- #125 artifact schemas ----------------------------------------------------------

DATASET_SCHEMA_VERSION = "issue-125-crm-dataset-v1"
EVALUATION_SCHEMA_VERSION = "issue-125-crm-evaluation-v1"
MODEL_SCHEMA_VERSION = "issue-125-crm-model-v1"

ARTIFACT_KIND_DATASET = "sales-dataset"
ARTIFACT_KIND_EVALUATION = "sales-evaluation"
ARTIFACT_KIND_MODEL = "sales-model"

# Separate HMAC domain: a sales artifact manifest can never be authenticated
# (or replayed) as a Bitrix stage-history artifact and vice versa.
SALES_ARTIFACT_HMAC_DOMAIN = b"sales-prediction-restricted-artifact-manifest-v1\x00"

_ENTITY_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def parse_entity_keys(value: str) -> tuple[str, ...]:
    """Parse and validate a comma-separated entity key list."""
    keys = tuple(item.strip() for item in value.split(",") if item.strip())
    if not keys:
        raise ValueError("at least one entity key is required")
    for key in keys:
        if not _ENTITY_KEY_PATTERN.match(key):
            raise ValueError(f"invalid entity key: {key}")
    if len(set(keys)) != len(keys):
        raise ValueError("entity keys must be unique")
    return keys
