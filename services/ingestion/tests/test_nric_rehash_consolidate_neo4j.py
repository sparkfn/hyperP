"""Neo4j 5.26 integration coverage for NRIC collision consolidation."""

from __future__ import annotations

import logging
import secrets
import uuid
from uuid import UUID, uuid4

import pytest
from _nric_rehash_neo4j_fingerprint import (
    _active_attribution_snapshot,
    _graph_fingerprint,
    _identifier_element_ids,
    _identifier_properties,
    _identity_revision_snapshot,
    _non_derived_relationship_snapshot,
    _normalize_legacy_clock,
    _normalize_migration_clock,
    _participant_nric_links,
    _person_properties_snapshot,
    _safe_equal,
)
from _nric_rehash_neo4j_fixture import (
    _apply,
    _clear_owned_graph,
    _Client,
    _counter,
    _counter_record,
    neo4j_driver,
)
from _nric_rehash_neo4j_seed_legacy import (
    read_legacy_evidence_counts,
    seed_identity_revisions,
    seed_legacy_world,
    seed_migration_baseline,
    seed_unsupported_relationship,
)
from neo4j import Driver, ManagedTransaction
from src import nric_rehash, nric_rehash_apply, nric_rehash_consolidate
from src.identity_links import ParticipantKind, link_id
from src.normalizers.nric import (
    _fernet_cipher,
    mask_nric,
    nric_checksum_valid,
    nric_digest,
)
from src.nric_rehash_plan import NricRehashError
from src.nric_rehash_state import acquire_lease

__all__ = ("neo4j_driver",)


def test_consolidation_preserves_evidence_and_reconciles_participants(
    neo4j_driver: Driver,
) -> None:
    key = secrets.token_hex(32)
    world = seed_legacy_world(neo4j_driver, key)
    groups = (
        (world.identifier_ids[:3], world.complete_survivor_id),
        (world.identifier_ids[3:5], world.sibling_survivor_id),
        (world.identifier_ids[5:7], world.gap_survivor_id),
    )
    evidence_before = {
        survivor_id: tuple(
            evidence
            for identifier_id in member_ids
            for evidence in _non_derived_relationship_snapshot(neo4j_driver, identifier_id)
        )
        for member_ids, survivor_id in groups
    }
    people_before = _person_properties_snapshot(neo4j_driver, world.person_ids)
    complete_before = _identifier_properties(neo4j_driver, world.complete_survivor_id)
    complete_ciphertext = complete_before.get("nric_encrypted")
    if not isinstance(complete_ciphertext, str):
        pytest.fail("complete survivor ciphertext was absent")
    evidence_counts_before = read_legacy_evidence_counts(neo4j_driver)

    report, exit_code = _apply(neo4j_driver, world, batch_size=100)

    assert exit_code == 0
    assert report["status"] == "ok"
    assert report["converted_in_place"] == 1
    assert report["converted_consolidated"] == 4
    assert report["removed_nodes"] == 4
    assert report["changes"] == 5
    assert report["count_equation_ok"]
    assert report["conversion_equation_ok"]
    assert report["evidence_equation_ok"]
    assert report["evidence_baseline"]["has_identifier"] == 14
    assert report["evidence_final"]["has_identifier"] == 9
    assert report["attribution_unchanged"]
    assert report["revision_unchanged"]
    assert report["final"]["identifier_count"] == 3
    _safe_equal(len(set(world.canonical_values)), 3, "seeded distinct NRIC count")
    assert (
        _counter(
            neo4j_driver, "MATCH (i:Identifier {identifier_type: 'nric'}) RETURN count(i) AS count"
        )
        == 3
    )

    evidence_after = read_legacy_evidence_counts(neo4j_driver)
    for key_name in ("asserts", "identified_by", "anchored_by", "moved", "active_links"):
        _safe_equal(evidence_after[key_name], evidence_counts_before[key_name], key_name)
    for survivor_id, expected in evidence_before.items():
        actual = _non_derived_relationship_snapshot(neo4j_driver, survivor_id)
        _safe_equal(actual, tuple(sorted(expected)), "survivor evidence")
    _safe_equal(
        _identifier_properties(neo4j_driver, world.complete_survivor_id).get("nric_encrypted"),
        complete_ciphertext,
        "complete survivor ciphertext",
    )
    _safe_equal(
        _person_properties_snapshot(neo4j_driver, world.person_ids),
        people_before,
        "Person golden properties",
    )

    canonical = world.canonical_values[1]
    sibling = _identifier_properties(neo4j_driver, world.sibling_survivor_id)
    _safe_equal(
        sibling.get("normalized_value"), nric_digest(canonical, key), "sibling survivor digest"
    )
    assert _identifier_count(neo4j_driver, world.identifier_ids[0]) == 0
    assert _identifier_count(neo4j_driver, world.identifier_ids[1]) == 0
    assert _identifier_count(neo4j_driver, world.identifier_ids[4]) == 0
    assert _identifier_count(neo4j_driver, world.identifier_ids[5]) == 0
    gap_survivor = _identifier_properties(neo4j_driver, world.gap_survivor_id)
    gap_digest = nric_digest(world.canonical_values[2], key)
    _safe_equal(gap_survivor.get("normalized_value"), gap_digest, "gap digest")
    _safe_equal(gap_survivor.get("hashed_value"), gap_digest, "gap hashed value")
    _safe_equal(gap_survivor.get("nric_key_version"), world.key_version, "gap key version")
    _safe_equal(gap_survivor.get("nric_masked"), mask_nric(world.canonical_values[2]), "gap mask")
    _safe_equal(
        gap_survivor.get("checksum_valid"),
        nric_checksum_valid(world.canonical_values[2]),
        "gap checksum",
    )
    gap_ciphertext = gap_survivor.get("nric_encrypted")
    if not isinstance(gap_ciphertext, str):
        pytest.fail("gap survivor ciphertext was absent")
    decrypted = _fernet_cipher(key).decrypt(gap_ciphertext.encode("ascii"))
    _safe_equal(decrypted, world.canonical_values[2].encode("utf-8"), "gap survivor decrypt")

    participants = (
        ("person", world.person_ids[0]),
        ("person", world.person_ids[1]),
        ("profile", world.profile_id),
    )
    expected_survivors = {
        world.complete_survivor_id,
        world.sibling_survivor_id,
        world.gap_survivor_id,
    }
    for kind, participant_id in participants:
        links = _participant_nric_links(neo4j_driver, kind, participant_id)
        assert len(links) == len(expected_survivors)
        _safe_equal(
            {identifier_id for identifier_id, _, _, _ in links},
            expected_survivors,
            "participant survivors",
        )
        for identifier_id, actual_link_id, participant_count, exclusivity in links:
            _safe_equal(
                actual_link_id, link_id(participant_id, identifier_id), "participant link id"
            )
            if (
                not isinstance(participant_count, int)
                or isinstance(participant_count, bool)
                or participant_count < 1
            ):
                pytest.fail("survivor participant count was malformed")
            _safe_equal(participant_count, 3, "survivor participant count")
            _safe_equal(
                abs(exclusivity - 1 / participant_count) <= 1e-6,
                True,
                "participant link exclusivity",
            )


def test_unsupported_edges_roll_back_the_fenced_batch(
    neo4j_driver: Driver,
) -> None:
    for outgoing in (False, True):
        key = secrets.token_hex(32)
        world = seed_legacy_world(neo4j_driver, key)
        seed_unsupported_relationship(
            neo4j_driver,
            world.identifier_ids[0],
            outgoing=outgoing,
        )
        seed_migration_baseline(neo4j_driver, world)
        client = _Client(neo4j_driver)
        owner = uuid.uuid4().hex
        state = acquire_lease(
            client,
            owner,
            world.key,
            world.key_version,
            restart_completed=False,
        )
        before = _graph_fingerprint(neo4j_driver)
        with pytest.raises(NricRehashError) as error:
            nric_rehash_apply._process_batch(
                client,
                state,
                owner,
                1,
                world.key,
                world.key_version,
            )
        assert error.value.code == "unsupported_edge"
        _safe_equal(_graph_fingerprint(neo4j_driver), before, "unsupported edge rollback")
        migration = _counter_record(neo4j_driver)
        _safe_equal(migration.get("cursor"), None, "unsupported edge cursor")
        assert migration["batches"] == 0
        assert "unsupported_edge" in nric_rehash._APPLY_EXIT_ONE_CODES
        _clear_owned_graph(neo4j_driver)


def test_crash_after_edge_moves_rolls_back_and_resumes_like_uninterrupted(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = secrets.token_hex(32)
    world = seed_legacy_world(neo4j_driver, key)
    seed_migration_baseline(neo4j_driver, world)
    encryption_cache: dict[str, str] = {}
    original_encrypt = nric_rehash_apply.encrypt_nric

    def cached_encrypt(canonical: str, secret: str) -> str:
        if canonical not in encryption_cache:
            encryption_cache[canonical] = original_encrypt(canonical, secret)
        return encryption_cache[canonical]

    monkeypatch.setattr(nric_rehash_apply, "encrypt_nric", cached_encrypt)
    monkeypatch.setattr(nric_rehash_consolidate, "encrypt_nric", cached_encrypt)
    original_recompute = nric_rehash_consolidate.recompute_participant_links
    failed = False

    def fail_once(tx: ManagedTransaction, kind: ParticipantKind, participant_id: str) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("synthetic consolidation interruption")
        original_recompute(tx, kind, participant_id)

    _normalize_legacy_clock(neo4j_driver)
    _normalize_migration_clock(neo4j_driver)
    before = _graph_fingerprint(neo4j_driver)
    with monkeypatch.context() as patch:
        patch.setattr(nric_rehash_consolidate, "recompute_participant_links", fail_once)
        with pytest.raises(RuntimeError):
            _apply(neo4j_driver, world, batch_size=1)
    _normalize_legacy_clock(neo4j_driver)
    _normalize_migration_clock(neo4j_driver)
    _safe_equal(_graph_fingerprint(neo4j_driver), before, "crashed batch rollback")
    migration = _counter_record(neo4j_driver)
    _safe_equal(migration.get("cursor"), None, "crashed batch cursor")
    assert migration["batches"] == 0
    assert migration["converted_consolidated"] == 0

    resumed, resumed_status = _apply(neo4j_driver, world, batch_size=100)
    assert resumed_status == 0
    assert resumed["converted_consolidated"] == 4
    _normalize_legacy_clock(neo4j_driver)
    _normalize_migration_clock(neo4j_driver)
    resumed_fingerprint = _graph_fingerprint(neo4j_driver)

    _clear_owned_graph(neo4j_driver)
    seed_legacy_world(neo4j_driver, key, world=world)
    seed_migration_baseline(neo4j_driver, world)
    uninterrupted, uninterrupted_status = _apply(neo4j_driver, world, batch_size=100)
    assert uninterrupted_status == 0
    assert uninterrupted["converted_consolidated"] == 4
    _normalize_legacy_clock(neo4j_driver)
    _normalize_migration_clock(neo4j_driver)
    _safe_equal(
        _graph_fingerprint(neo4j_driver),
        resumed_fingerprint,
        "resumed and uninterrupted fingerprints",
    )


def test_parity_revision_continuity_idempotency_and_no_leak(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    key = secrets.token_hex(32)
    world = seed_legacy_world(neo4j_driver, key)
    seed_identity_revisions(neo4j_driver, world.person_ids[0])
    revision_before = _identity_revision_snapshot(neo4j_driver)
    assert revision_before[0] >= 2
    assert len(revision_before[1]) >= 2
    assert revision_before[3] == 0
    attribution_before = _active_attribution_snapshot(neo4j_driver)
    element_ids_before = _identifier_element_ids(neo4j_driver)
    lease_owner = uuid4().hex
    monkeypatch.setattr(
        nric_rehash_apply.uuid,
        "uuid4",
        lambda: UUID(hex=lease_owner),
    )
    caplog.set_level(logging.DEBUG)

    report, exit_code = _apply(neo4j_driver, world, batch_size=100)

    assert exit_code == 0
    assert report["status"] == "ok"
    assert report["final"]["identifier_count"] == 3
    assert report["count_equation_ok"]
    assert report["conversion_equation_ok"]
    assert report["evidence_equation_ok"]
    assert report["attribution_unchanged"]
    assert report["revision_unchanged"]
    revision_after = _identity_revision_snapshot(neo4j_driver)
    _safe_equal(revision_after, revision_before, "identity-link revision stream")
    attribution_after = _active_attribution_snapshot(neo4j_driver)
    _safe_equal(attribution_after, attribution_before, "active source attribution")

    before_retry = _graph_fingerprint(neo4j_driver)
    rerun, rerun_status = _apply(neo4j_driver, world, batch_size=100)
    assert rerun_status == 0
    assert rerun["changes"] == 0
    _safe_equal(
        _graph_fingerprint(neo4j_driver),
        before_retry,
        "completed rerun including migration timestamps",
    )

    captured = capsys.readouterr()
    # The Neo4j driver logs query parameters at DEBUG; exclude its records from this app-leak check.
    application_logs = "\n".join(
        record.getMessage() for record in caplog.records if not record.name.startswith("neo4j")
    )
    output = captured.out + captured.err + application_logs
    sensitive_values = [key, lease_owner, *world.canonical_values, *world.identifier_ids]
    sensitive_values.extend(nric_digest(value, key) for value in world.canonical_values)
    sensitive_values.extend(value.lower() for value in world.canonical_values)
    sensitive_values.extend(f"xxx{value}" for value in world.canonical_values)
    sensitive_values.extend(element_ids_before)
    sensitive_values.extend(_identifier_element_ids(neo4j_driver))
    for identifier_id in (
        world.complete_survivor_id,
        world.sibling_survivor_id,
        world.gap_survivor_id,
    ):
        ciphertext: object = _identifier_properties(neo4j_driver, identifier_id).get(
            "nric_encrypted"
        )
        if isinstance(ciphertext, str):
            sensitive_values.append(ciphertext)
    for sensitive in sensitive_values:
        if sensitive and sensitive in output:
            pytest.fail("generated sensitive value leaked to logs or command output")


def _identifier_count(driver: Driver, identifier_id: str) -> int:
    return _counter(
        driver,
        "MATCH (i:Identifier {identifier_id: $identifier_id}) RETURN count(i) AS count",
        identifier_id=identifier_id,
    )
