"""Golden profile recompute transaction — shared between survivorship and review routes.

Also defines the canonical mapping from editable golden-profile field names onto
their backing graph evidence (HAS_FACT / IDENTIFIED_BY / LIVES_AT) plus the helper
that re-derives a field's value from a chosen source record. Both the survivorship
override apply path and this recompute honour the same mapping, so a pinned override
survives a later recompute.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from neo4j import AsyncManagedTransaction

from src.graph.converters import GraphValue, to_optional_str, to_str, to_str_dict
from src.graph.queries import (
    CHECK_PERSON_ACTIVE,
    CREATE_RECOMPUTE_AUDIT,
    GET_ADDRESS_BY_NORMALIZED,
    GET_ADDRESS_FOR_SR,
    GET_BEST_ADDRESS,
    GET_BEST_IDENTIFIER,
    GET_FACT_VALUE,
    GET_IDENTIFIER_VALUE_FOR_SR,
    GET_PERSON_FACTS,
    GET_PERSON_OVERRIDES,
    UPDATE_GOLDEN_PROFILE,
)

TRUST_RANK: dict[str, int] = {"tier_1": 1, "tier_2": 2, "tier_3": 3, "tier_4": 4}
INVALID_QUALITY_FLAGS: frozenset[str] = frozenset({"invalid_format", "placeholder_value"})
GOLDEN_FACT_FIELDS: tuple[str, ...] = ("full_name", "phone", "email", "dob")

#: Editable golden-profile field -> (source_kind, evidence key).
#: ``key`` is the HAS_FACT attribute name for facts, the identifier_type for
#: identifiers, and ``None`` for address (resolved via LIVES_AT).
GOLDEN_FIELD_SPEC: dict[str, tuple[str, str | None]] = {
    "preferred_full_name": ("source_record_fact", "full_name"),
    "preferred_dob": ("source_record_fact", "dob"),
    "preferred_race_ethnicity": ("source_record_fact", "race_ethnicity"),
    "preferred_phone": ("identifier", "phone"),
    "preferred_email": ("identifier", "email"),
    "preferred_nric": ("identifier", "nric"),
    "preferred_address": ("address", None),
}


@dataclass
class _BestFact:
    value: str | None
    trust_rank: int
    observed_at: str


@dataclass
class DerivedValue:
    """A value re-derived from a chosen source record, plus the person property to SET."""

    field_to_set: str
    value: str | None


def _select_best_fact(current: _BestFact | None, candidate: _BestFact) -> _BestFact:
    if current is None:
        return candidate
    if candidate.trust_rank < current.trust_rank:
        return candidate
    if candidate.trust_rank == current.trust_rank and candidate.observed_at > current.observed_at:
        return candidate
    return current


def _fact_value_to_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _field_trust_tier(field_trust: GraphValue, attribute_name: str) -> str:
    value = to_str_dict(field_trust).get(attribute_name)
    return value if value is not None else "tier_4"


def _empty_fact() -> _BestFact:
    return _BestFact(value=None, trust_rank=99, observed_at="")


def _normalize_custom_address(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _completeness_score(
    full_name: str | None,
    phone: str | None,
    email: str | None,
    dob: str | None,
    has_address: bool,
) -> float:
    core = (full_name, phone, email, dob)
    filled = sum(1 for v in core if v is not None) + (1 if has_address else 0)
    return filled / (len(GOLDEN_FACT_FIELDS) + 1)


def parse_overrides(raw: object) -> dict[str, dict[str, str]]:
    """Decode the ``survivorship_overrides`` property into nested str->str dicts.

    Stored as a JSON string (Neo4j cannot hold a nested map as a property), but
    tolerates a raw dict for robustness.
    """
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    return {
        to_str(k): {to_str(ik): to_str(iv) for ik, iv in v.items()}
        for k, v in raw.items()
        if isinstance(v, dict)
    }


async def _load_overrides(tx: AsyncManagedTransaction, person_id: str) -> dict[str, dict[str, str]]:
    result = await tx.run(GET_PERSON_OVERRIDES, person_id=person_id)
    record = await result.single()
    return parse_overrides(record["overrides"] if record else None)


async def derive_override_value(
    tx: AsyncManagedTransaction,
    person_id: str,
    field_name: str,
    source_record_pk: str,
) -> DerivedValue | None:
    """Re-derive the value a field would take from ``source_record_pk``.

    Returns None when the field is unknown or the source record carries no value
    for it. The value is read from the graph (never trusted from the client).
    """
    spec = GOLDEN_FIELD_SPEC.get(field_name)
    if spec is None:
        return None
    source_kind, key = spec
    if source_kind == "source_record_fact":
        record = await (
            await tx.run(
                GET_FACT_VALUE,
                person_id=person_id,
                attribute_name=key,
                source_record_pk=source_record_pk,
            )
        ).single()
        if record is None:
            return None
        return DerivedValue(field_name, _fact_value_to_str(record["value"]))
    if source_kind == "identifier":
        record = await (
            await tx.run(
                GET_IDENTIFIER_VALUE_FOR_SR,
                person_id=person_id,
                source_record_pk=source_record_pk,
                identifier_type=key,
            )
        ).single()
        if record is None:
            return None
        return DerivedValue(field_name, to_optional_str(record["value"]))
    record = await (
        await tx.run(
            GET_ADDRESS_FOR_SR,
            person_id=person_id,
            source_record_pk=source_record_pk,
        )
    ).single()
    if record is None:
        return None
    return DerivedValue("preferred_address_id", to_optional_str(record["address_id"]))


async def _gather_best_facts(
    tx: AsyncManagedTransaction,
    person_id: str,
    overrides: dict[str, dict[str, str]],
) -> dict[str, _BestFact]:
    facts_result = await tx.run(GET_PERSON_FACTS, person_id=person_id)

    best: dict[str, _BestFact] = {}
    async for record in facts_result:
        attr_name = to_str(record["attribute_name"])
        attr_value = _fact_value_to_str(record["attribute_value"])
        quality_flag = to_str(record["quality_flag"], "valid")
        trust_tier = _field_trust_tier(record["field_trust"], attr_name)
        source_pk = to_str(record["source_record_pk"])
        observed_at = to_str(record["observed_at"], "")

        if quality_flag in INVALID_QUALITY_FLAGS:
            continue

        override = overrides.get(f"preferred_{attr_name}")
        if override is not None:
            custom_value = override.get("custom_value")
            if custom_value:
                best[attr_name] = _BestFact(
                    value=custom_value, trust_rank=0, observed_at=observed_at
                )
                continue
            if override.get("source_record_pk") == source_pk:
                best[attr_name] = _BestFact(value=attr_value, trust_rank=0, observed_at=observed_at)
                continue

        rank = TRUST_RANK.get(trust_tier, 4)
        best[attr_name] = _select_best_fact(
            best.get(attr_name),
            _BestFact(value=attr_value, trust_rank=rank, observed_at=observed_at),
        )
    for attr_name in (*GOLDEN_FACT_FIELDS, "race_ethnicity"):
        override = overrides.get(f"preferred_{attr_name}")
        custom_value = override.get("custom_value") if override is not None else None
        if custom_value:
            best[attr_name] = _BestFact(value=custom_value, trust_rank=0, observed_at="")
    return best


async def _resolve_identifier(
    tx: AsyncManagedTransaction,
    person_id: str,
    identifier_type: str,
    override: dict[str, str] | None,
) -> str | None:
    if override is not None:
        custom_value = override.get("custom_value")
        if custom_value:
            return custom_value
        source_record_pk = override.get("source_record_pk")
        if source_record_pk:
            record = await (
                await tx.run(
                    GET_IDENTIFIER_VALUE_FOR_SR,
                    person_id=person_id,
                    source_record_pk=source_record_pk,
                    identifier_type=identifier_type,
                )
            ).single()
            if record is not None:
                value = to_optional_str(record["value"])
                if value is not None:
                    return value
    result = await tx.run(GET_BEST_IDENTIFIER, person_id=person_id, identifier_type=identifier_type)
    record = await result.single()
    if record is None:
        return None
    return to_optional_str(record["normalized_value"])


async def _resolve_best_address(
    tx: AsyncManagedTransaction,
    person_id: str,
    override: dict[str, str] | None,
) -> str | None:
    if override is not None:
        custom_value = override.get("custom_value")
        if custom_value:
            record = await (
                await tx.run(
                    GET_ADDRESS_BY_NORMALIZED,
                    normalized_full=_normalize_custom_address(custom_value),
                )
            ).single()
            if record is not None:
                address_id = to_optional_str(record["address_id"])
                if address_id is not None:
                    return address_id
        source_record_pk = override.get("source_record_pk")
        if source_record_pk:
            record = await (
                await tx.run(
                    GET_ADDRESS_FOR_SR,
                    person_id=person_id,
                    source_record_pk=source_record_pk,
                )
            ).single()
            if record is not None:
                address_id = to_optional_str(record["address_id"])
                if address_id is not None:
                    return address_id
    address_result = await tx.run(GET_BEST_ADDRESS, person_id=person_id)
    record = await address_result.single()
    if record is None:
        return None
    return to_optional_str(record["address_id"])


async def recompute_golden_profile_tx(tx: AsyncManagedTransaction, person_id: str) -> float | None:
    """Recompute golden profile for person_id within a write transaction.

    Returns completeness score, or None if the person is not found / not active.
    """
    person_check = await tx.run(CHECK_PERSON_ACTIVE, person_id=person_id)
    if await person_check.single() is None:
        return None

    overrides = await _load_overrides(tx, person_id)
    best_by_field = await _gather_best_facts(tx, person_id, overrides)
    full_name = best_by_field.get("full_name", _empty_fact()).value
    dob = best_by_field.get("dob", _empty_fact()).value
    race_ethnicity = best_by_field.get("race_ethnicity", _empty_fact()).value
    phone = await _resolve_identifier(tx, person_id, "phone", overrides.get("preferred_phone"))
    email = await _resolve_identifier(tx, person_id, "email", overrides.get("preferred_email"))
    nric = await _resolve_identifier(tx, person_id, "nric", overrides.get("preferred_nric"))
    address_id = await _resolve_best_address(tx, person_id, overrides.get("preferred_address"))

    completeness = _completeness_score(full_name, phone, email, dob, address_id is not None)
    version = f"computed-{datetime.now(UTC).isoformat()}"
    await tx.run(
        UPDATE_GOLDEN_PROFILE,
        person_id=person_id,
        full_name=full_name,
        phone=phone,
        email=email,
        dob=dob,
        race_ethnicity=race_ethnicity,
        address_id=address_id,
        nric=nric,
        completeness=completeness,
        version=version,
    )
    await tx.run(CREATE_RECOMPUTE_AUDIT, person_id=person_id)
    return completeness
