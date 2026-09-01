"""Typed active-authority golden-profile recomputation for CRM repair verification."""

from __future__ import annotations

import json
from collections.abc import Mapping

from neo4j import ManagedTransaction

from src.golden_profile_authority_models import (
    AuthorityAddress,
    AuthorityFact,
    AuthorityIdentifier,
    GoldenFieldKey,
    GoldenProfileFields,
    GoldenProfileRecomputeResult,
    OverrideEntry,
)
from src.graph import queries
from src.graph.queries.persons import (
    FETCH_ACTIVE_PERSON_AUTHORITY_WITH_OVERRIDES,
    FETCH_ADDRESS_IDS_BY_NORMALIZED_FULL,
    MARK_PERSON_ANALYSIS_DIRTY_BY_ID,
)

# Trust tier ordering (higher index = higher trust).
_TRUST_TIER_RANK: dict[str, int] = {
    "tier_1": 4,  # highest trust
    "tier_2": 3,
    "tier_3": 2,
    "tier_4": 1,  # lowest trust
}

# Golden profile version — bump when survivorship logic changes.
_GOLDEN_PROFILE_VERSION = "v0.1.0"


_GOLDEN_OVERRIDE_FIELDS: dict[str, tuple[str, str | None, GoldenFieldKey]] = {
    "preferred_full_name": ("fact", "full_name", "preferred_full_name"),
    "preferred_dob": ("fact", "dob", "preferred_dob"),
    "preferred_race_ethnicity": ("fact", "race_ethnicity", "preferred_race_ethnicity"),
    "preferred_phone": ("identifier", "phone", "preferred_phone"),
    "preferred_email": ("identifier", "email", "preferred_email"),
    "preferred_nric": ("identifier", "nric", "preferred_nric"),
    "preferred_address": ("address", None, "preferred_address_id"),
}
_INVALID_QUALITY_FLAGS = frozenset({"invalid_format", "placeholder_value"})


def recompute_golden_profile_from_active_authority(
    tx: ManagedTransaction,
    person_id: str,
    *,
    invalidate_analysis: bool,
) -> GoldenProfileRecomputeResult | None:
    """Rebuild derived Person fields from active authority while preserving override JSON."""
    result = derive_golden_profile_from_active_authority(tx, person_id)
    if result is None:
        return None
    if result.changed:
        tx.run(queries.UPDATE_GOLDEN_PROFILE, person_id=person_id, **result.profile).consume()
    if invalidate_analysis and result.changed:
        tx.run(MARK_PERSON_ANALYSIS_DIRTY_BY_ID, person_id=person_id).consume()
    return result


def derive_golden_profile_from_active_authority(
    tx: ManagedTransaction, person_id: str
) -> GoldenProfileRecomputeResult | None:
    """Derive the desired active-authority profile without writing any state."""
    row = tx.run(FETCH_ACTIVE_PERSON_AUTHORITY_WITH_OVERRIDES, person_id=person_id).single()
    if row is None:
        return None
    person = _person_mapping(row["person"])
    facts = _authority_facts(row["facts"])
    identifiers = _authority_identifiers(row["identifiers"])
    addresses = _authority_addresses(row["addresses"])
    desired = _derived_fields(facts, identifiers, addresses)
    overrides = _overrides(person["survivorship_overrides"])
    custom_addresses = _custom_address_ids(tx, overrides)
    conflicts = _apply_canonical_overrides(
        desired, overrides, facts, identifiers, addresses, custom_addresses
    )
    current = _current_fields(person)
    changed = desired != current
    return GoldenProfileRecomputeResult(
        person_id, changed, tuple(sorted(conflicts)), desired, current
    )


def _person_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("active authority Person row is malformed")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _authority_facts(value: object) -> tuple[AuthorityFact, ...]:
    values: list[AuthorityFact] = []
    for item in _mapping_list(value, "facts"):
        attribute_name = _required_string(item, "attribute_name")
        attribute_value = _required_string(item, "attribute_value")
        quality = _optional_string(item, "quality_flag")
        if quality in _INVALID_QUALITY_FLAGS:
            continue
        values.append(
            {
                "attribute_name": attribute_name,
                "attribute_value": attribute_value,
                "source_trust_tier": _optional_string(item, "source_trust_tier"),
                "observed_at": _optional_string(item, "observed_at"),
                "quality_flag": quality,
                "source_record_pk": _optional_string(item, "source_record_pk"),
            }
        )
    return tuple(values)


def _authority_identifiers(value: object) -> tuple[AuthorityIdentifier, ...]:
    values: list[AuthorityIdentifier] = []
    for item in _mapping_list(value, "identifiers"):
        verified = item.get("is_verified")
        if verified is not None and not isinstance(verified, bool):
            raise ValueError("active authority identifier verification is malformed")
        values.append(
            {
                "identifier_type": _required_string(item, "identifier_type"),
                "normalized_value": _required_string(item, "normalized_value"),
                "is_verified": verified is True,
                "last_confirmed_at": _optional_string(item, "last_confirmed_at"),
                "source_record_pk": _optional_string(item, "source_record_pk"),
            }
        )
    return tuple(values)


def _authority_addresses(value: object) -> tuple[AuthorityAddress, ...]:
    values: list[AuthorityAddress] = []
    for item in _mapping_list(value, "addresses"):
        verified = item.get("is_verified")
        if verified is not None and not isinstance(verified, bool):
            raise ValueError("active authority address verification is malformed")
        values.append(
            {
                "address_id": _required_string(item, "address_id"),
                "normalized_full": _optional_string(item, "normalized_full"),
                "is_verified": verified is True,
                "last_confirmed_at": _optional_string(item, "last_confirmed_at"),
                "source_record_pk": _optional_string(item, "source_record_pk"),
            }
        )
    return tuple(values)


def _mapping_list(value: object, label: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError("active authority " + label + " row is malformed")
    records: list[Mapping[str, object]] = []
    for item in value:
        if item is None:
            continue
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            raise ValueError("active authority " + label + " item is malformed")
        records.append(
            {key: item_value for key, item_value in item.items() if isinstance(key, str)}
        )
    return tuple(records)


def _required_string(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("active authority string is malformed")
    return value


def _optional_string(values: Mapping[str, object], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("active authority optional string is malformed")
    return value


def _derived_fields(
    facts: tuple[AuthorityFact, ...],
    identifiers: tuple[AuthorityIdentifier, ...],
    addresses: tuple[AuthorityAddress, ...],
) -> GoldenProfileFields:
    full_name = _best_fact(facts, ("full_name", "preferred_name", "legal_name"))
    dob = _best_fact(facts, ("dob",))
    phone = _best_identifier(identifiers, "phone")
    email = _best_identifier(identifiers, "email")
    address_id = _best_address(addresses)
    return {
        "preferred_full_name": full_name,
        "preferred_dob": dob,
        "preferred_phone": phone,
        "preferred_email": email,
        "preferred_address_id": address_id,
        "preferred_nric": _best_identifier(identifiers, "nric"),
        "preferred_race_ethnicity": _best_fact(facts, ("race_ethnicity",)),
        "profile_completeness_score": round(
            sum(value is not None for value in (full_name, dob, phone, email, address_id)) / 5,
            2,
        ),
        "golden_profile_version": _GOLDEN_PROFILE_VERSION,
    }


def _best_fact(facts: tuple[AuthorityFact, ...], names: tuple[str, ...]) -> str | None:
    matching = [item for item in facts if item["attribute_name"] in names]
    if not matching:
        return None
    return max(
        matching,
        key=lambda item: (
            item["quality_flag"] == "valid",
            _TRUST_TIER_RANK.get(item["source_trust_tier"] or "", 0),
            item["observed_at"] or "",
        ),
    )["attribute_value"]


def _best_identifier(
    identifiers: tuple[AuthorityIdentifier, ...], identifier_type: str
) -> str | None:
    matching = [item for item in identifiers if item["identifier_type"] == identifier_type]
    if not matching:
        return None
    return max(matching, key=lambda item: (item["is_verified"], item["last_confirmed_at"] or ""))[
        "normalized_value"
    ]


def _best_address(addresses: tuple[AuthorityAddress, ...]) -> str | None:
    if not addresses:
        return None
    return max(addresses, key=lambda item: (item["is_verified"], item["last_confirmed_at"] or ""))[
        "address_id"
    ]


def _overrides(raw: object) -> tuple[OverrideEntry, ...]:
    if raw is None or raw == "":
        return ()
    value: object = raw
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return (OverrideEntry("survivorship_overrides", None, None, True, False),)
    if not isinstance(value, dict):
        return (OverrideEntry("survivorship_overrides", None, None, True, False),)
    entries: list[OverrideEntry] = []
    for field, item in sorted(value.items()):
        if not isinstance(field, str) or not field:
            raise ValueError("stored override field is malformed")
        if not isinstance(item, dict):
            entries.append(
                OverrideEntry(field, None, None, True, field not in _GOLDEN_OVERRIDE_FIELDS)
            )
            continue
        custom = item.get("custom_value")
        source = item.get("source_record_pk")
        malformed = (custom is not None and not isinstance(custom, str)) or (
            source is not None and not isinstance(source, str)
        )
        entries.append(
            OverrideEntry(
                field,
                custom if isinstance(custom, str) and custom else None,
                source if isinstance(source, str) and source else None,
                malformed,
                field not in _GOLDEN_OVERRIDE_FIELDS,
            )
        )
    return tuple(entries)


def _custom_address_ids(
    tx: ManagedTransaction, overrides: tuple[OverrideEntry, ...]
) -> Mapping[str, str]:
    values: dict[str, str] = {}
    for override in overrides:
        if override.field != "preferred_address" or override.custom_value is None:
            continue
        row = tx.run(
            FETCH_ADDRESS_IDS_BY_NORMALIZED_FULL,
            normalized_full=_normalize_address(override.custom_value),
        ).single()
        if row is None:
            continue
        raw_ids = row["address_ids"]
        if not isinstance(raw_ids, list) or len(raw_ids) != 1 or not isinstance(raw_ids[0], str):
            continue
        values[override.custom_value] = raw_ids[0]
    return values


def _apply_canonical_overrides(
    fields: GoldenProfileFields,
    overrides: tuple[OverrideEntry, ...],
    facts: tuple[AuthorityFact, ...],
    identifiers: tuple[AuthorityIdentifier, ...],
    addresses: tuple[AuthorityAddress, ...],
    custom_addresses: Mapping[str, str],
) -> set[str]:
    conflicts: set[str] = set()
    for override in overrides:
        spec = _GOLDEN_OVERRIDE_FIELDS.get(override.field)
        if spec is None or override.malformed:
            conflicts.add(override.field)
            continue
        kind, evidence_key, field_to_set = spec
        if override.custom_value is not None:
            if kind == "address":
                matching = [
                    item
                    for item in addresses
                    if item["normalized_full"] == _normalize_address(override.custom_value)
                ]
                if matching:
                    fields[field_to_set] = matching[0]["address_id"]
                elif override.custom_value in custom_addresses:
                    fields[field_to_set] = custom_addresses[override.custom_value]
                else:
                    conflicts.add(override.field)
            else:
                fields[field_to_set] = override.custom_value
            continue
        if override.source_record_pk is None:
            conflicts.add(override.field)
            continue
        value = _source_backed_value(
            kind, evidence_key, override.source_record_pk, facts, identifiers, addresses
        )
        if value is None:
            conflicts.add(override.field)
        else:
            fields[field_to_set] = value
    return conflicts


def _source_backed_value(
    kind: str,
    evidence_key: str | None,
    source_record_pk: str,
    facts: tuple[AuthorityFact, ...],
    identifiers: tuple[AuthorityIdentifier, ...],
    addresses: tuple[AuthorityAddress, ...],
) -> str | None:
    if kind == "fact":
        return next(
            (
                item["attribute_value"]
                for item in facts
                if item["attribute_name"] == evidence_key
                and item["source_record_pk"] == source_record_pk
            ),
            None,
        )
    if kind == "identifier":
        return next(
            (
                item["normalized_value"]
                for item in identifiers
                if item["identifier_type"] == evidence_key
                and item["source_record_pk"] == source_record_pk
            ),
            None,
        )
    return next(
        (item["address_id"] for item in addresses if item["source_record_pk"] == source_record_pk),
        None,
    )


def _normalize_address(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _current_fields(person: Mapping[str, object]) -> GoldenProfileFields:
    values: GoldenProfileFields = {
        "preferred_full_name": _person_optional_string(person, "preferred_full_name"),
        "preferred_dob": _person_optional_string(person, "preferred_dob"),
        "preferred_phone": _person_optional_string(person, "preferred_phone"),
        "preferred_email": _person_optional_string(person, "preferred_email"),
        "preferred_address_id": _person_optional_string(person, "preferred_address_id"),
        "preferred_nric": _person_optional_string(person, "preferred_nric"),
        "preferred_race_ethnicity": _person_optional_string(person, "preferred_race_ethnicity"),
        "profile_completeness_score": _person_float(person, "profile_completeness_score"),
        "golden_profile_version": _person_optional_string(person, "golden_profile_version") or "",
    }
    return values


def _person_optional_string(person: Mapping[str, object], key: str) -> str | None:
    value = person.get(key)
    if value is None or isinstance(value, str):
        return value
    raise ValueError("Person derived field is malformed")


def _person_float(person: Mapping[str, object], key: str) -> float:
    value = person.get(key)
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Person completeness is malformed")
    return float(value)
