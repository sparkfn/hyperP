"""Audit guard: verify connector skip sites contain record_skip calls."""

from __future__ import annotations

from pathlib import Path
import re

_BASE_DIR = Path(__file__).resolve().parent.parent / "src" / "connectors"

_EXPECTED_SKIPS: dict[str, list[str]] = {
    "eko/connector.py": ["employee_customer"],
    "speedzone/connector.py": ["employee_customer"],
    "phppos_sales_common.py": ["employee_sale"],
    "fundbox/users.py": ["internal_user"],
    "fundbox/legacy.py": ["internal_user"],
    "fundbox/sales.py": ["internal_user"],
    "fundbox/merged.py": ["internal_user"],
    "onediver/connector.py": ["deleted_profile", "missing_id", "empty_content"],
    "dumps/connectors.py": [
        "order_status_excluded",
        "session_status_excluded",
        "org_unmapped",
        "empty_content",
        "orphan_or_deleted_parent",
        "crm_category_missing",
        "extraction_failed",
        "dump_secondary_person_dropped",
        "deleted_customer",
        "missing_person_row",
    ],
    "whatsapp/connector.py": ["org_unmapped", "empty_content", "extraction_failed", "person_excluded"],
    "whatsadmin_api/connector.py": ["empty_content"],
    "whatsadmin_api/incremental.py": ["empty_content"],
    "bitrix/connector.py": ["crm_category_missing", "extraction_failed", "person_excluded"],
    "bitrix_openlines/connector.py": ["crm_category_missing", "empty_content"],
    "sggov/bankruptcy.py": ["missing_id"],
}


def test_connector_skip_coverage() -> None:
    for rel_path, reasons in _EXPECTED_SKIPS.items():
        file_path = _BASE_DIR / rel_path
        assert file_path.is_file(), f"Connector file not found: {file_path}"
        content = file_path.read_text(encoding="utf-8")
        for reason in reasons:
            pattern = rf'record_skip\(\s*["\']{reason}["\']'
            assert re.search(pattern, content) or f'reason="{reason}"' in content or f'reason=\'{reason}\'' in content, (
                f"Missing record_skip({reason!r}) in {rel_path}"
            )
