"""Bounded streaming parsing for persisted CRM deal-reference NDJSON pages."""

from __future__ import annotations

import json
from pathlib import Path

from intelligence.artifacts import canonical_json
from intelligence.crm_deal_refs.models import MAX_PAGE_BYTES, MAX_RECORD_BYTES, Boundary, PageKind
from intelligence.crm_deal_refs.row_validation import validate_row


def page_rows(path: Path, kind: PageKind, boundary: Boundary) -> list[dict[str, object]]:
    """Read one bounded canonical NDJSON page without materializing the whole file."""
    if path.stat().st_size > MAX_PAGE_BYTES:
        raise ValueError("snapshot page exceeds size limit")
    rows: list[dict[str, object]] = []
    with path.open("rb") as handle:
        while line := handle.readline(MAX_RECORD_BYTES + 2):
            row = _page_row(line, kind, boundary)
            rows.append(row)
            if len(rows) > boundary.page_size:
                raise ValueError("snapshot page record count exceeds boundary limit")
    return rows


def _page_row(line: bytes, kind: PageKind, boundary: Boundary) -> dict[str, object]:
    if not line.endswith(b"\n"):
        raise ValueError("snapshot page line exceeds size limit")
    content = line[:-1]
    if content.endswith(b"\r"):
        raise ValueError("snapshot page newline is not canonical")
    if len(content) >= MAX_RECORD_BYTES:
        raise ValueError("snapshot page line exceeds size limit")
    try:
        encoded = content.decode("utf-8")
        value = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("page row is invalid") from error
    if not isinstance(value, dict) or canonical_json(value) != encoded:
        raise ValueError("page row is invalid")
    row = {str(key): item for key, item in value.items()}
    validate_row(row, kind, boundary)
    return row
