"""Audit guard: verify connector skip sites contain record_skip calls."""

from __future__ import annotations

import ast
import re
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent / "src" / "connectors"

_EXPECTED_SKIPS: dict[str, list[str]] = {
    "eko/connector.py": ["employee_customer"],
    "speedzone/connector.py": ["employee_customer"],
    "phppos_sales_common.py": ["employee_sale", "missing_customer_id"],
    "fundbox/users.py": ["internal_user"],
    "fundbox/legacy.py": ["internal_user"],
    "fundbox/sales.py": ["internal_user"],
    "fundbox/merged.py": ["internal_user"],
    "onediver/connector.py": [
        "deleted_profile",
        "missing_id",
        "empty_content",
        "orphan_or_deleted_parent",
    ],
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
    "whatsapp/connector.py": [
        "org_unmapped",
        "empty_content",
        "extraction_failed",
        "person_excluded",
    ],
    "whatsadmin_api/connector.py": ["empty_content"],
    "whatsadmin_api/incremental.py": ["empty_content"],
    "bitrix/connector.py": ["crm_category_missing", "extraction_failed", "person_excluded"],
    "bitrix_openlines/connector.py": ["crm_category_missing", "empty_content"],
    "sggov/bankruptcy.py": ["missing_id"],
}

# Call sites that reach a *different* function which happens to share its name with an
# ``on_skip``-aware builder. Keyed by ``(relative path, callee name, call line text)``.
_ALLOWED_MISSING_ON_SKIP: dict[tuple[str, str, str], str] = {
    (
        "bitrix_crm/incremental.py",
        "_build_envelopes",
        "envelopes = self._build_envelopes(current)",
    ): "Bitrix CRM deal builder — a different function with no skip channel.",
    (
        "phppos_api/connectors.py",
        "_build_envelope",
        "return _build_envelope(",
    ): "phppos customer envelope builder — a different function with no skip channel.",
    (
        "phppos_sales_common.py",
        "_build_envelope",
        "yield _build_envelope(",
    ): "phppos sales envelope builder — a different function with no skip channel.",
    (
        "sggov/bankruptcy.py",
        "_build_envelope",
        "yield self._build_envelope(case, latest_event, document)",
    ): "SG bankruptcy envelope builder — a different function with no skip channel.",
    (
        "sggov/rental_flats_incremental.py",
        "_build_envelope",
        "envelopes: list[dict[str, JsonValue]] = [_build_envelope(item) for item in page.items]",
    ): "SG rental-flat envelope builder — a different function with no skip channel.",
    (
        "whatsapp/connector.py",
        "_build_envelopes",
        "envelopes = _build_envelopes(",
    ): "Unused single-envelope wrapper — no src caller (tests only).",
}


def test_connector_skip_coverage() -> None:
    for rel_path, reasons in _EXPECTED_SKIPS.items():
        file_path = _BASE_DIR / rel_path
        assert file_path.is_file(), f"Connector file not found: {file_path}"
        content = file_path.read_text(encoding="utf-8")
        for reason in reasons:
            pattern = rf'record_skip\(\s*["\']{reason}["\']'
            assert (
                re.search(pattern, content)
                or f'reason="{reason}"' in content
                or f"reason='{reason}'" in content
            ), f"Missing record_skip({reason!r}) in {rel_path}"


def _on_skip_builder_names() -> set[str]:
    """Names of functions under ``src/connectors`` that declare an ``on_skip`` parameter.

    Aliased imports of such a function (``import x as y``) are included so a call
    through the alias is checked too.
    """
    names: set[str] = set()
    aliases: list[tuple[str, str]] = []
    for path in sorted(_BASE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                params = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
                if any(param.arg == "on_skip" for param in params):
                    names.add(node.name)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.asname is not None:
                        aliases.append((alias.name, alias.asname))
    return names | {alias for original, alias in aliases if original in names}


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def test_on_skip_call_sites_forward_the_skip_channel() -> None:
    """Every call to an ``on_skip``-aware builder must pass ``on_skip=``."""
    builder_names = _on_skip_builder_names()
    assert builder_names
    violations: list[str] = []
    for path in sorted(_BASE_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        rel_path = path.relative_to(_BASE_DIR).as_posix()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            callee = _callee_name(node.func)
            if callee is None or callee not in builder_names:
                continue
            if any(keyword.arg == "on_skip" for keyword in node.keywords):
                continue
            line_text = lines[node.lineno - 1].strip()
            if (rel_path, callee, line_text) in _ALLOWED_MISSING_ON_SKIP:
                continue
            violations.append(f"{rel_path}:{node.lineno}: {callee} — {line_text}")
    assert not violations, (
        "Call sites of an on_skip-aware builder that drop the skip channel: "
        + ", ".join(violations)
    )
