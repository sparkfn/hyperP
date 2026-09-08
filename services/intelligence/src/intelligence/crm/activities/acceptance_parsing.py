"""Exact immutable candidate codecs for CRM activity archive attempts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from intelligence.crm.activities.acceptance_descriptor import (
    PublicationPointer,
    count,
    descriptor_relative_path,
    inventory,
    run_id,
)
from intelligence.crm.activities.models import validate_snapshot_id
from intelligence.models import OutputInventory

_SHA256 = frozenset("0123456789abcdef")
PUBLICATION_PREFIX = "publication-candidate-"
VERIFICATION_PREFIX = "verification-candidate-"


@dataclass(frozen=True)
class VerificationCandidate:
    """A child-written verification candidate tied to one accepted descriptor."""

    raw: dict[str, object]
    run_id: str
    accepted_run_id: str
    snapshot_id: str
    accepted_manifest_digest: str
    accepted_descriptor_sha256: str
    inventory: tuple[OutputInventory, ...]


def publication_candidate_name(run_id_value: str) -> str:
    """Return immutable publication evidence name bound to its embedded run ID."""
    run_id(run_id_value, "candidate run")
    return f"{PUBLICATION_PREFIX}{sha256(run_id_value.encode('utf-8')).hexdigest()}.json"


def verification_candidate_name(run_id_value: str) -> str:
    """Return immutable verification evidence name bound to its embedded run ID."""
    run_id(run_id_value, "candidate run")
    return f"{VERIFICATION_PREFIX}{sha256(run_id_value.encode('utf-8')).hexdigest()}.json"


def parse_publication(value: Mapping[str, object], checkpoint_id: str) -> PublicationPointer:
    """Parse one minimal publication candidate pointer."""
    expected = {
        "run_id",
        "descriptor_relative_path",
        "descriptor_sha256",
        "descriptor_byte_count",
    }
    if set(value) != expected:
        raise ValueError("publication candidate schema is invalid")
    pointer = PublicationPointer(
        run_id(text(value, "run_id"), "candidate run"),
        text(value, "descriptor_relative_path"),
        digest(value.get("descriptor_sha256"), "descriptor sha256"),
        count(value, "descriptor_byte_count"),
    )
    if pointer.descriptor_relative_path != descriptor_relative_path(checkpoint_id):
        raise ValueError("publication candidate descriptor path is invalid")
    return pointer


def parse_verification(value: Mapping[str, object], checkpoint_id: str) -> VerificationCandidate:
    """Parse one verification candidate bound to a publication descriptor."""
    expected = {
        "checkpoint_id",
        "run_id",
        "accepted_run_id",
        "snapshot_id",
        "accepted_manifest_digest",
        "accepted_descriptor_sha256",
        "inventory",
    }
    if set(value) != expected or value.get("checkpoint_id") != checkpoint_id:
        raise ValueError("verification candidate schema is invalid")
    snapshot_id = text(value, "snapshot_id")
    validate_snapshot_id(snapshot_id)
    return VerificationCandidate(
        dict(value),
        run_id(text(value, "run_id"), "candidate run"),
        run_id(text(value, "accepted_run_id"), "accepted run"),
        snapshot_id,
        digest(value.get("accepted_manifest_digest"), "accepted manifest digest"),
        digest(value.get("accepted_descriptor_sha256"), "accepted descriptor sha256"),
        inventory(value, "inventory"),
    )


def candidate_name(value: str, prefix: str) -> bool:
    suffix = value.removeprefix(prefix).removesuffix(".json")
    return value == f"{prefix}{suffix}.json" and len(suffix) == 64 and set(suffix) <= _SHA256


def digest(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value) - _SHA256:
        raise ValueError(f"{field} is invalid")
    return value


def text(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} is invalid")
    return result
