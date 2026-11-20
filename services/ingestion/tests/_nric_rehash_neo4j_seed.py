"""Generated NRIC fixtures and basic source-evidence seeders for Neo4j tests."""

from __future__ import annotations

import secrets
import string
import uuid
from dataclasses import dataclass, field

from neo4j import Driver
from src.normalizers.nric import encrypt_nric, mask_nric, nric_checksum_valid, nric_digest
from src.nric_rehash_state import key_check_for

_VERSION = "integration-key-version"


@dataclass(frozen=True)
class _Dataset:
    key: str = field(repr=False)
    key_version: str
    identifier_ids: tuple[str, ...]
    person_ids: tuple[str, ...]
    source_record_pks: tuple[str, ...]
    assertion_ids: tuple[str, ...]
    link_ids: tuple[str, ...]
    raw_values: tuple[str, ...] = field(repr=False)
    canonical_values: tuple[str, ...] = field(repr=False)
    masked_value: str = field(repr=False)
    existing_digest: str = field(repr=False)
    existing_mask: str = field(repr=False)
    existing_ciphertext: str = field(repr=False)


def _valid_canonical(excluded: set[str]) -> str:
    while True:
        digits = f"{secrets.randbelow(10_000_000):07d}"
        if len(set(digits)) == 1:
            continue
        for letter in string.ascii_uppercase:
            candidate = f"S{digits}{letter}"
            if candidate not in excluded and nric_checksum_valid(candidate):
                return candidate


def _invalid_checksum(canonical: str) -> str:
    for letter in string.ascii_uppercase:
        candidate = f"{canonical[:-1]}{letter}"
        if candidate != canonical and not nric_checksum_valid(candidate):
            return candidate
    raise AssertionError("could not synthesize checksum-invalid fixture")


def _dataset(key: str, *, include_skipped: bool) -> _Dataset:
    canonicals: list[str] = []
    while len(canonicals) < (6 if include_skipped else 4):
        candidate = _valid_canonical(set(canonicals))
        canonicals.append(candidate)
    raw_values = [
        f"xxx{canonicals[0]}",
        canonicals[1].lower(),
        _invalid_checksum(canonicals[2]),
        canonicals[3],
    ]
    if include_skipped:
        raw_values.append(mask_nric(canonicals[4]))
        raw_values.append(nric_digest(canonicals[5], key))
    prefixes = ("a", "b", "c", "d", "y", "z")
    identifier_ids = tuple(f"{prefix}-{uuid.uuid4().hex}" for prefix in prefixes[: len(raw_values)])
    return _Dataset(
        key=key,
        key_version=_VERSION,
        identifier_ids=identifier_ids,
        person_ids=tuple(uuid.uuid4().hex for _ in raw_values),
        source_record_pks=tuple(uuid.uuid4().hex for _ in raw_values),
        assertion_ids=tuple(uuid.uuid4().hex for _ in raw_values),
        link_ids=tuple(uuid.uuid4().hex for _ in raw_values),
        raw_values=tuple(raw_values),
        canonical_values=tuple(canonicals),
        masked_value=mask_nric(canonicals[4]) if include_skipped else "",
        existing_digest=nric_digest(canonicals[5], key) if include_skipped else "",
        existing_mask=mask_nric(canonicals[5]) if include_skipped else "",
        existing_ciphertext=encrypt_nric(canonicals[5], key) if include_skipped else "",
    )


def _seed_identity(
    driver: Driver,
    dataset: _Dataset,
    index: int,
    *,
    checksum_valid: bool | None = None,
    masked: str | None = None,
    digest_metadata: bool = False,
) -> None:
    with driver.session() as session:
        session.run(
            "MERGE (system:SourceSystem {source_key: 'nric-rehash-test'}) "
            "SET system.field_trust = '{}' "
            "CREATE (person:Person {person_id: $person_id, status: 'active'}) "
            "CREATE (record:SourceRecord {source_record_pk: $source_record_pk, "
            "source_record_id: $source_record_pk, source_instance_id: 'test', "
            "record_type: 'identity'})-[:FROM_SOURCE]->(system) "
            "CREATE (identifier:Identifier {identifier_id: $identifier_id, "
            "identifier_type: 'nric', identifier_scope: 'global', "
            "normalized_value: $normalized_value, hashed_value: $hashed_value, "
            "nric_masked: $nric_masked, checksum_valid: $checksum_valid, "
            "nric_key_version: $key_version, nric_encrypted: $nric_encrypted}) "
            "CREATE (person)-[:IDENTIFIED_BY {source_system_key: 'nric-rehash-test', "
            "source_record_pk: $source_record_pk, is_active: true, is_verified: true, "
            "quality_flag: 'valid'}]->(identifier) "
            "CREATE (record)-[:ASSERTS {assertion_id: $assertion_id, "
            "field_path: 'identifiers', role: 'subject', quality_flag: 'valid', "
            "is_verified: true}]->(identifier) "
            "CREATE (person)-[:HAS_IDENTIFIER {link_id: $link_id, "
            "assertion_count: 1, source_count: 1, verified_count: 1, "
            "basis: 'ASSERTS', status: 'active'}]->(identifier) "
            "CREATE (person)-[:ANCHORED_BY]->(identifier)",
            person_id=dataset.person_ids[index],
            source_record_pk=dataset.source_record_pks[index],
            identifier_id=dataset.identifier_ids[index],
            normalized_value=dataset.raw_values[index],
            hashed_value=dataset.raw_values[index] if digest_metadata else None,
            nric_masked=masked,
            checksum_valid=checksum_valid,
            key_version=dataset.key_version if digest_metadata else None,
            nric_encrypted=dataset.existing_ciphertext if digest_metadata else None,
            assertion_id=dataset.assertion_ids[index],
            link_id=dataset.link_ids[index],
        ).consume()


def _seed_dataset(driver: Driver, dataset: _Dataset, *, include_skipped: bool) -> None:
    for index in range(4):
        _seed_identity(driver, dataset, index)
    if include_skipped:
        _seed_identity(driver, dataset, 4, masked=dataset.masked_value)
        _seed_identity(
            driver,
            dataset,
            5,
            checksum_valid=nric_checksum_valid(dataset.canonical_values[5]),
            masked=dataset.existing_mask,
            digest_metadata=True,
        )


def _seed_revision_counter(driver: Driver, value: int) -> None:
    with driver.session() as session:
        session.run(
            "MERGE (counter:IdentityLinkRevisionCounter {stream_key: $stream_key}) "
            "SET counter.current_revision = $revision",
            stream_key="identity_link_revision_stream_v1",
            revision=value,
        ).consume()


def _seed_migration(
    driver: Driver,
    dataset: _Dataset,
    *,
    owner: str,
    lease_seconds: int,
    baseline_total: int,
    baseline_non_digest: int,
    baseline_digest_shaped: int,
    baseline_revision: int | None,
) -> None:
    with driver.session() as session:
        session.run(
            "CREATE (:DataMigration {migration_key: $migration_key, phase: 'convert', "
            "key_version: $key_version, key_check: $key_check, cursor: null, "
            "baseline_total: $baseline_total, baseline_non_digest: $baseline_non_digest, "
            "baseline_digest_shaped: $baseline_digest_shaped, "
            "baseline_revision: $baseline_revision, "
            "batches: 0, converted_in_place: 0, converted_consolidated: 0, removed_nodes: 0, "
            "lease_owner: $owner, lease_until: datetime() + duration({seconds: $lease_seconds}), "
            "lock_version: 0, created_at: datetime(), updated_at: datetime()})",
            migration_key="nric_hmac_rehash_v1",
            key_version=dataset.key_version,
            key_check=key_check_for(dataset.key),
            baseline_total=baseline_total,
            baseline_non_digest=baseline_non_digest,
            baseline_digest_shaped=baseline_digest_shaped,
            baseline_revision=baseline_revision,
            owner=owner,
            lease_seconds=lease_seconds,
        ).consume()


def _seed_identifier_only(
    driver: Driver,
    identifier_id: str,
    value: str,
    *,
    scope: str | None = "global",
    hashed_value: str | None = None,
    nric_masked: str | None = None,
    checksum_valid: bool | None = None,
    key_version: str | None = None,
    encrypted: str | None = None,
) -> None:
    with driver.session() as session:
        session.run(
            "CREATE (:Identifier {identifier_id: $identifier_id, identifier_type: 'nric', "
            "identifier_scope: $scope, normalized_value: $value, hashed_value: $hashed_value, "
            "nric_masked: $nric_masked, checksum_valid: $checksum_valid, "
            "nric_key_version: $key_version, nric_encrypted: $encrypted})",
            identifier_id=identifier_id,
            scope=scope,
            value=value,
            hashed_value=hashed_value,
            nric_masked=nric_masked,
            checksum_valid=checksum_valid,
            key_version=key_version,
            encrypted=encrypted,
        ).consume()
