from __future__ import annotations

import pytest
from src.connectors.bitrix_stage_history.capability_provenance import (
    effective_config_fingerprint,
    normalize_image_digest,
    portal_fingerprint,
)
from src.ingestion_config import BitrixOpenLinesConfig


def test_portal_fingerprint_excludes_webhook_path_and_is_keyed() -> None:
    key = b"a" * 32
    first = portal_fingerprint(key, "https://portal.example.test/rest/secret/path")
    second = portal_fingerprint(key, "https://PORTAL.example.test/another/path")

    assert first == second
    assert first.startswith("hmac-sha256:")
    assert "portal.example.test" not in first
    assert first == portal_fingerprint(key, "https://portal.example.test:443/rest/hook")


def test_portal_fingerprint_rejects_userinfo() -> None:
    with pytest.raises(ValueError, match="credentials"):
        portal_fingerprint(b"a" * 32, "https://user:secret@portal.example.test/rest")


def test_capability_hmac_rejects_short_keys() -> None:
    from src.connectors.bitrix_stage_history.capability_provenance import capability_hmac

    with pytest.raises(ValueError, match="32 bytes"):
        capability_hmac(b"short", "test-domain", b"value")


def test_effective_config_fingerprint_changes_for_selected_mapping() -> None:
    config = BitrixOpenLinesConfig(entity_by_crm_category_id={"2": "alpha", "3": "beta"})

    first = effective_config_fingerprint(b"a" * 32, config, ("2",))
    second = effective_config_fingerprint(b"a" * 32, config, ("3",))

    assert first != second
    assert first.startswith("hmac-sha256:")


def test_image_digest_requires_immutable_sha256() -> None:
    digest = "sha256:" + "a" * 64
    assert normalize_image_digest(digest.upper()) == digest
    assert normalize_image_digest(None) is None
    with pytest.raises(ValueError, match="immutable"):
        normalize_image_digest("latest")
