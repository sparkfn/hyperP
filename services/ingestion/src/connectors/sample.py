"""DISABLED — sample connector commented out; see fundbox.py for the active connector."""

_DISABLED = '''
"""Sample connector that yields hardcoded test records.

Designed to exercise the full pipeline: normalization, candidate generation
through shared identifiers/addresses, and new person creation.

Records simulate a small sales dataset from a POS system with overlapping
customers — Alice appears in two records with the same phone, Bob shares
an address with Alice, and Charlie shares an email with Alice.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator

from src.connectors.base import SourceConnector

_SAMPLE_RECORDS: list[dict] = [
    {
        "source_record_id": "pos-001",
        "observed_at": "2026-03-15T10:00:00Z",
        "identifiers": [
            {"type": "phone", "value": "+6591234567", "is_verified": True},
            {"type": "email", "value": "alice@example.com", "is_verified": False},
        ],
        "attributes": {
            "full_name": "Alice Tan",
            "dob": "1989-10-01",
            "address": "10 Example Street, Singapore 123456",
        },
        "raw_payload": {"loyalty_tier": "gold", "last_purchase": "2026-03-14"},
    },
    {
        "source_record_id": "pos-002",
        "observed_at": "2026-03-16T14:30:00Z",
        "identifiers": [
            {"type": "phone", "value": "+6591234567", "is_verified": False},
            {"type": "membership_id", "value": "MEM-2024-0042", "is_verified": True},
        ],
        "attributes": {
            "full_name": "Alice T.",
            "address": "10 Example Street, Singapore 123456",
        },
        "raw_payload": {"loyalty_tier": "gold", "last_purchase": "2026-03-16"},
    },
    {
        "source_record_id": "pos-003",
        "observed_at": "2026-03-17T09:15:00Z",
        "identifiers": [
            {"type": "phone", "value": "+6598765432", "is_verified": True},
            {"type": "email", "value": "bob.lee@example.com", "is_verified": True},
        ],
        "attributes": {
            "full_name": "Bob Lee",
            "dob": "1985-05-22",
            "address": "10 Example Street, Singapore 123456",
        },
        "raw_payload": {"loyalty_tier": "silver", "last_purchase": "2026-03-17"},
    },
    {
        "source_record_id": "pos-004",
        "observed_at": "2026-03-18T11:45:00Z",
        "identifiers": [
            {"type": "phone", "value": "+6593334444", "is_verified": False},
            {"type": "email", "value": "alice@example.com", "is_verified": False},
        ],
        "attributes": {
            "full_name": "Charlie Wong",
            "dob": "1992-08-15",
            "address": "25 Marina Boulevard, Singapore 018950",
        },
        "raw_payload": {"loyalty_tier": "bronze", "last_purchase": "2026-03-18"},
    },
    {
        "source_record_id": "pos-005",
        "observed_at": "2026-03-19T16:00:00Z",
        "identifiers": [
            {"type": "phone", "value": "+6599887766", "is_verified": True},
        ],
        "attributes": {
            "full_name": "Dana Lim",
            "dob": "1995-12-03",
            "address": "25 Marina Boulevard, Singapore 018950",
        },
        "raw_payload": {"loyalty_tier": "silver", "last_purchase": "2026-03-19"},
    },
]


def _compute_hash(record: dict) -> str:
    payload = json.dumps(record, sort_keys=True, default=str)
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


class SampleConnector(SourceConnector):
    """Yields hardcoded sample records for testing the full pipeline."""

    def get_source_key(self) -> str:
        return "sample_pos"

    def fetch_records(self) -> Iterator[dict]:
        for record in _SAMPLE_RECORDS:
            yield {
                **record,
                "record_hash": _compute_hash(record),
            }
'''
