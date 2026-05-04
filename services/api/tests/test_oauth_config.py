"""Tests for OAuth client-credentials configuration."""

from __future__ import annotations

import pytest
from src.auth import oauth_tokens
from src.config import AppConfig


def test_oauth_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OAUTH_ISSUER", raising=False)
    monkeypatch.delenv("OAUTH_AUDIENCE", raising=False)
    monkeypatch.delenv("OAUTH_ACCESS_TOKEN_EXPIRY_MINUTES", raising=False)
    monkeypatch.delenv("OAUTH_MAX_ACCESS_TOKEN_EXPIRY_MINUTES", raising=False)
    monkeypatch.delenv("OAUTH_ACTIVE_KEY_ID", raising=False)
    monkeypatch.delenv("OAUTH_PRIVATE_KEY_PEM", raising=False)
    monkeypatch.delenv("OAUTH_PUBLIC_KEY_PEM", raising=False)
    monkeypatch.delenv("OAUTH_SECRET_HASH_KEY", raising=False)
    cfg = AppConfig(NEO4J_PASSWORD="pw")

    assert not hasattr(cfg, "api_keys_enabled")
    assert not hasattr(cfg, "api_key_secret")
    assert not hasattr(cfg, "api_key_header_name")
    assert cfg.oauth_issuer == "http://localhost/api"
    assert cfg.oauth_audience == "hyperp-api"
    assert cfg.oauth_access_token_expiry_minutes == 15
    assert cfg.oauth_max_access_token_expiry_minutes == 60
    assert cfg.oauth_active_key_id == "local-dev"
    assert cfg.oauth_private_key_pem == ""
    assert cfg.oauth_public_key_pem == ""
    assert cfg.oauth_secret_hash_key == ""


def test_oauth_config_reads_env_aliases() -> None:
    cfg = AppConfig(
        NEO4J_PASSWORD="pw",
        OAUTH_ISSUER="https://hyperp.example/api",
        OAUTH_AUDIENCE="profile-unifier",
        OAUTH_ACCESS_TOKEN_EXPIRY_MINUTES="10",
        OAUTH_MAX_ACCESS_TOKEN_EXPIRY_MINUTES="30",
        OAUTH_ACTIVE_KEY_ID="kid-2026-05",
        OAUTH_PRIVATE_KEY_PEM="private-pem",
        OAUTH_PUBLIC_KEY_PEM="public-pem",
        OAUTH_SECRET_HASH_KEY="hash-key",
    )

    assert cfg.oauth_issuer == "https://hyperp.example/api"
    assert cfg.oauth_audience == "profile-unifier"
    assert cfg.oauth_access_token_expiry_minutes == 10
    assert cfg.oauth_max_access_token_expiry_minutes == 30
    assert cfg.oauth_active_key_id == "kid-2026-05"
    assert cfg.oauth_private_key_pem == "private-pem"
    assert cfg.oauth_public_key_pem == "public-pem"
    assert cfg.oauth_secret_hash_key == "hash-key"


def test_validate_oauth_runtime_config_accepts_non_empty_values() -> None:
    cfg = AppConfig(
        NEO4J_PASSWORD="pw",
        OAUTH_ISSUER="https://hyperp.example/api",
        OAUTH_AUDIENCE="profile-unifier",
        OAUTH_ACTIVE_KEY_ID="kid-2026-05",
        OAUTH_PRIVATE_KEY_PEM="private-pem",
        OAUTH_PUBLIC_KEY_PEM="public-pem",
        OAUTH_SECRET_HASH_KEY="hash-key",
    )

    oauth_tokens.validate_oauth_runtime_config(cfg)


def test_validate_oauth_runtime_config_rejects_blank_values() -> None:
    cfg = AppConfig(
        NEO4J_PASSWORD="pw",
        OAUTH_ISSUER="https://hyperp.example/api",
        OAUTH_AUDIENCE="profile-unifier",
        OAUTH_ACTIVE_KEY_ID=" ",
        OAUTH_PRIVATE_KEY_PEM="private-pem",
        OAUTH_PUBLIC_KEY_PEM="public-pem",
        OAUTH_SECRET_HASH_KEY="hash-key",
    )

    with pytest.raises(ValueError, match="OAUTH_ACTIVE_KEY_ID"):
        oauth_tokens.validate_oauth_runtime_config(cfg)
