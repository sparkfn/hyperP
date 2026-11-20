"""Generated legacy assertion replay worlds for the disposable Neo4j shard."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field

from _nric_rehash_neo4j_seed import _VERSION, _invalid_checksum, _valid_canonical
from neo4j import Driver
from src.normalizers.nric import (
    encrypt_nric,
    mask_nric,
    nric_checksum_valid,
    nric_digest,
)


@dataclass(frozen=True)
class _ReplayDataset:
    key: str = field(repr=False)
    key_version: str
    canonicals: tuple[str, ...] = field(repr=False)
    identifier_ids: tuple[str, ...] = field(repr=False)
    source_record_pks: tuple[str, ...] = field(repr=False)


def _replay_dataset(key: str, *, canonical_count: int) -> _ReplayDataset:
    canonicals: list[str] = []
    while len(canonicals) < canonical_count:
        canonicals.append(_valid_canonical(set(canonicals)))
    return _ReplayDataset(
        key=key,
        key_version=_VERSION,
        canonicals=tuple(canonicals),
        identifier_ids=tuple(uuid.uuid4().hex for _ in range(canonical_count)),
        source_record_pks=tuple(uuid.uuid4().hex for _ in range(canonical_count)),
    )


def _historical_assertion_id(source_record_pk: str, raw_value: str) -> str:
    target_id = f"nric|global|{raw_value}"
    identity = f"{source_record_pk}|identifiers.nric|{target_id}"
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()


def _seed_replay_record(
    driver: Driver,
    dataset: _ReplayDataset,
    index: int,
    raw_value: str,
    *,
    prehashed: bool = False,
    metadata_index: int | None = None,
) -> None:
    source_record_pk = dataset.source_record_pks[index]
    _create_replay_graph(
        driver,
        source_record_pk,
        dataset.identifier_ids[index],
        raw_value,
        _legacy_payload(raw_value),
        _identifier_metadata(
            dataset, metadata_index if metadata_index is not None else index, raw_value, prehashed
        ),
    )


def _identifier_metadata(
    dataset: _ReplayDataset,
    index: int,
    raw_value: str,
    prehashed: bool,
) -> dict[str, object]:
    if not prehashed:
        return {
            "hashed_value": None,
            "nric_masked": None,
            "checksum_valid": None,
            "nric_key_version": None,
            "nric_encrypted": None,
        }
    canonical = dataset.canonicals[index]
    return {
        "hashed_value": raw_value,
        "nric_masked": mask_nric(canonical),
        "checksum_valid": nric_checksum_valid(canonical),
        "nric_key_version": dataset.key_version,
        "nric_encrypted": encrypt_nric(canonical, dataset.key),
    }


def _legacy_payload(raw_value: str) -> str:
    return json.dumps(
        {
            "identifiers": [
                {
                    "identifier_type": "nric",
                    "normalized_value": raw_value,
                    "source_instance_id": None,
                    "quality_flag": "valid",
                }
            ]
        },
        sort_keys=True,
    )


def _create_replay_graph(
    driver: Driver,
    source_record_pk: str,
    identifier_id: str,
    raw_value: str,
    payload: str,
    metadata: dict[str, object],
) -> None:
    with driver.session() as session:
        session.run(
            "MERGE (system:SourceSystem {source_key: 'nric-replay-test'}) "
            "CREATE (record:SourceRecord {source_record_pk: $source_record_pk, "
            "source_record_id: $source_record_pk, source_instance_id: 'replay', "
            "record_type: 'identity', lifecycle_status: 'active', is_latest: true, "
            "normalized_payload: $payload})-[:FROM_SOURCE]->(system) "
            "CREATE (identifier:Identifier {identifier_id: $identifier_id, "
            "identifier_type: 'nric', identifier_scope: 'global', "
            "normalized_value: $raw_value, hashed_value: $hashed_value, "
            "nric_masked: $nric_masked, checksum_valid: $checksum_valid, "
            "nric_key_version: $nric_key_version, nric_encrypted: $nric_encrypted}) "
            "CREATE (record)-[:ASSERTS {assertion_id: $assertion_id, "
            "field_path: 'identifiers.nric', role: 'subject', quality_flag: 'valid', "
            "is_verified: true}]->(identifier)",
            source_record_pk=source_record_pk,
            identifier_id=identifier_id,
            raw_value=raw_value,
            payload=payload,
            assertion_id=_historical_assertion_id(source_record_pk, raw_value),
            **metadata,
        ).consume()


def _seed_s1_world(driver: Driver, dataset: _ReplayDataset) -> None:
    _seed_replay_record(driver, dataset, 0, dataset.canonicals[0])
    _seed_replay_record(driver, dataset, 1, _invalid_checksum(dataset.canonicals[1]))


def _seed_s2_collision_world(driver: Driver, dataset: _ReplayDataset) -> str:
    canonical = dataset.canonicals[0]
    digest = nric_digest(canonical, dataset.key)
    _seed_replay_record(driver, dataset, 0, canonical)
    _seed_replay_record(driver, dataset, 1, canonical.lower())
    _seed_replay_record(driver, dataset, 2, digest, prehashed=True, metadata_index=0)
    return digest


def _assertion_snapshot(driver: Driver) -> tuple[tuple[str, str, str], ...]:
    with driver.session() as session:
        rows = session.run(
            "MATCH (record:SourceRecord)-[assertion:ASSERTS]->"
            "(identifier:Identifier {identifier_type: 'nric'}) "
            "RETURN record.source_record_pk AS source_record_pk, "
            "assertion.assertion_id AS assertion_id, "
            "identifier.normalized_value AS normalized_value "
            "ORDER BY source_record_pk, assertion_id"
        )
        snapshot: list[tuple[str, str, str]] = []
        for row in rows:
            source_record_pk: object = row.get("source_record_pk")
            assertion_id: object = row.get("assertion_id")
            normalized_value: object = row.get("normalized_value")
            if (
                not isinstance(source_record_pk, str)
                or not isinstance(assertion_id, str)
                or not isinstance(normalized_value, str)
            ):
                raise AssertionError("NRIC assertion snapshot was malformed")
            snapshot.append((source_record_pk, assertion_id, normalized_value))
    return tuple(snapshot)


def _identifier_ids(driver: Driver) -> tuple[str, ...]:
    with driver.session() as session:
        rows = session.run(
            "MATCH (identifier:Identifier {identifier_type: 'nric'}) "
            "RETURN identifier.identifier_id AS identifier_id ORDER BY identifier_id"
        )
        identifiers: list[str] = []
        for row in rows:
            identifier_id: object = row.get("identifier_id")
            if not isinstance(identifier_id, str):
                raise AssertionError("NRIC Identifier id was malformed")
            identifiers.append(identifier_id)
    return tuple(identifiers)
