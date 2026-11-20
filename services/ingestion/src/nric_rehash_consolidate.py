"""Consolidate a plaintext NRIC Identifier into its keyed digest survivor."""

from __future__ import annotations

from dataclasses import dataclass

from neo4j import ManagedTransaction, Record

from src.graph.queries import nric_rehash_consolidate as consolidate_queries
from src.identity_link_writes import recompute_participant_counts, recompute_participant_links
from src.identity_links import ParticipantKind
from src.normalizers.nric import encrypt_nric
from src.nric_rehash_plan import NricRehashError

SUPPORTED_EDGES: frozenset[tuple[str, bool, str]] = frozenset(
    {
        ("ASSERTS", False, "SourceRecord"),
        ("IDENTIFIED_BY", False, "Person"),
        ("ANCHORED_BY", False, "Person"),
        ("MOVED_RELATIONSHIP", False, "MergeEvent"),
        ("HAS_IDENTIFIER", False, "Person"),
        ("HAS_IDENTIFIER", False, "Profile"),
    }
)

_MOVE_QUERIES: tuple[tuple[str, str], ...] = (
    ("ASSERTS", consolidate_queries.NRIC_REHASH_MOVE_ASSERTS),
    ("IDENTIFIED_BY", consolidate_queries.NRIC_REHASH_MOVE_IDENTIFIED_BY),
    ("ANCHORED_BY", consolidate_queries.NRIC_REHASH_MOVE_ANCHORED_BY),
    ("MOVED_RELATIONSHIP", consolidate_queries.NRIC_REHASH_MOVE_MOVED_RELATIONSHIP),
)


@dataclass(frozen=True)
class _RelationshipInventory:
    relationship_type: str
    loser_is_start: bool
    other_labels: tuple[str, ...]
    count: int


@dataclass(frozen=True, order=True)
class _Participant:
    kind: ParticipantKind
    participant_id: str


def is_supported_edge(relationship_type: str, loser_is_start: bool, other_label: str) -> bool:
    """Return whether one direction/type/endpoint-label edge can be preserved."""
    return (relationship_type, loser_is_start, other_label) in SUPPORTED_EDGES


def consolidate_identifier(
    tx: ManagedTransaction,
    *,
    loser_element_id: str,
    survivor_element_id: str,
    plaintext_value: str,
    canonical: str,
    digest: str,
    key: str,
    key_version: str,
    masked: str,
    checksum_valid: bool,
) -> None:
    """Move supported evidence from a plaintext node to its digest-keyed survivor."""
    survivor = _lock_and_validate_nodes(
        tx,
        loser_element_id,
        survivor_element_id,
        plaintext_value,
        digest,
    )
    inventory = _read_relationship_inventory(tx, loser_element_id)
    _validate_relationship_inventory(inventory)
    participants = _read_participants(tx, loser_element_id, survivor_element_id)
    _move_evidence(tx, inventory, loser_element_id, survivor_element_id)
    _refresh_participant_links(tx, participants, digest)
    _delete_loser(tx, loser_element_id)
    _complete_survivor_metadata(
        tx,
        survivor,
        survivor_element_id,
        digest,
        canonical,
        key,
        key_version,
        masked,
        checksum_valid,
    )


def _refresh_participant_links(
    tx: ManagedTransaction,
    participants: tuple[_Participant, ...],
    digest: str,
) -> None:
    for participant in participants:
        recompute_participant_links(tx, participant.kind, participant.participant_id)
    recompute_participant_counts(tx, {("nric", "global", digest)})
    for participant in participants:
        recompute_participant_links(tx, participant.kind, participant.participant_id)


def _lock_and_validate_nodes(
    tx: ManagedTransaction,
    loser_element_id: str,
    survivor_element_id: str,
    plaintext_value: str,
    digest: str,
) -> Record:
    if loser_element_id == survivor_element_id:
        raise NricRehashError("drift_detected")
    element_ids = sorted((loser_element_id, survivor_element_id))
    nodes = tuple(
        tx.run(
            consolidate_queries.NRIC_REHASH_LOCK_CONSOLIDATION_IDENTIFIERS,
            element_ids=element_ids,
        )
    )
    if len(nodes) != 2:
        raise NricRehashError("drift_detected")
    by_element_id: dict[str, Record] = {}
    for node in nodes:
        element_id = _required_string(node, "element_id")
        if element_id in by_element_id:
            raise NricRehashError("drift_detected")
        by_element_id[element_id] = node
    loser = by_element_id.get(loser_element_id)
    survivor = by_element_id.get(survivor_element_id)
    if loser is None or survivor is None:
        raise NricRehashError("drift_detected")
    if not _matches_identifier(loser, plaintext_value):
        raise NricRehashError("drift_detected")
    if not _matches_identifier(survivor, digest):
        raise NricRehashError("drift_detected")
    return survivor


def _matches_identifier(record: Record, normalized_value: str) -> bool:
    return (
        record.get("identifier_type") == "nric"
        and record.get("identifier_scope") == "global"
        and record.get("normalized_value") == normalized_value
    )


def _read_relationship_inventory(
    tx: ManagedTransaction, loser_element_id: str
) -> tuple[_RelationshipInventory, ...]:
    records = tuple(
        tx.run(
            consolidate_queries.NRIC_REHASH_INVENTORY_CONSOLIDATION_RELATIONSHIPS,
            loser=loser_element_id,
        )
    )
    if not records:
        raise NricRehashError("drift_detected")
    inventory: list[_RelationshipInventory] = []
    for record in records:
        count = _required_count(record, "relationship_count")
        relationship_type: object = record.get("relationship_type")
        if relationship_type is None:
            if count != 0:
                raise NricRehashError("drift_detected")
            continue
        if not isinstance(relationship_type, str) or count < 1:
            raise NricRehashError("drift_detected")
        loser_is_start: object = record.get("loser_is_start")
        labels: object = record.get("other_labels")
        if not isinstance(loser_is_start, bool) or not isinstance(labels, list):
            raise NricRehashError("drift_detected")
        if any(not isinstance(label, str) for label in labels):
            raise NricRehashError("drift_detected")
        inventory.append(
            _RelationshipInventory(
                relationship_type=relationship_type,
                loser_is_start=loser_is_start,
                other_labels=tuple(labels),
                count=count,
            )
        )
    return tuple(inventory)


def _validate_relationship_inventory(
    inventory: tuple[_RelationshipInventory, ...],
) -> None:
    for item in inventory:
        if len(item.other_labels) != 1 or not is_supported_edge(
            item.relationship_type,
            item.loser_is_start,
            item.other_labels[0],
        ):
            raise NricRehashError("unsupported_edge")


def _read_participants(
    tx: ManagedTransaction,
    loser_element_id: str,
    survivor_element_id: str,
) -> tuple[_Participant, ...]:
    records = tx.run(
        consolidate_queries.NRIC_REHASH_CONSOLIDATION_PARTICIPANTS,
        loser=loser_element_id,
        survivor=survivor_element_id,
    )
    participants: set[_Participant] = set()
    for record in records:
        labels = record.get("participant_labels")
        kind = record.get("participant_kind")
        participant_id = record.get("participant_id")
        if not isinstance(labels, list) or len(labels) != 1 or not isinstance(labels[0], str):
            raise NricRehashError("unsupported_edge")
        if labels[0] == "Person" and kind == "person":
            participant_kind: ParticipantKind = "person"
        elif labels[0] == "Profile" and kind == "profile":
            participant_kind = "profile"
        else:
            raise NricRehashError("unsupported_edge")
        if not isinstance(participant_id, str) or not participant_id:
            raise NricRehashError("unsupported_edge")
        participants.add(_Participant(participant_kind, participant_id))
    return tuple(sorted(participants))


def _move_evidence(
    tx: ManagedTransaction,
    inventory: tuple[_RelationshipInventory, ...],
    loser_element_id: str,
    survivor_element_id: str,
) -> None:
    expected_counts = {relationship_type: 0 for relationship_type, _ in _MOVE_QUERIES}
    for item in inventory:
        if item.relationship_type in expected_counts:
            expected_counts[item.relationship_type] += item.count
    for relationship_type, query in _MOVE_QUERIES:
        record = tx.run(
            query,
            loser=loser_element_id,
            survivor=survivor_element_id,
        ).single()
        if record is None or _required_count(record, "moved") != expected_counts[relationship_type]:
            raise NricRehashError("drift_detected")


def _delete_loser(tx: ManagedTransaction, loser_element_id: str) -> None:
    record = tx.run(
        consolidate_queries.NRIC_REHASH_DELETE_CONSOLIDATED_IDENTIFIER,
        loser=loser_element_id,
    ).single()
    if record is None or _required_count(record, "deleted") != 1:
        raise NricRehashError("residual_edges")


def _complete_survivor_metadata(
    tx: ManagedTransaction,
    survivor: Record,
    survivor_element_id: str,
    digest: str,
    canonical: str,
    key: str,
    key_version: str,
    masked: str,
    checksum_valid: bool,
) -> None:
    if _survivor_metadata_complete(survivor, digest, key_version):
        return
    record = tx.run(
        consolidate_queries.NRIC_REHASH_UPDATE_SURVIVOR_METADATA,
        survivor=survivor_element_id,
        digest=digest,
        hashed_value=digest,
        key_version=key_version,
        masked=masked,
        checksum_valid=checksum_valid,
        ciphertext=encrypt_nric(canonical, key),
    ).single()
    if record is None or _required_count(record, "updated") != 1:
        raise NricRehashError("drift_detected")


def _survivor_metadata_complete(record: Record, digest: str, key_version: str) -> bool:
    metadata_keys = ("nric_masked", "checksum_valid", "nric_key_version", "nric_encrypted")
    return (
        all(record.get(key) is not None for key in metadata_keys)
        and record.get("hashed_value") == digest
        and record.get("nric_key_version") == key_version
    )


def _required_string(record: Record, key: str) -> str:
    value: object = record.get(key)
    if isinstance(value, str) and value:
        return value
    raise NricRehashError("drift_detected")


def _required_count(record: Record, key: str) -> int:
    value: object = record.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise NricRehashError("drift_detected")
