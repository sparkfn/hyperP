"""Generated legacy identity graphs for NRIC consolidation integration tests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from _nric_rehash_neo4j_fixture import _Client, _counter
from _nric_rehash_neo4j_seed import _VERSION, _seed_identifier_only, _valid_canonical
from neo4j import Driver, ManagedTransaction
from src.identity_link_revisions import (
    IdentityLinkDesiredRevision,
    IdentityLinkResolutionKind,
    append_identity_link_revisions,
)
from src.identity_link_writes import recompute_participant_links
from src.identity_links import ParticipantKind
from src.normalizers.nric import (
    encrypt_nric,
    mask_nric,
    nric_checksum_valid,
    nric_digest,
)
from src.nric_rehash_state import EvidenceCounts, key_check_for, read_evidence_counts


@dataclass(frozen=True)
class LegacyWorld:
    key: str = field(repr=False)
    key_version: str
    seed_id: str = field(repr=False)
    identifier_ids: tuple[str, ...] = field(repr=False)
    canonical_values: tuple[str, ...] = field(repr=False)
    person_ids: tuple[str, ...] = field(repr=False)
    profile_id: str = field(repr=False)
    complete_ciphertext: str = field(repr=False)

    @property
    def complete_survivor_id(self) -> str:
        return self.identifier_ids[2]

    @property
    def sibling_survivor_id(self) -> str:
        return self.identifier_ids[3]

    @property
    def gap_survivor_id(self) -> str:
        return self.identifier_ids[6]


def seed_legacy_world(
    driver: Driver,
    key: str,
    *,
    world: LegacyWorld | None = None,
) -> LegacyWorld:
    """Create or reproduce three generated NRIC groups and legacy evidence."""
    fixture = world if world is not None else _new_legacy_world(key)
    if fixture.key != key:
        raise AssertionError("legacy fixture key did not match")
    _seed_world(driver, fixture)
    return fixture


def _new_legacy_world(key: str) -> LegacyWorld:
    canonical_values: list[str] = []
    while len(canonical_values) < 3:
        canonical_values.append(_valid_canonical(set(canonical_values)))
    seed_id = uuid.uuid4().hex
    ids = tuple(
        f"{letter}-{_stable_id(seed_id, f'identifier:{index}')}"
        for index, letter in enumerate("abcdefg")
    )
    return LegacyWorld(
        key=key,
        key_version=_VERSION,
        seed_id=seed_id,
        identifier_ids=ids,
        canonical_values=tuple(canonical_values),
        person_ids=(
            _stable_id(seed_id, "person:0"),
            _stable_id(seed_id, "person:1"),
        ),
        profile_id=_stable_id(seed_id, "profile:0"),
        complete_ciphertext=encrypt_nric(canonical_values[0], key),
    )


def _stable_id(seed_id: str, purpose: str) -> str:
    return uuid.uuid5(uuid.UUID(hex=seed_id), purpose).hex


def _seed_world(driver: Driver, world: LegacyWorld) -> None:
    values, ids = world.canonical_values, world.identifier_ids
    _seed_identifier_only(driver, ids[0], f"xxx{values[0]}")
    _seed_identifier_only(driver, ids[1], values[0].lower())
    complete_digest = nric_digest(values[0], world.key)
    _seed_identifier_only(
        driver,
        ids[2],
        complete_digest,
        hashed_value=complete_digest,
        nric_masked=mask_nric(values[0]),
        checksum_valid=nric_checksum_valid(values[0]),
        key_version=world.key_version,
        encrypted=world.complete_ciphertext,
    )
    _seed_identifier_only(driver, ids[3], values[1])
    _seed_identifier_only(driver, ids[4], values[1].lower())
    _seed_identifier_only(driver, ids[5], values[2])
    gap_digest = nric_digest(values[2], world.key)
    _seed_identifier_only(
        driver,
        ids[6],
        gap_digest,
        hashed_value=gap_digest,
        nric_masked=mask_nric(values[2]),
        checksum_valid=nric_checksum_valid(values[2]),
        key_version=world.key_version,
    )
    person_indexes: tuple[int, ...] = (0, 1, 0, 0, 1, 0, 1)
    for index, person_index in enumerate(person_indexes):
        _seed_person_evidence(driver, world, index, ids[index], world.person_ids[person_index])
        _seed_profile_evidence(driver, world, index, ids[index])
    participants: tuple[tuple[ParticipantKind, str], ...] = (
        ("person", world.person_ids[0]),
        ("person", world.person_ids[1]),
        ("profile", world.profile_id),
    )
    with driver.session() as session:
        session.execute_write(_recompute_participants, participants)


def _seed_person_evidence(
    driver: Driver,
    world: LegacyWorld,
    index: int,
    identifier_id: str,
    person_id: str,
) -> None:
    source_record_pk = _stable_id(world.seed_id, f"person-record:{index}")
    with driver.session() as session:
        session.run(
            "MERGE (system:SourceSystem {source_key: 'bitrix_chat'}) "
            "SET system.field_trust = '{}' "
            "MERGE (person:Person {person_id: $person_id}) "
            "ON CREATE SET person.status = 'active', person.golden_marker = $marker "
            "WITH system, person "
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "CREATE (record:SourceRecord {source_record_pk: $source_record_pk, "
            "source_record_id: $source_record_pk, source_instance_id: $instance_id, "
            "record_type: 'identity', lifecycle_status: 'active', link_status: 'linked', "
            "attribution_status: 'linked', extraction_confidence: 0.9, "
            "observed_at: datetime('2026-01-01T00:00:00Z')})-[:FROM_SOURCE]->(system) "
            "CREATE (record)-[:LINKED_TO {is_active: true}]->(person) "
            "CREATE (record)-[:ASSERTS {assertion_id: $assertion_one, "
            "field_path: 'identifiers', role: 'subject', quality_flag: 'valid', "
            "is_verified: true}]->(identifier) "
            "CREATE (record)-[:ASSERTS {assertion_id: $assertion_two, "
            "field_path: 'identifiers', role: 'subject', quality_flag: 'valid', "
            "is_verified: true}]->(identifier) "
            "CREATE (person)-[:IDENTIFIED_BY {source_system_key: 'bitrix_chat', "
            "source_record_pk: $source_record_pk, is_active: true, is_verified: true, "
            "quality_flag: 'valid'}]->(identifier) "
            "CREATE (person)-[:ANCHORED_BY {anchor_source: 'legacy'}]->(identifier) "
            "CREATE (event:MergeEvent {merge_event_id: $event_id}) "
            "CREATE (event)-[:MOVED_RELATIONSHIP {relationship_type: 'IDENTIFIED_BY', "
            "direction: 'out', origin_person_id: $person_id, "
            "source_record_pk: $source_record_pk}]->(identifier)",
            identifier_id=identifier_id,
            person_id=person_id,
            marker=_stable_id(world.seed_id, f"person-marker:{person_id}"),
            source_record_pk=source_record_pk,
            instance_id=_stable_id(world.seed_id, f"person-instance:{index}"),
            assertion_one=_stable_id(world.seed_id, f"person-assertion:{index}:1"),
            assertion_two=_stable_id(world.seed_id, f"person-assertion:{index}:2"),
            event_id=_stable_id(world.seed_id, f"person-event:{index}"),
        ).consume()


def _seed_profile_evidence(
    driver: Driver,
    world: LegacyWorld,
    index: int,
    identifier_id: str,
) -> None:
    source_record_pk = _stable_id(world.seed_id, f"profile-record:{index}")
    with driver.session() as session:
        session.run(
            "MERGE (system:SourceSystem {source_key: 'bitrix_chat'}) "
            "SET system.field_trust = '{}' "
            "MERGE (profile:Profile {profile_id: $profile_id}) "
            "ON CREATE SET profile.status = 'open', profile.evidence_version = 0 "
            "WITH system, profile "
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "CREATE (record:SourceRecord {source_record_pk: $source_record_pk, "
            "source_record_id: $source_record_pk, source_instance_id: $instance_id, "
            "record_type: 'identity', lifecycle_status: 'active', link_status: 'linked', "
            "extraction_confidence: 0.9, observed_at: datetime('2026-01-01T00:00:00Z')})"
            "-[:FROM_SOURCE]->(system) "
            "CREATE (record)-[:IN_PROFILE]->(profile) "
            "CREATE (record)-[:ASSERTS {assertion_id: $assertion_one, "
            "field_path: 'identifiers', role: 'subject', quality_flag: 'valid', "
            "is_verified: true}]->(identifier) "
            "CREATE (record)-[:ASSERTS {assertion_id: $assertion_two, "
            "field_path: 'identifiers', role: 'subject', quality_flag: 'valid', "
            "is_verified: true}]->(identifier)",
            identifier_id=identifier_id,
            profile_id=world.profile_id,
            source_record_pk=source_record_pk,
            instance_id=_stable_id(world.seed_id, f"profile-instance:{index}"),
            assertion_one=_stable_id(world.seed_id, f"profile-assertion:{index}:1"),
            assertion_two=_stable_id(world.seed_id, f"profile-assertion:{index}:2"),
        ).consume()


def _recompute_participants(
    tx: ManagedTransaction,
    participants: tuple[tuple[ParticipantKind, str], ...],
) -> None:
    for kind, participant_id in participants:
        recompute_participant_links(tx, kind, participant_id)


def seed_unsupported_relationship(
    driver: Driver,
    identifier_id: str,
    *,
    outgoing: bool,
) -> None:
    """Add a generated unsupported edge for fail-closed rollback tests."""
    with driver.session() as session:
        if outgoing:
            session.run(
                "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
                "CREATE (identifier)-[:UNSUPPORTED_TEST_EDGE]->"
                "(:SourceRecord {source_record_pk: $source_record_pk})",
                identifier_id=identifier_id,
                source_record_pk=uuid.uuid4().hex,
            ).consume()
        else:
            session.run(
                "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
                "CREATE (:SourceRecord {source_record_pk: $source_record_pk})"
                "-[:UNSUPPORTED_TEST_EDGE]->(identifier)",
                identifier_id=identifier_id,
                source_record_pk=uuid.uuid4().hex,
            ).consume()


def seed_identity_revisions(driver: Driver, person_id: str) -> None:
    """Append two valid revisions for one generated Bitrix identity-link head."""
    instance_id = uuid.uuid4().hex
    entity_id = uuid.uuid4().hex
    first = _revision_row(instance_id, entity_id, person_id, "baseline")
    second = _revision_row(instance_id, entity_id, person_id, "automatic_activation")
    with driver.session() as session:
        session.execute_write(append_identity_link_revisions, [first])
        session.execute_write(append_identity_link_revisions, [second])


def _revision_row(
    instance_id: str,
    entity_id: str,
    person_id: str,
    resolution_kind: IdentityLinkResolutionKind,
) -> IdentityLinkDesiredRevision:
    return IdentityLinkDesiredRevision(
        source_system="bitrix_chat",
        source_instance_id=instance_id,
        source_entity_type="deal",
        source_entity_id=entity_id,
        identity_policy_version="generated-policy-version",
        link_status="resolved",
        hyperp_person_id=person_id,
        resolution_kind=resolution_kind,
        effective_at="2026-01-01T00:00:00Z",
        cause_key=uuid.uuid4().hex,
    )


def seed_migration_baseline(driver: Driver, world: LegacyWorld) -> None:
    """Seed settled rollback state, unlike `_seed_migration`,
    which deliberately creates a live lease."""
    evidence = read_legacy_evidence_counts(driver)
    baseline_total = _counter(
        driver,
        "MATCH (identifier:Identifier {identifier_type: 'nric'}) RETURN count(identifier) AS count",
    )
    baseline_non_digest = _counter(
        driver,
        "MATCH (identifier:Identifier {identifier_type: 'nric'}) "
        "WHERE NOT coalesce(identifier.normalized_value =~ '^[0-9a-f]{64}$', false) "
        "RETURN count(identifier) AS count",
    )
    baseline_digest_shaped = _counter(
        driver,
        "MATCH (identifier:Identifier {identifier_type: 'nric'}) "
        "WHERE coalesce(identifier.normalized_value =~ '^[0-9a-f]{64}$', false) "
        "RETURN count(identifier) AS count",
    )
    with driver.session() as session:
        session.run(
            "CREATE (:DataMigration {migration_key: 'nric_hmac_rehash_v1', "
            "phase: 'convert', cursor: null, key_version: $key_version, "
            "key_check: $key_check, baseline_total: $total, "
            "baseline_non_digest: $non_digest, baseline_digest_shaped: $digest_shaped, "
            "baseline_asserts: $asserts, "
            "baseline_identified_by: $identified_by, baseline_anchored_by: $anchored_by, "
            "baseline_moved: $moved, baseline_has_identifier: $has_identifier, "
            "baseline_active_links: $active_links, baseline_revision: null, "
            "batches: 0, converted_in_place: 0, converted_consolidated: 0, removed_nodes: 0, "
            "lock_version: 0, lease_owner: null, lease_until: null, "
            "offline_acknowledged_at: datetime('2020-01-01T00:00:00Z'), "
            "created_at: datetime(), updated_at: datetime()})",
            key_version=world.key_version,
            key_check=key_check_for(world.key),
            total=baseline_total,
            non_digest=baseline_non_digest,
            digest_shaped=baseline_digest_shaped,
            asserts=evidence["asserts"],
            identified_by=evidence["identified_by"],
            anchored_by=evidence["anchored_by"],
            moved=evidence["moved"],
            has_identifier=evidence["has_identifier"],
            active_links=evidence["active_links"],
        ).consume()


def read_legacy_evidence_counts(driver: Driver) -> EvidenceCounts:
    """Read the rehash query's aggregate-only evidence count contract."""
    return _Client(driver).execute_read(read_evidence_counts)
