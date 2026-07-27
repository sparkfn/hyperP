"""Shared payload builders for ingestion orchestration tests."""

from __future__ import annotations

import json

from pydantic.types import JsonValue


def manifest_payload() -> dict[str, JsonValue]:
    """Return a valid all-source ingestion payload."""
    return {
        "identity": [
            {"source_key": "fundbox", "mode": "api"},
            {
                "source_key": "fundbox:legacy",
                "mode": "dump",
                "dump_path": "full/fundbox_legacy.sql",
            },
            {
                "source_key": "fundbox:merged",
                "mode": "dump",
                "dump_path": "full/fundbox_merged.sql",
            },
            {"source_key": "eko_phppos", "mode": "api"},
            {"source_key": "speedzone_phppos", "mode": "api"},
            {"source_key": "onediver", "mode": "dump", "dump_path": "full/onediver.sql"},
        ],
        "dependent": [
            {"source_key": "fundbox:contacts", "mode": "api"},
            {"source_key": "fundbox:sales", "mode": "api"},
            {"source_key": "eko_phppos:sales", "mode": "api"},
            {"source_key": "speedzone_phppos:sales", "mode": "api"},
            {
                "source_key": "onediver:sales",
                "mode": "dump",
                "dump_path": "full/onediver_sales.sql",
            },
            {"source_key": "bitrix_chat", "mode": "api"},
            {"source_key": "whatsapp_chat", "mode": "api"},
            {"source_key": "sgbankruptcy", "mode": "api"},
            {"source_key": "sgrentalflats", "mode": "api"},
        ],
    }


def manifest_json() -> str:
    """Return the valid payload serialized for CLI tests."""
    return json.dumps(manifest_payload())
