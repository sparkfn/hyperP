"""Generated candidate evidence seeders for the A4 anchor integration shard."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from _nric_rehash_neo4j_seed import _invalid_checksum, _valid_canonical
from neo4j import Driver
from src.graph.bootstrap import SOURCE_NRIC_TRUST
from src.identity_registry import PROMOTION_MIN_TRUST_TIERS, source_promotes_person
from src.models import RecordType
from src.normalizers.nric import mask_nric, nric_digest

_ANCHOR_KEY_VERSION = "integration-anchor-key-version"


@dataclass(frozen=True)
class _AnchorDataset:
    key: str = field(repr=False)
    key_version: str
    canonicals: tuple[str, ...] = field(repr=False)
    identifier_ids: tuple[str, ...] = field(repr=False)
    person_ids: tuple[str, ...] = field(repr=False)
    source_record_pks: tuple[str, ...] = field(repr=False)


def _anchor_dataset(key: str, *, canonical_count: int = 10) -> _AnchorDataset:
    canonicals: list[str] = []
    while len(canonicals) < canonical_count:
        canonicals.append(_valid_canonical(set(canonicals)))
    return _AnchorDataset(
        key=key,
        key_version=_ANCHOR_KEY_VERSION,
        canonicals=tuple(canonicals),
        identifier_ids=tuple(uuid.uuid4().hex for _ in range(canonical_count + 2)),
        person_ids=tuple(uuid.uuid4().hex for _ in range(canonical_count + 3)),
        source_record_pks=tuple(uuid.uuid4().hex for _ in range(canonical_count + 8)),
    )


def _promoting_source_key() -> str:
    candidates = sorted(
        source_key
        for source_key, trust_tier in SOURCE_NRIC_TRUST.items()
        if source_promotes_person(source_key) and trust_tier in PROMOTION_MIN_TRUST_TIERS
    )
    if not candidates:
        raise AssertionError("no configured NRIC source satisfies the promotion gate")
    return candidates[0]


def _rejected_source() -> tuple[str, str]:
    for source_key in sorted(SOURCE_NRIC_TRUST):
        if not source_promotes_person(source_key):
            return source_key, "source_not_promoting"
    for source_key, trust_tier in sorted(SOURCE_NRIC_TRUST.items()):
        if trust_tier not in PROMOTION_MIN_TRUST_TIERS:
            return source_key, "below_tier_floor"
    raise AssertionError("no configured NRIC source is rejected by the promotion gate")


@dataclass(frozen=True)
class _AnchorDecisionWorld:
    dataset: _AnchorDataset
    anchor_person_id: str
    conflicting_person_id: str
    conflicting_key: str = field(repr=False)
    handoffs: tuple[tuple[str, str], ...]


def _seed_anchor_identifier(driver: Driver, identifier_id: str, raw_value: str) -> None:
    with driver.session() as session:
        session.run(
            "CREATE (:Identifier {identifier_id: $identifier_id, identifier_type: 'nric', "
            "identifier_scope: 'global', normalized_value: $raw_value})",
            identifier_id=identifier_id,
            raw_value=raw_value,
        ).consume()


def _seed_anchor_person(
    driver: Driver,
    person_id: str,
    *,
    nric_key: str | None = None,
    with_anchor_edge: bool = False,
    identifier_id: str | None = None,
) -> None:
    with driver.session() as session:
        session.run(
            "CREATE (person:Person {person_id: $person_id, status: 'active'}) "
            "SET person.nric_key = $nric_key "
            "WITH person "
            "OPTIONAL MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "FOREACH (_ IN CASE WHEN $with_anchor_edge AND identifier IS NOT NULL "
            "THEN [1] ELSE [] END | CREATE (person)-[:ANCHORED_BY]->(identifier))",
            person_id=person_id,
            nric_key=nric_key,
            with_anchor_edge=with_anchor_edge,
            identifier_id=identifier_id,
        ).consume()


def _seed_anchor_record(
    driver: Driver,
    *,
    person_id: str,
    identifier_id: str,
    source_record_pk: str,
    source_key: str,
    record_type: str,
    normalized_payload: str | None,
    link_id: str,
) -> None:
    with driver.session() as session:
        session.run(
            "MERGE (system:SourceSystem {source_key: $source_key}) "
            "SET system.field_trust = '{}' "
            "WITH system "
            "MATCH (person:Person {person_id: $person_id}) "
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "CREATE (record:SourceRecord {source_record_pk: $source_record_pk, "
            "source_record_id: $source_record_pk, source_instance_id: $source_record_pk, "
            "record_type: $record_type, normalized_payload: $normalized_payload, "
            "lifecycle_status: 'active', link_status: 'linked'})-[:FROM_SOURCE]->(system) "
            "CREATE (record)-[:LINKED_TO {is_active: true}]->(person) "
            "CREATE (record)-[:ASSERTS {assertion_id: $assertion_id, field_path: 'identifiers', "
            "role: 'subject', quality_flag: 'valid', is_verified: true}]->(identifier) "
            "MERGE (person)-[:IDENTIFIED_BY {source_system_key: $source_key, "
            "source_record_pk: $source_record_pk, is_active: true, is_verified: true, "
            "quality_flag: 'valid'}]->(identifier) "
            "MERGE (person)-[projection:HAS_IDENTIFIER {link_id: $link_id}]->(identifier) "
            "ON CREATE SET projection.assertion_count = 1, projection.source_count = 1, "
            "projection.verified_count = 1, projection.basis = 'ASSERTS', "
            "projection.status = 'active'",
            person_id=person_id,
            identifier_id=identifier_id,
            source_record_pk=source_record_pk,
            source_key=source_key,
            record_type=record_type,
            normalized_payload=normalized_payload,
            link_id=link_id,
            assertion_id=uuid.uuid5(uuid.NAMESPACE_URL, source_record_pk).hex,
        ).consume()


def _seed_anchor_decision_world(driver: Driver, key: str) -> _AnchorDecisionWorld:
    dataset = _anchor_dataset(key, canonical_count=13)
    promoting = _promoting_source_key()
    rejected, rejected_reason = _rejected_source()
    conflict_key = nric_digest(dataset.canonicals[12], key)
    _seed_single_decision_cases(driver, dataset, promoting, rejected, conflict_key)
    _seed_already_person(driver, dataset, rejected)
    _seed_multi_person_cases(
        driver,
        dataset,
        2,
        ((2, 2, _valid_raw(dataset, 2)), (3, 3, _invalid_raw(dataset, 3))),
        promoting,
    )
    _seed_contradictory_person(driver, dataset, promoting)
    _seed_shared_people(driver, dataset, promoting)
    handoffs = tuple(
        sorted(
            (
                (dataset.person_ids[2], "multi_nric"),
                (dataset.person_ids[3], "checksum_invalid"),
                (dataset.person_ids[4], "conversation_record"),
                (dataset.person_ids[5], "nric_contradiction"),
                (dataset.person_ids[6], "shared_owner"),
                (dataset.person_ids[7], "shared_owner"),
                (dataset.person_ids[8], "person_key_conflict"),
                (dataset.person_ids[10], rejected_reason),
            )
        )
    )
    return _AnchorDecisionWorld(
        dataset=dataset,
        anchor_person_id=dataset.person_ids[0],
        conflicting_person_id=dataset.person_ids[8],
        conflicting_key=conflict_key,
        handoffs=handoffs,
    )


def _seed_single_decision_cases(
    driver: Driver,
    dataset: _AnchorDataset,
    promoting: str,
    rejected: str,
    conflict_key: str,
) -> None:
    cases = ((0, 0, 0, 0), (3, 4, 4, 4), (4, 5, 5, 5), (8, 8, 10, 8), (10, 11, 13, 11))
    for person, identifier, record, canonical in cases:
        source = rejected if person == 10 else promoting
        record_type = RecordType.CONVERSATION.value if person == 4 else RecordType.IDENTITY.value
        raw = _invalid_raw(dataset, canonical) if person == 3 else _valid_raw(dataset, canonical)
        nric_key = conflict_key if person == 8 else None
        _seed_single_case(
            driver,
            dataset,
            person_index=person,
            identifier_index=identifier,
            record_index=record,
            raw_value=raw,
            source_key=source,
            record_type=record_type,
            name=f"Generated Person {person}",
            dob=f"1980-{canonical + 1:02d}-15",
            nric_key=nric_key,
        )


def _seed_already_person(
    driver: Driver,
    dataset: _AnchorDataset,
    source_key: str,
) -> None:
    person_id = dataset.person_ids[1]
    identifier_id = dataset.identifier_ids[1]
    _seed_anchor_identifier(driver, identifier_id, _valid_raw(dataset, 1))
    _seed_anchor_person(
        driver,
        person_id,
        nric_key=nric_digest(dataset.canonicals[1], dataset.key),
        with_anchor_edge=True,
        identifier_id=identifier_id,
    )
    _seed_anchor_record(
        driver,
        person_id=person_id,
        identifier_id=identifier_id,
        source_record_pk=dataset.source_record_pks[1],
        source_key=source_key,
        record_type=RecordType.IDENTITY.value,
        normalized_payload=None,
        link_id=_link_id(person_id, identifier_id),
    )


def _seed_multi_person_cases(
    driver: Driver,
    dataset: _AnchorDataset,
    person_index: int,
    cases: tuple[tuple[int, int, str], ...],
    source_key: str,
) -> None:
    person_id = dataset.person_ids[person_index]
    _seed_anchor_person(driver, person_id)
    for identifier_index, record_index, raw_value in cases:
        identifier_id = dataset.identifier_ids[identifier_index]
        _seed_anchor_identifier(driver, identifier_id, raw_value)
        _seed_anchor_record(
            driver,
            person_id=person_id,
            identifier_id=identifier_id,
            source_record_pk=dataset.source_record_pks[record_index],
            source_key=source_key,
            record_type=RecordType.IDENTITY.value,
            normalized_payload=_payload(f"Generated Person {person_index}", "1980-02-15"),
            link_id=_link_id(person_id, identifier_id),
        )


def _seed_contradictory_person(
    driver: Driver,
    dataset: _AnchorDataset,
    source_key: str,
) -> None:
    person_id = dataset.person_ids[5]
    identifier_id = dataset.identifier_ids[6]
    _seed_anchor_identifier(driver, identifier_id, _valid_raw(dataset, 6))
    _seed_anchor_person(driver, person_id)
    for record_index, dob in ((6, "1980-06-15"), (7, "1991-04-05")):
        _seed_anchor_record(
            driver,
            person_id=person_id,
            identifier_id=identifier_id,
            source_record_pk=dataset.source_record_pks[record_index],
            source_key=source_key,
            record_type=RecordType.IDENTITY.value,
            normalized_payload=_payload("Generated Person 5", dob),
            link_id=_link_id(person_id, identifier_id),
        )


def _seed_shared_people(
    driver: Driver,
    dataset: _AnchorDataset,
    source_key: str,
) -> None:
    identifier_id = dataset.identifier_ids[7]
    _seed_anchor_identifier(driver, identifier_id, _valid_raw(dataset, 7))
    for person_index, record_index in ((6, 8), (7, 9)):
        person_id = dataset.person_ids[person_index]
        _seed_anchor_person(driver, person_id)
        _seed_anchor_record(
            driver,
            person_id=person_id,
            identifier_id=identifier_id,
            source_record_pk=dataset.source_record_pks[record_index],
            source_key=source_key,
            record_type=RecordType.IDENTITY.value,
            normalized_payload=_payload(f"Generated Person {person_index}", "1980-07-15"),
            link_id=_link_id(person_id, identifier_id),
        )


def _seed_single_case(
    driver: Driver,
    dataset: _AnchorDataset,
    *,
    person_index: int,
    identifier_index: int,
    record_index: int,
    raw_value: str,
    source_key: str,
    record_type: str,
    name: str,
    dob: str,
    nric_key: str | None = None,
) -> None:
    person_id = dataset.person_ids[person_index]
    identifier_id = dataset.identifier_ids[identifier_index]
    source_record_pk = dataset.source_record_pks[record_index]
    with driver.session() as session:
        session.run(
            "MERGE (system:SourceSystem {source_key: $source_key}) "
            "SET system.field_trust = '{}' "
            "CREATE (person:Person {person_id: $person_id, status: 'active'}) "
            "SET person.nric_key = $nric_key "
            "CREATE (identifier:Identifier {identifier_id: $identifier_id, "
            "identifier_type: 'nric', identifier_scope: 'global', normalized_value: $raw_value}) "
            "CREATE (record:SourceRecord {source_record_pk: $source_record_pk, "
            "source_record_id: $source_record_pk, source_instance_id: $source_record_pk, "
            "record_type: $record_type, normalized_payload: $payload, lifecycle_status: 'active', "
            "link_status: 'linked'})-[:FROM_SOURCE]->(system) "
            "CREATE (record)-[:LINKED_TO {is_active: true}]->(person) "
            "CREATE (record)-[:ASSERTS {assertion_id: $assertion_id}]->(identifier) "
            "CREATE (person)-[:IDENTIFIED_BY {source_system_key: $source_key, "
            "source_record_pk: $source_record_pk, is_active: true, is_verified: true, "
            "quality_flag: 'valid'}]->(identifier) "
            "CREATE (person)-[:HAS_IDENTIFIER {link_id: $link_id, "
            "assertion_count: 1, source_count: 1, verified_count: 1, basis: 'ASSERTS', "
            "status: 'active'}]->(identifier)",
            source_key=source_key,
            person_id=person_id,
            nric_key=nric_key,
            identifier_id=identifier_id,
            raw_value=raw_value,
            source_record_pk=source_record_pk,
            record_type=record_type,
            payload=_payload(name, dob),
            assertion_id=uuid.uuid5(uuid.NAMESPACE_URL, source_record_pk).hex,
            link_id=_link_id(person_id, identifier_id),
        ).consume()


def _link_id(person_id: str, identifier_id: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{person_id}:{identifier_id}").hex


def _payload(name: str | None, dob: str | None) -> str | None:
    attributes = [
        {
            "attribute_name": field_name,
            "attribute_value": value,
            "quality_flag": "valid",
        }
        for field_name, value in (("full_name", name), ("dob", dob))
        if value is not None
    ]
    return None if not attributes else json.dumps({"attributes": attributes}, sort_keys=True)


def _valid_raw(dataset: _AnchorDataset, index: int) -> str:
    return dataset.canonicals[index]


def _invalid_raw(dataset: _AnchorDataset, index: int) -> str:
    return _invalid_checksum(dataset.canonicals[index])


def _masked_raw(dataset: _AnchorDataset, index: int) -> str:
    return mask_nric(dataset.canonicals[index])
