"""Disposable Neo4j 5.26 integration coverage for NRIC rehash apply."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import string
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from cryptography.fernet import InvalidToken
from neo4j import Driver, GraphDatabase, ManagedTransaction, Record
from src.graph.client import Neo4jClient
from src.graph.schema_init import apply_deferred_identifier_scope_constraints, apply_schema
from src.normalizers.nric import (
    _fernet_cipher,
    encrypt_nric,
    mask_nric,
    normalize_nric,
    nric_checksum_valid,
    nric_digest,
)
from src.nric_rehash_apply import run_nric_rehash_apply
from src.nric_rehash_plan import NricRehashError
from src.nric_rehash_state import key_check_for

_ENV_PREFIX = "HYPERP_NEO4J_CONTROL_MIGRATION_TEST"
_VERSION = "integration-key-version"
_TestT = TypeVar("_TestT")


@dataclass(frozen=True)
class _Configuration:
    uri: str
    user: str
    password: str
    service_host: str


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


def _configuration() -> _Configuration | None:
    values = {
        "uri": os.getenv(f"{_ENV_PREFIX}_URI"),
        "user": os.getenv(f"{_ENV_PREFIX}_USER"),
        "password": os.getenv(f"{_ENV_PREFIX}_PASSWORD"),
        "service_host": os.getenv(f"{_ENV_PREFIX}_SERVICE_HOST"),
    }
    if all(value is None for value in values.values()):
        return None
    if any(value is None or not value.strip() for value in values.values()):
        pytest.fail("NRIC rehash integration requires complete disposable Neo4j settings")
    return _Configuration(
        uri=cast(str, values["uri"]),
        user=cast(str, values["user"]),
        password=cast(str, values["password"]),
        service_host=cast(str, values["service_host"]),
    )


def _validate_target(configuration: _Configuration) -> None:
    parsed = urlparse(configuration.uri)
    allowed_hosts = {"localhost", "127.0.0.1", "::1", configuration.service_host}
    if parsed.scheme != "bolt" or parsed.hostname not in allowed_hosts:
        pytest.fail("NRIC rehash integration requires an explicitly disposable Bolt target")
    if parsed.username is not None or parsed.password is not None:
        pytest.fail("NRIC rehash integration URI must not embed credentials")


def _require_neo4j_526(driver: Driver) -> None:
    with driver.session() as session:
        row = session.run(
            "CALL dbms.components() YIELD versions RETURN versions[0] AS version"
        ).single(strict=True)
    version = row["version"]
    if not isinstance(version, str) or version.split(".")[:2] != ["5", "26"]:
        pytest.fail("NRIC rehash integration requires Neo4j 5.26")


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    configuration = _configuration()
    if configuration is None:
        pytest.skip("disposable Neo4j control migration database is not configured")
    _validate_target(configuration)
    driver = GraphDatabase.driver(
        configuration.uri,
        auth=(configuration.user, configuration.password),
    )
    cleanup_owned = False
    try:
        driver.verify_connectivity()
        _require_neo4j_526(driver)
        with driver.session() as session:
            nodes = session.run(
                "MATCH (node) WHERE NOT node:PersonListSummaryCounter RETURN count(node) AS count"
            ).single(strict=True)["count"]
        if nodes != 0:
            pytest.fail("NRIC rehash integration database must be empty")
        cleanup_owned = True
        client = _Client(driver)
        apply_schema(client)
        apply_deferred_identifier_scope_constraints(client)
        yield driver
    finally:
        try:
            if cleanup_owned:
                with driver.session() as session:
                    session.run(
                        "MATCH (node) WHERE NOT node:PersonListSummaryCounter DETACH DELETE node"
                    ).consume()
        finally:
            driver.close()


class _Client(Neo4jClient):
    def __init__(self, driver: Driver) -> None:
        self._driver = driver
        self._bounded_timeout_seconds = None


class _StealingClient(_Client):
    def __init__(self, driver: Driver, replacement_owner: str) -> None:
        super().__init__(driver)
        self.write_count = 0
        self.replacement_owner = replacement_owner

    def execute_write(self, work: Callable[[ManagedTransaction], _TestT]) -> _TestT:
        result = super().execute_write(work)
        self.write_count += 1
        if self.write_count == 2:
            with self._driver.session() as session:
                session.run(
                    "MATCH (migration:DataMigration {migration_key: $migration_key}) "
                    "SET migration.lock_version = migration.lock_version + 1, "
                    "migration.lease_owner = $owner, "
                    "migration.lease_until = datetime() + duration({seconds: 300})",
                    migration_key="nric_hmac_rehash_v1",
                    owner=self.replacement_owner,
                ).consume()
        return result


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
        _seed_identity(
            driver,
            dataset,
            4,
            masked=dataset.masked_value,
        )
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


def _database_name(driver: Driver) -> str:
    with driver.session() as session:
        row = session.run("CALL db.info() YIELD name RETURN name").single(strict=True)
    name = row["name"]
    if not isinstance(name, str):
        raise AssertionError("database name result was malformed")
    return name


def _identifier_properties(driver: Driver, identifier_id: str) -> dict[str, object]:
    with driver.session() as session:
        row = session.run(
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "RETURN properties(identifier) AS properties",
            identifier_id=identifier_id,
        ).single(strict=True)
    properties = row["properties"]
    if not isinstance(properties, dict):
        raise AssertionError("Identifier properties were malformed")
    return dict(properties)


def _relationship_snapshot(driver: Driver, identifier_id: str) -> tuple[str, ...]:
    with driver.session() as session:
        row = session.run(
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "OPTIONAL MATCH (identifier)-[rel]-(other) "
            "WITH identifier, [item IN collect(CASE WHEN rel IS NULL THEN null ELSE {"
            "type: type(rel), identifier_is_start: startNode(rel) = identifier, "
            "other_labels: labels(other), other_properties: properties(other), "
            "properties: properties(rel)} END) WHERE item IS NOT NULL] AS incident "
            "RETURN incident",
            identifier_id=identifier_id,
        ).single(strict=True)
    incident = row["incident"]
    if not isinstance(incident, list):
        raise AssertionError("incident relationship inventory was malformed")
    serialized = [json.dumps(dict(item), sort_keys=True, default=str) for item in incident]
    return tuple(sorted(serialized))


def _safe_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        pytest.fail(f"{label} did not match")


def _graph_fingerprint(
    driver: Driver,
) -> tuple[tuple[tuple[str, int], ...], tuple[tuple[str, int], ...], str]:
    with driver.session() as session:
        node_counts = tuple(
            sorted(
                (str(row["label"]), int(row["count"]))
                for row in session.run(
                    "MATCH (node) UNWIND labels(node) AS label RETURN label, count(*) AS count"
                )
            )
        )
        relationship_counts = tuple(
            sorted(
                (str(row["type"]), int(row["count"]))
                for row in session.run(
                    "MATCH ()-[rel]->() RETURN type(rel) AS type, count(rel) AS count"
                )
            )
        )
        nodes = [
            {"labels": list(row["labels"]), "properties": dict(row["properties"])}
            for row in session.run(
                "MATCH (node) RETURN labels(node) AS labels, properties(node) AS properties"
            )
        ]
        relationships = [
            {
                "type": row["type"],
                "properties": dict(row["properties"]),
                "start_labels": list(row["start_labels"]),
                "start_properties": dict(row["start_properties"]),
                "end_labels": list(row["end_labels"]),
                "end_properties": dict(row["end_properties"]),
            }
            for row in session.run(
                "MATCH (start)-[rel]->(end) RETURN type(rel) AS type, "
                "properties(rel) AS properties, labels(start) AS start_labels, "
                "properties(start) AS start_properties, labels(end) AS end_labels, "
                "properties(end) AS end_properties"
            )
        ]
    nodes.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
    relationships.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
    payload = json.dumps(
        {"nodes": nodes, "relationships": relationships}, sort_keys=True, default=str
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return node_counts, relationship_counts, digest


def _counter(driver: Driver, query: str, **parameters: object) -> int:
    with driver.session() as session:
        row: Record = session.run(query, **parameters).single(strict=True)
    value: object = row["count"]
    if not isinstance(value, int) or isinstance(value, bool):
        raise AssertionError("count result was malformed")
    return value


def _clear_owned_graph(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (node) WHERE NOT node:PersonListSummaryCounter DETACH DELETE node"
        ).consume()


def _apply(
    driver: Driver,
    dataset: _Dataset,
    *,
    batch_size: int = 2,
    expected_database: str | None = None,
    client: Neo4jClient | None = None,
) -> tuple[dict[str, object], int]:
    report, status = run_nric_rehash_apply(
        client or _Client(driver),
        batch_size=batch_size,
        key=dataset.key,
        key_version=dataset.key_version,
        expected_database=expected_database or _database_name(driver),
        acknowledge_offline=True,
    )
    return cast(dict[str, object], report), status


def test_in_place_conversion_preserves_evidence_and_second_apply_is_zero_diff(
    neo4j_driver: Driver,
) -> None:
    key = secrets.token_hex(32)
    dataset = _dataset(key, include_skipped=True)
    _seed_dataset(neo4j_driver, dataset, include_skipped=True)
    revision = secrets.randbelow(100_000) + 1
    _seed_revision_counter(neo4j_driver, revision)
    identifiers = _counter(neo4j_driver, "MATCH (i:Identifier) RETURN count(i) AS count")
    before_relations = {
        identifier_id: _relationship_snapshot(neo4j_driver, identifier_id)
        for identifier_id in dataset.identifier_ids
    }
    skipped_props = (
        _identifier_properties(neo4j_driver, dataset.identifier_ids[4]),
        _identifier_properties(neo4j_driver, dataset.identifier_ids[5]),
    )

    report, exit_code = _apply(neo4j_driver, dataset)
    assert exit_code == 1
    assert report["status"] == "blocked"
    assert report["changes"] == 4
    assert report["converted_in_place"] == 4
    assert report["already_digest"] == 5
    assert report["unprocessable"] == {"masked": 1, "empty": 0, "unknown_format": 0}

    for index in range(4):
        properties = _identifier_properties(neo4j_driver, dataset.identifier_ids[index])
        canonical, _quality = normalize_nric(dataset.raw_values[index])
        if canonical is None:
            pytest.fail("generated fixture did not normalize")
        digest = nric_digest(canonical, key)
        _safe_equal(properties.get("identifier_id"), dataset.identifier_ids[index], "Identifier id")
        _safe_equal(properties.get("identifier_scope"), "global", "Identifier scope")
        _safe_equal(properties.get("normalized_value"), digest, "normalized digest")
        _safe_equal(properties.get("hashed_value"), digest, "hashed digest")
        _safe_equal(properties.get("nric_masked"), mask_nric(canonical), "masked value")
        _safe_equal(
            properties.get("checksum_valid"), nric_checksum_valid(canonical), "checksum flag"
        )
        _safe_equal(properties.get("nric_key_version"), dataset.key_version, "key version")
        ciphertext = properties.get("nric_encrypted")
        if not isinstance(ciphertext, str):
            pytest.fail("converted Identifier ciphertext was absent")
        try:
            decrypted = _fernet_cipher(key).decrypt(ciphertext.encode("ascii"))
        except (InvalidToken, UnicodeEncodeError):
            pytest.fail("converted Identifier ciphertext could not be decrypted")
        _safe_equal(decrypted, canonical.encode("utf-8"), "decrypted canonical")
        _safe_equal(
            _relationship_snapshot(neo4j_driver, dataset.identifier_ids[index]),
            before_relations[dataset.identifier_ids[index]],
            "incident evidence",
        )

    _safe_equal(
        _identifier_properties(neo4j_driver, dataset.identifier_ids[4]),
        skipped_props[0],
        "masked Identifier unchanged",
    )
    _safe_equal(
        _identifier_properties(neo4j_driver, dataset.identifier_ids[5]),
        skipped_props[1],
        "existing digest Identifier unchanged",
    )
    assert _counter(neo4j_driver, "MATCH (i:Identifier) RETURN count(i) AS count") == identifiers
    assert _counter(neo4j_driver, "MATCH (e:IdentityLinkRevision) RETURN count(e) AS count") == 0
    assert (
        _counter(
            neo4j_driver,
            "MATCH (c:IdentityLinkRevisionCounter {stream_key: $key}) "
            "RETURN c.current_revision AS count",
            key="identity_link_revision_stream_v1",
        )
        == revision
    )

    before_retry = _graph_fingerprint(neo4j_driver)
    rerun, rerun_status = _apply(neo4j_driver, dataset)
    assert rerun_status == 1
    assert rerun["changes"] == 0
    _safe_equal(_graph_fingerprint(neo4j_driver), before_retry, "completed rerun fingerprint")


def test_crash_resume_matches_uninterrupted_conversion(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = secrets.token_hex(32)
    dataset = _dataset(key, include_skipped=False)
    _seed_dataset(neo4j_driver, dataset, include_skipped=False)
    _seed_revision_counter(neo4j_driver, 53)
    original_encrypt = encrypt_nric
    ciphertext_cache: dict[str, str] = {}
    calls = 0
    fail_on_call: list[int | None] = [4]

    def controlled_encrypt(canonical: str, secret: str) -> str:
        nonlocal calls
        calls += 1
        if fail_on_call[0] == calls:
            raise RuntimeError("synthetic encryption interruption")
        if canonical not in ciphertext_cache:
            ciphertext_cache[canonical] = original_encrypt(canonical, secret)
        return ciphertext_cache[canonical]

    monkeypatch.setattr("src.nric_rehash_apply.encrypt_nric", controlled_encrypt)
    with pytest.raises(RuntimeError):
        _apply(neo4j_driver, dataset, batch_size=2)

    migration = _counter_record(neo4j_driver)
    assert migration["phase"] == "convert"
    _safe_equal(migration.get("cursor"), dataset.identifier_ids[1], "last committed cursor")
    assert migration["batches"] == 1
    assert migration["converted_in_place"] == 2
    for index in range(2):
        properties = _identifier_properties(neo4j_driver, dataset.identifier_ids[index])
        if not isinstance(properties.get("normalized_value"), str):
            pytest.fail("committed Identifier digest was absent")
        if properties.get("normalized_value") == dataset.raw_values[index]:
            pytest.fail("failed batch changed a plaintext Identifier")
    for index in range(2, 4):
        properties = _identifier_properties(neo4j_driver, dataset.identifier_ids[index])
        _safe_equal(
            properties.get("normalized_value"), dataset.raw_values[index], "rolled-back row"
        )

    before_key_rejection = _graph_fingerprint(neo4j_driver)
    with pytest.raises(NricRehashError) as changed_key:
        _apply(neo4j_driver, _dataset(secrets.token_hex(32), include_skipped=False), batch_size=2)
    assert changed_key.value.code == "key_changed"
    _safe_equal(
        _graph_fingerprint(neo4j_driver), before_key_rejection, "key-change no-write fingerprint"
    )

    fail_on_call[0] = None
    resumed, resumed_status = _apply(neo4j_driver, dataset, batch_size=2)
    assert resumed_status == 0
    assert resumed["converted_in_place"] == 4
    _normalize_migration_clock(neo4j_driver)
    resumed_fingerprint = _graph_fingerprint(neo4j_driver)

    _clear_owned_graph(neo4j_driver)
    _seed_dataset(neo4j_driver, dataset, include_skipped=False)
    _seed_revision_counter(neo4j_driver, 53)
    calls = 0
    uninterrupted, uninterrupted_status = _apply(neo4j_driver, dataset, batch_size=2)
    assert uninterrupted_status == 0
    assert uninterrupted["converted_in_place"] == 4
    _normalize_migration_clock(neo4j_driver)
    _safe_equal(
        _graph_fingerprint(neo4j_driver),
        resumed_fingerprint,
        "resumed and uninterrupted graph fingerprints",
    )


def _counter_record(driver: Driver) -> dict[str, object]:
    with driver.session() as session:
        row = session.run(
            "MATCH (migration:DataMigration {migration_key: $key}) "
            "RETURN properties(migration) AS properties",
            key="nric_hmac_rehash_v1",
        ).single(strict=True)
    properties = row["properties"]
    if not isinstance(properties, dict):
        raise AssertionError("migration properties were malformed")
    return dict(properties)


def _normalize_migration_clock(driver: Driver) -> None:
    # Normalize wall-clock and lease-lock fields only for cross-run comparison.
    with driver.session() as session:
        session.run(
            "MATCH (migration:DataMigration {migration_key: $key}) "
            "SET migration.created_at = datetime($fixed), "
            "migration.updated_at = datetime($fixed), "
            "migration.offline_acknowledged_at = datetime($fixed), "
            "migration.lock_version = 0",
            key="nric_hmac_rehash_v1",
            fixed="2020-01-01T00:00:00Z",
        ).consume()


def test_owner_overwritten_between_batches_rolls_back_the_lost_batch(
    neo4j_driver: Driver,
) -> None:
    dataset = _dataset(secrets.token_hex(32), include_skipped=False)
    _seed_dataset(neo4j_driver, dataset, include_skipped=False)
    _seed_revision_counter(neo4j_driver, 83)
    client = _StealingClient(neo4j_driver, uuid.uuid4().hex)

    with pytest.raises(NricRehashError) as error:
        run_nric_rehash_apply(
            client,
            batch_size=1,
            key=dataset.key,
            key_version=dataset.key_version,
            expected_database=_database_name(neo4j_driver),
            acknowledge_offline=True,
        )
    assert error.value.code == "lease_lost"
    migration = _counter_record(neo4j_driver)
    _safe_equal(migration.get("cursor"), dataset.identifier_ids[0], "committed cursor")
    assert migration["batches"] == 1
    assert migration["converted_in_place"] == 1
    _safe_equal(
        migration.get("lease_owner"), client.replacement_owner, "replacement owner retained"
    )
    first = _identifier_properties(neo4j_driver, dataset.identifier_ids[0])
    second = _identifier_properties(neo4j_driver, dataset.identifier_ids[1])
    assert first.get("nric_encrypted") is not None
    _safe_equal(second.get("normalized_value"), dataset.raw_values[1], "lost batch rollback")


def test_lease_collision_database_and_null_scope_fail_closed(
    neo4j_driver: Driver,
) -> None:
    key = secrets.token_hex(32)
    dataset = _dataset(key, include_skipped=False)
    _seed_revision_counter(neo4j_driver, 71)
    owner = uuid.uuid4().hex
    _seed_migration(
        neo4j_driver,
        dataset,
        owner=owner,
        lease_seconds=600,
        baseline_total=0,
        baseline_non_digest=0,
        baseline_digest_shaped=0,
        baseline_revision=71,
    )
    before_live_lease = _counter_record(neo4j_driver)
    with pytest.raises(NricRehashError) as live_lease:
        _apply(neo4j_driver, dataset)
    assert live_lease.value.code == "lease_unavailable"
    _safe_equal(_counter_record(neo4j_driver), before_live_lease, "live lease unchanged")

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (migration:DataMigration {migration_key: $key}) "
            "SET migration.lease_until = datetime() - duration({seconds: 1})",
            key="nric_hmac_rehash_v1",
        ).consume()
    expired, expired_status = _apply(neo4j_driver, dataset)
    assert expired_status == 0
    assert expired["phase"] == "convert_complete"
    assert _counter_record(neo4j_driver).get("offline_acknowledged_at") is not None

    changed_key_fingerprint = _graph_fingerprint(neo4j_driver)
    with pytest.raises(NricRehashError) as changed_key:
        _apply(neo4j_driver, _dataset(secrets.token_hex(32), include_skipped=False))
    assert changed_key.value.code == "key_changed"
    _safe_equal(_graph_fingerprint(neo4j_driver), changed_key_fingerprint, "key change no-write")

    _clear_owned_graph(neo4j_driver)
    collision_key = secrets.token_hex(32)
    canonical = _valid_canonical(set())
    digest = nric_digest(canonical, collision_key)
    plain_id = f"a-{uuid.uuid4().hex}"
    digest_id = f"b-{uuid.uuid4().hex}"
    _seed_identifier_only(neo4j_driver, plain_id, canonical)
    _seed_identifier_only(
        neo4j_driver,
        digest_id,
        digest,
        hashed_value=digest,
        nric_masked=mask_nric(canonical),
        checksum_valid=nric_checksum_valid(canonical),
        key_version=_VERSION,
        encrypted=encrypt_nric(canonical, collision_key),
    )
    plain_before = _identifier_properties(neo4j_driver, plain_id)
    digest_before = _identifier_properties(neo4j_driver, digest_id)
    with pytest.raises(NricRehashError) as collision:
        run_nric_rehash_apply(
            _Client(neo4j_driver),
            batch_size=10,
            key=collision_key,
            key_version=_VERSION,
            expected_database=_database_name(neo4j_driver),
            acknowledge_offline=True,
        )
    assert collision.value.code == "collision_unsupported"
    _safe_equal(
        _identifier_properties(neo4j_driver, plain_id), plain_before, "collision plaintext row"
    )
    _safe_equal(
        _identifier_properties(neo4j_driver, digest_id), digest_before, "collision target row"
    )
    migration = _counter_record(neo4j_driver)
    _safe_equal(migration.get("cursor"), None, "collision cursor unchanged")
    assert migration["batches"] == 0

    _clear_owned_graph(neo4j_driver)
    scope_data = _dataset(secrets.token_hex(32), include_skipped=False)
    _seed_identifier_only(
        neo4j_driver, scope_data.identifier_ids[0], scope_data.raw_values[0], scope=None
    )
    before_scope = _graph_fingerprint(neo4j_driver)
    wrong_database = f"{_database_name(neo4j_driver)}-wrong"
    with pytest.raises(NricRehashError) as mismatch:
        _apply(neo4j_driver, scope_data, expected_database=wrong_database)
    assert mismatch.value.code == "database_mismatch"
    with pytest.raises(NricRehashError) as null_scope:
        _apply(neo4j_driver, scope_data)
    assert null_scope.value.code == "null_scope_present"
    _safe_equal(_graph_fingerprint(neo4j_driver), before_scope, "precondition no-write fingerprint")
    assert (
        _counter(
            neo4j_driver,
            "MATCH (migration:DataMigration {migration_key: $key}) "
            "RETURN count(migration) AS count",
            key="nric_hmac_rehash_v1",
        )
        == 0
    )


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
