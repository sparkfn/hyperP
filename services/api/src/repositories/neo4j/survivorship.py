"""Neo4j implementation of SurvivorshipRepository."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from neo4j import AsyncManagedTransaction

from src.graph.client import TimedAsyncSession, get_session
from src.graph.converters import to_optional_str, to_str
from src.graph.golden_profile import (
    GOLDEN_FIELD_SPEC,
    DerivedValue,
    derive_override_value,
    parse_overrides,
    recompute_golden_profile_tx,
)
from src.graph.queries import (
    CHECK_SOURCE_RECORD_LINKED,
    CREATE_OVERRIDE_AUDIT,
    GET_FIELD_OPTIONS,
    GET_OVERRIDE_SOURCE_METADATA,
    GET_PERSON_OVERRIDES_FULL,
    UPDATE_GOLDEN_FIELD,
    UPDATE_GOLDEN_FIELDS,
    UPDATE_OVERRIDES,
    UPSERT_CUSTOM_ADDRESS,
)
from src.repositories.protocols.survivorship import (
    BatchOverrideResult,
    FieldOptionRow,
    FieldOptionsData,
)


def _override_entry(
    field_name: str, source_record_pk: str, reason: str, actor_id: str, created_at: str
) -> dict[str, str]:
    source_kind, key = GOLDEN_FIELD_SPEC[field_name]
    return {
        "source_record_pk": source_record_pk,
        "source_kind": source_kind,
        "identifier_type": key if source_kind == "identifier" and key is not None else "",
        "reason": reason,
        "actor_type": "admin",
        "actor_id": actor_id,
        "created_at": created_at,
    }


def _custom_override_entry(
    custom_value: str, reason: str, actor_id: str, created_at: str
) -> dict[str, str]:
    return {
        "source_record_pk": "",
        "source_kind": "literal",
        "identifier_type": "",
        "custom_value": custom_value,
        "reason": reason,
        "actor_type": "admin",
        "actor_id": actor_id,
        "created_at": created_at,
    }


def _normalize_custom_address(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _is_valid_custom_value(field_name: str, value: str) -> bool:
    if value == "":
        return False
    if field_name == "preferred_phone":
        return re.fullmatch(r"\+?[0-9][0-9\s-]{5,19}", value) is not None
    if field_name == "preferred_email":
        return re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) is not None
    if field_name == "preferred_dob":
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return False
    if field_name == "preferred_nric":
        return re.fullmatch(r"[A-Za-z0-9*\-\s]{4,32}", value) is not None
    return True


class Neo4jSurvivorshipRepository:
    async def recompute_golden_profile(self, person_id: str) -> float | None:
        async with get_session(write=True) as session:
            return await session.execute_write(recompute_golden_profile_tx, person_id)

    async def get_field_options(self, person_id: str) -> FieldOptionsData | None:
        async with get_session() as session:
            return await _field_options_tx(session, person_id)

    async def create_override(
        self,
        person_id: str,
        field_name: str,
        source_record_pk: str,
        reason: str,
        actor_id: str,
    ) -> str:
        async with get_session(write=True) as session:
            return await session.execute_write(
                _override_tx,
                person_id,
                field_name,
                source_record_pk,
                reason,
                actor_id,
            )

    async def create_custom_override(
        self,
        person_id: str,
        field_name: str,
        custom_value: str,
        reason: str,
        actor_id: str,
    ) -> str:
        async with get_session(write=True) as session:
            return await session.execute_write(
                _custom_override_tx,
                person_id,
                field_name,
                custom_value,
                reason,
                actor_id,
            )

    async def create_batch_overrides(
        self,
        person_id: str,
        items: list[tuple[str, str]],
        reason: str,
        actor_id: str,
    ) -> BatchOverrideResult:
        async with get_session(write=True) as session:
            return await session.execute_write(
                _batch_override_tx,
                person_id,
                items,
                reason,
                actor_id,
            )


async def _field_options_tx(session: TimedAsyncSession, person_id: str) -> FieldOptionsData | None:
    record = await (await session.run(GET_FIELD_OPTIONS, person_id=person_id)).single()
    if record is None:
        return None

    rows: list[FieldOptionRow] = []
    raw_options = record["options"]
    if isinstance(raw_options, list):
        for raw in raw_options:
            if not isinstance(raw, dict):
                continue
            value = to_optional_str(raw.get("value"))
            field_name = to_optional_str(raw.get("field_name"))
            source_record_pk = to_optional_str(raw.get("source_record_pk"))
            source_system = to_optional_str(raw.get("source_system"))
            if value is None or field_name is None or source_record_pk is None:
                continue
            rows.append(
                FieldOptionRow(
                    field_name=field_name,
                    source_kind=to_str(raw.get("source_kind")),
                    identifier_type=to_optional_str(raw.get("identifier_type")),
                    value=value,
                    address_id=to_optional_str(raw.get("address_id")),
                    source_record_pk=source_record_pk,
                    source_system=source_system if source_system is not None else "",
                    entity_display_name=to_optional_str(raw.get("entity_display_name")),
                    observed_at=to_optional_str(raw.get("observed_at")),
                )
            )

    preferred_full_name = to_optional_str(record["preferred_full_name"])
    preferred_dob = to_optional_str(record["preferred_dob"])
    preferred_phone = to_optional_str(record["preferred_phone"])
    preferred_email = to_optional_str(record["preferred_email"])
    preferred_nric = to_optional_str(record["preferred_nric"])
    preferred_race_ethnicity = to_optional_str(record["preferred_race_ethnicity"])
    preferred_address_id = to_optional_str(record["preferred_address_id"])
    preferred_address_value = to_optional_str(record["preferred_address_value"])
    overrides = parse_overrides(record["overrides"])
    current_values = {
        "preferred_full_name": preferred_full_name,
        "preferred_dob": preferred_dob,
        "preferred_phone": preferred_phone,
        "preferred_email": preferred_email,
        "preferred_nric": preferred_nric,
        "preferred_race_ethnicity": preferred_race_ethnicity,
        "preferred_address": preferred_address_value,
    }
    existing_keys = {(row.field_name, row.source_record_pk) for row in rows}
    for field_name, override in overrides.items():
        source_record_pk = override.get("source_record_pk")
        current_value = current_values.get(field_name)
        if not source_record_pk or current_value is None:
            continue
        if (field_name, source_record_pk) in existing_keys:
            continue
        source_kind, key = GOLDEN_FIELD_SPEC.get(field_name, ("source_record_fact", None))
        metadata = await (
            await session.run(GET_OVERRIDE_SOURCE_METADATA, source_record_pk=source_record_pk)
        ).single()
        if metadata is None:
            continue
        rows.append(
            FieldOptionRow(
                field_name=field_name,
                source_kind=source_kind,
                identifier_type=key if source_kind == "identifier" else None,
                value=current_value,
                address_id=preferred_address_id if field_name == "preferred_address" else None,
                source_record_pk=source_record_pk,
                source_system=to_optional_str(metadata["source_system"]) or "",
                entity_display_name=to_optional_str(metadata["entity_display_name"]),
                observed_at=to_optional_str(metadata["observed_at"]),
            )
        )

    return FieldOptionsData(
        person_id=person_id,
        preferred_full_name=preferred_full_name,
        preferred_dob=preferred_dob,
        preferred_phone=preferred_phone,
        preferred_email=preferred_email,
        preferred_nric=preferred_nric,
        preferred_race_ethnicity=preferred_race_ethnicity,
        preferred_address_id=preferred_address_id,
        preferred_address_value=preferred_address_value,
        overrides=overrides,
        options=rows,
    )


async def _override_tx(
    tx: AsyncManagedTransaction,
    person_id: str,
    field_name: str,
    source_record_pk: str,
    reason: str,
    actor_id: str,
) -> str:
    person_record = await (await tx.run(GET_PERSON_OVERRIDES_FULL, person_id=person_id)).single()
    if person_record is None:
        return "person_not_found"
    if field_name not in GOLDEN_FIELD_SPEC:
        return "invalid_field"
    if (
        await (
            await tx.run(
                CHECK_SOURCE_RECORD_LINKED,
                source_record_pk=source_record_pk,
                person_id=person_id,
            )
        ).single()
        is None
    ):
        return "sr_not_found"

    derived = await derive_override_value(tx, person_id, field_name, source_record_pk)
    if derived is None or derived.value is None:
        return "value_not_found"

    now = datetime.now(UTC).isoformat()
    overrides = parse_overrides(person_record["overrides"])
    overrides[field_name] = _override_entry(field_name, source_record_pk, reason, actor_id, now)
    await tx.run(UPDATE_OVERRIDES, person_id=person_id, overrides=json.dumps(overrides))
    await tx.run(
        UPDATE_GOLDEN_FIELD,
        person_id=person_id,
        field_name=derived.field_to_set,
        value=derived.value,
    )
    await tx.run(CREATE_OVERRIDE_AUDIT, person_id=person_id, actor_id=actor_id, reason=reason)
    return "ok"


async def _custom_override_tx(
    tx: AsyncManagedTransaction,
    person_id: str,
    field_name: str,
    custom_value: str,
    reason: str,
    actor_id: str,
) -> str:
    person_record = await (await tx.run(GET_PERSON_OVERRIDES_FULL, person_id=person_id)).single()
    if person_record is None:
        return "person_not_found"
    if field_name not in GOLDEN_FIELD_SPEC:
        return "invalid_field"

    trimmed_value = custom_value.strip()
    if not _is_valid_custom_value(field_name, trimmed_value):
        return "value_not_found"

    value_to_store = trimmed_value
    field_to_set = field_name
    if field_name == "preferred_address":
        record = await (
            await tx.run(
                UPSERT_CUSTOM_ADDRESS, normalized_full=_normalize_custom_address(trimmed_value)
            )
        ).single()
        if record is None:
            return "value_not_found"
        value_to_store = to_str(record["address_id"])
        field_to_set = "preferred_address_id"

    now = datetime.now(UTC).isoformat()
    overrides = parse_overrides(person_record["overrides"])
    overrides[field_name] = _custom_override_entry(trimmed_value, reason, actor_id, now)
    await tx.run(UPDATE_OVERRIDES, person_id=person_id, overrides=json.dumps(overrides))
    await tx.run(
        UPDATE_GOLDEN_FIELD,
        person_id=person_id,
        field_name=field_to_set,
        value=value_to_store,
    )
    await tx.run(CREATE_OVERRIDE_AUDIT, person_id=person_id, actor_id=actor_id, reason=reason)
    return "ok"


async def _batch_override_tx(
    tx: AsyncManagedTransaction,
    person_id: str,
    items: list[tuple[str, str]],
    reason: str,
    actor_id: str,
) -> BatchOverrideResult:
    person_record = await (await tx.run(GET_PERSON_OVERRIDES_FULL, person_id=person_id)).single()
    if person_record is None:
        return BatchOverrideResult(outcome="person_not_found")

    # Validate (and re-derive) every item before writing anything — keeps the transaction atomic.
    validated: list[tuple[str, str, DerivedValue]] = []
    for field_name, source_record_pk in items:
        if field_name not in GOLDEN_FIELD_SPEC:
            return BatchOverrideResult(outcome="invalid_field", failed_field=field_name)
        linked = await (
            await tx.run(
                CHECK_SOURCE_RECORD_LINKED,
                source_record_pk=source_record_pk,
                person_id=person_id,
            )
        ).single()
        if linked is None:
            return BatchOverrideResult(outcome="sr_not_found", failed_field=field_name)

        derived = await derive_override_value(tx, person_id, field_name, source_record_pk)
        if derived is None or derived.value is None:
            return BatchOverrideResult(outcome="value_not_found", failed_field=field_name)
        validated.append((field_name, source_record_pk, derived))

    now = datetime.now(UTC).isoformat()
    overrides = parse_overrides(person_record["overrides"])
    for field_name, source_record_pk, _ in validated:
        overrides[field_name] = _override_entry(field_name, source_record_pk, reason, actor_id, now)
    await tx.run(UPDATE_OVERRIDES, person_id=person_id, overrides=json.dumps(overrides))

    updates = [
        {"field_name": derived.field_to_set, "value": derived.value} for _, _, derived in validated
    ]
    if updates:
        await tx.run(
            UPDATE_GOLDEN_FIELDS,
            person_id=person_id,
            updates=updates,
            invalidate_analysis=True,
        )

    await tx.run(CREATE_OVERRIDE_AUDIT, person_id=person_id, actor_id=actor_id, reason=reason)
    return BatchOverrideResult(outcome="ok")
