"""Redacted provenance helpers for a Bitrix capability re-gate."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Collection
from dataclasses import asdict
from hashlib import sha256
from urllib.parse import urlsplit

from src.ingestion_config import BitrixOpenLinesConfig

_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def capability_hmac(key: bytes, domain: str, value: bytes) -> str:
    """Return a domain-separated redacted evidence identifier."""
    if len(key) < 32:
        raise ValueError("capability HMAC key must contain at least 32 bytes")
    message = domain.encode("ascii") + b"\x00" + value
    return "hmac-sha256:" + hmac.new(key, message, sha256).hexdigest()


def portal_fingerprint(key: bytes, base_url: str) -> str:
    """Fingerprint only the normalized portal origin, never the webhook path."""
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Bitrix portal URL must include an HTTP(S) hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Bitrix portal URL must not include user credentials")
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError("Bitrix portal URL contains an invalid port") from exc
    default_port = 443 if parsed.scheme == "https" else 80
    port = f":{parsed_port}" if parsed_port is not None and parsed_port != default_port else ""
    normalized = f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}"
    return capability_hmac(key, "bitrix-portal-origin-v1", normalized.encode("utf-8"))


def effective_config_fingerprint(
    key: bytes,
    config: BitrixOpenLinesConfig,
    included_category_ids: Collection[str],
) -> str:
    """Fingerprint only effective Bitrix selection/mapping configuration."""
    payload = asdict(config)
    payload["included_crm_category_ids"] = sorted(set(included_category_ids))
    payload["entity_by_crm_category_id"] = {
        category_id: config.entity_by_crm_category_id[category_id]
        for category_id in sorted(set(included_category_ids))
        if category_id in config.entity_by_crm_category_id
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return capability_hmac(key, "bitrix-effective-ingestion-config-v1", encoded)


def normalize_image_digest(value: str | None) -> str | None:
    """Accept an immutable OCI SHA-256 digest only."""
    if value is None or not value.strip():
        return None
    normalized = value.strip().lower()
    if not _IMAGE_DIGEST.fullmatch(normalized):
        raise ValueError("image digest must be an immutable sha256:<64 hex> digest")
    return normalized
