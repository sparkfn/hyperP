"""Address normalization — regex-based parsing into structured components.

Designed for Singapore addresses as the primary format, with a fallback
partial parse for other formats.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import TypedDict

from src.llm import ChatMessage, get_llm_service
from src.llm_prompts import ADDRESS_NORMALIZATION_SYSTEM, build_address_normalization_prompt
from src.models import QualityFlag, RawAddress

logger = logging.getLogger(__name__)

# Matches common Singapore address patterns:
#   "#05-123 10 Example Street Singapore 123456"
#   "10 Example Street, Singapore 123456"
#   "Blk 10 Example Street #05-123 Singapore 123456"
_SG_ADDRESS_RE = re.compile(
    r"(?:#?(?P<unit>\d{1,3}-\d{1,4})\s+)?"  # optional unit e.g. #05-123
    r"(?:(?:Blk|Block)\s+)?"  # optional Blk/Block prefix
    r"(?P<street_num>\d+[A-Za-z]?)\s+"  # street number
    r"(?P<street_name>.+?)"  # street name (non-greedy)
    r"(?:\s*,?\s*(?:Singapore|SG))?"  # optional city marker
    r"\s+(?P<postal>\d{6})"  # 6-digit postal code
    r"\s*$",
    re.IGNORECASE,
)


class _LlmAddress(TypedDict):
    input_index: int
    unit_number: str | None
    street_number: str | None
    street_name: str | None
    building_name: str | None
    city: str | None
    state_province: str | None
    postal_code: str | None
    country_code: str | None
    normalized_full: str | None


@dataclass(frozen=True, slots=True)
class NormalizedAddress:
    """Structured, normalized address ready for graph storage."""

    unit_number: str | None
    street_number: str
    street_name: str
    building_name: str | None
    city: str
    state_province: str | None
    postal_code: str
    country_code: str
    normalized_full: str


_PLACEHOLDER_VALUES: frozenset[str] = frozenset(
    {"na", "n/a", "-", "unknown", "nil", "none", "test", "tbc", "tba"}
)


def _partial_parse(
    raw: str, default_country: str, default_city: str
) -> tuple[NormalizedAddress | None, QualityFlag]:
    """Fallback: extract a 6-digit postal code from an otherwise unparseable address."""
    postal_match = re.search(r"\b(\d{6})\b", raw)
    if not postal_match:
        return None, QualityFlag.INVALID_FORMAT
    postal = postal_match.group(1)
    full = re.sub(r"\s+", " ", raw).strip().lower()
    return NormalizedAddress(
        unit_number=None,
        street_number="",
        street_name=full,
        building_name=None,
        city=default_city.lower(),
        state_province=None,
        postal_code=postal,
        country_code=default_country.upper(),
        normalized_full=full,
    ), QualityFlag.PARTIAL_PARSE


def _full_parse(match: re.Match[str], default_country: str, default_city: str) -> NormalizedAddress:
    """Build a NormalizedAddress from a successful SG regex match."""
    unit = match.group("unit")
    street_num = match.group("street_num").strip().lower()
    street_name = re.sub(r"\s+", " ", match.group("street_name")).strip().lower()
    postal = match.group("postal")
    city = default_city.lower()
    country = default_country.upper()

    parts = [street_num, street_name]
    if unit:
        parts.insert(0, f"#{unit}")
    parts.append(f"{city} {postal}")
    parts.append(country.lower())

    return NormalizedAddress(
        unit_number=unit,
        street_number=street_num,
        street_name=street_name,
        building_name=None,
        city=city,
        state_province=None,
        postal_code=postal,
        country_code=country,
        normalized_full=", ".join(parts),
    )


def normalize_address(
    raw: str,
    *,
    default_country: str = "SG",
    default_city: str = "Singapore",
) -> tuple[NormalizedAddress | None, QualityFlag]:
    """Parse a raw address string into structured components.

    Returns ``(NormalizedAddress, quality_flag)``.  If the address cannot be
    parsed at all, returns ``(None, 'invalid_format')``.
    """
    stripped = raw.strip()
    if not stripped:
        return None, QualityFlag.INVALID_FORMAT
    if stripped.lower() in _PLACEHOLDER_VALUES:
        return None, QualityFlag.PLACEHOLDER_VALUE

    match = _SG_ADDRESS_RE.match(stripped)
    if not match:
        return _partial_parse(stripped, default_country, default_city)
    return _full_parse(match, default_country, default_city), QualityFlag.VALID


def normalize_raw_addresses(
    addresses: list[RawAddress],
) -> list[tuple[NormalizedAddress, QualityFlag]]:
    raw_inputs = [_raw_address_text(address) for address in addresses]
    usable_inputs = [raw for raw in raw_inputs if raw]
    if not usable_inputs:
        return []
    llm_results = _normalize_with_llm(usable_inputs)
    return _merge_llm_and_fallback(usable_inputs, llm_results)


def _raw_address_text(address: RawAddress) -> str | None:
    parts = [
        address.unit_number,
        address.street_number,
        address.street_name,
        address.building_name,
        address.city,
        address.state_province,
        address.postal_code,
        address.country_code,
    ]
    text = ", ".join(str(part).strip() for part in parts if part)
    if text and _has_structured_location(address):
        return text
    if address.raw:
        return address.raw.strip()
    return text or None


def _has_structured_location(address: RawAddress) -> bool:
    return any(
        bool(value and value.strip())
        for value in (
            address.unit_number,
            address.street_number,
            address.street_name,
            address.building_name,
        )
    )


def _normalize_with_llm(inputs: list[str]) -> dict[int, list[NormalizedAddress]]:
    try:
        response_text = asyncio.run(_normalize_with_llm_async(inputs))
    except Exception as exc:
        logger.warning("LLM address normalization failed: %s", exc)
        return {}
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError:
        logger.warning("LLM address normalization returned invalid JSON")
        return {}
    if not isinstance(payload, dict):
        return {}
    raw_addresses = payload.get("addresses")
    if not isinstance(raw_addresses, list):
        return {}
    results: dict[int, list[NormalizedAddress]] = {}
    for raw_item in raw_addresses:
        parsed = _parse_llm_address(raw_item)
        if parsed is None:
            continue
        input_index, address = parsed
        results.setdefault(input_index, []).append(address)
    return results


async def _normalize_with_llm_async(inputs: list[str]) -> str:
    svc = get_llm_service()
    return await svc.chat_json(
        [
            ChatMessage(role="system", content=ADDRESS_NORMALIZATION_SYSTEM),
            ChatMessage(role="user", content=build_address_normalization_prompt(inputs)),
        ]
    )


def _parse_llm_address(raw: object) -> tuple[int, NormalizedAddress] | None:
    if not isinstance(raw, dict):
        return None
    input_index = raw.get("input_index")
    if not isinstance(input_index, int):
        return None
    postal_code = _str_or_none(raw.get("postal_code"))
    street_name = _str_or_none(raw.get("street_name"))
    normalized_full = _str_or_none(raw.get("normalized_full"))
    if not postal_code or not street_name or not normalized_full:
        return None
    country_code = _str_or_none(raw.get("country_code")) or "SG"
    city = _str_or_none(raw.get("city")) or "singapore"
    address = NormalizedAddress(
        unit_number=_str_or_none(raw.get("unit_number")),
        street_number=_str_or_none(raw.get("street_number")) or "",
        street_name=street_name.lower(),
        building_name=_str_or_none(raw.get("building_name")),
        city=city.lower(),
        state_province=_str_or_none(raw.get("state_province")),
        postal_code=postal_code,
        country_code=country_code.upper(),
        normalized_full=normalized_full.lower(),
    )
    return input_index, address


def _merge_llm_and_fallback(
    inputs: list[str],
    llm_results: dict[int, list[NormalizedAddress]],
) -> list[tuple[NormalizedAddress, QualityFlag]]:
    results: list[tuple[NormalizedAddress, QualityFlag]] = []
    seen: set[tuple[str, str, str, str, str | None]] = set()
    for index, raw in enumerate(inputs):
        addresses = llm_results.get(index, [])
        if addresses:
            for address in addresses:
                _append_unique(results, seen, address, QualityFlag.VALID)
            continue
        fallback, flag = normalize_address(raw)
        if fallback is not None:
            _append_unique(results, seen, fallback, flag)
    return results


def _append_unique(
    results: list[tuple[NormalizedAddress, QualityFlag]],
    seen: set[tuple[str, str, str, str, str | None]],
    address: NormalizedAddress,
    flag: QualityFlag,
) -> None:
    key = _dedupe_key(address)
    if key in seen:
        return
    seen.add(key)
    results.append((address, flag))


def _dedupe_key(address: NormalizedAddress) -> tuple[str, str, str, str, str | None]:
    normalized_full = re.sub(r"\s+", " ", address.normalized_full).strip().lower()
    if normalized_full:
        return (address.country_code, address.postal_code, normalized_full, "", None)
    return (
        address.country_code,
        address.postal_code,
        address.street_number,
        address.street_name,
        address.unit_number,
    )


def _str_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text else None
