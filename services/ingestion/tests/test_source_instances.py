"""Security and canonicalization contracts for source-instance identifiers."""

from __future__ import annotations

import pytest
from src.source_instances import canonical_source_instance_id


@pytest.mark.parametrize(
    "value",
    [
        "bitrix-primary",
        "portal_2",
        "a",
        "a" * 64,
    ],
)
def test_canonical_source_instance_slug_is_accepted(value: str) -> None:
    assert canonical_source_instance_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        " bitrix-primary",
        "bitrix-primary ",
        "Bitrix-Primary",
        "a" * 65,
        "-bitrix",
        "bitrix-",
        "https://portal.example/rest/secret",
        "portal.example/rest/hook",
        "portal:secret",
    ],
)
def test_ambiguous_or_credential_shaped_source_instance_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="canonical non-secret slug"):
        canonical_source_instance_id(value)
