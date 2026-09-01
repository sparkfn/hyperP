"""Zero-Bitrix mapping activation child for published mapping census work."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from src.crm_tenant_activation_contracts import (
    CrmTenantActivationCandidate,
    CrmTenantActivationCommand,
    CrmTenantActivationRelease,
)
from src.crm_tenant_mapping_contracts import CrmTenantMappingExpectedHead, CrmTenantMappingScope
from src.crm_tenant_projection_records import (
    CrmTenantProjectionExpectedHead,
    CrmTenantProjectionScope,
)
from src.standalone_crm_census_models import (
    MappingPrepareCensusRequest,
    MappingRollbackCensusRequest,
    StandaloneCrmChildEnvelope,
)
from src.standalone_crm_census_request_parser import parse_stored_census_request
from src.standalone_crm_census_requests import mapping_candidate_identity
from src.standalone_crm_census_types import StandaloneCrmStreamKind

MAPPING_CHILD_TASK_NAME = "src.standalone_crm_census_tasks.run_standalone_crm_mapping_activation"
_FIELDS = frozenset(
    {
        "census_id",
        "generation",
        "stream_kind",
        "frozen_upper_id",
        "revision_id",
        "task_name",
        "task_id",
        "queue",
        "payload_version",
    }
)


@dataclass(frozen=True)
class MappingChildClaim:
    envelope: StandaloneCrmChildEnvelope
    request: MappingPrepareCensusRequest | MappingRollbackCensusRequest
    fence_token: int


def parse_mapping_publication(raw: Mapping[str, object]) -> tuple[str, StandaloneCrmChildEnvelope]:
    """Accept exactly one mapping locator and reject source payloads before graph/client work."""
    if set(raw) != _FIELDS:
        raise ValueError("mapping child payload must be the exact stored publication")
    if raw.get("frozen_upper_id") is not None:
        raise ValueError("source child publications cannot execute through mapping child")
    revision_id = raw.get("revision_id")
    task_name = raw.get("task_name")
    if not isinstance(revision_id, str) or not revision_id or task_name != MAPPING_CHILD_TASK_NAME:
        raise ValueError("mapping child requires exact mapping task and revision")
    census_id, task_id, queue, payload_version = _texts(
        raw, "census_id", "task_id", "queue", "payload_version"
    )
    generation = raw.get("generation")
    stream_kind = raw.get("stream_kind")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or stream_kind not in {"contact", "lead", "company"}
    ):
        raise ValueError("mapping child has invalid generation or stream")
    kind: StandaloneCrmStreamKind
    if stream_kind == "contact":
        kind = "contact"
    elif stream_kind == "lead":
        kind = "lead"
    else:
        kind = "company"
    envelope = StandaloneCrmChildEnvelope(
        census_id,
        generation,
        kind,
        None,
        revision_id,
        task_name,
        task_id,
        queue,
        payload_version,
    )
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True), envelope


def build_mapping_claim(
    envelope: StandaloneCrmChildEnvelope, row: Mapping[str, object]
) -> MappingChildClaim:
    request_json = row.get("request_json")
    fence_token = row.get("fence_token")
    if (
        not isinstance(request_json, str)
        or isinstance(fence_token, bool)
        or not isinstance(fence_token, int)
        or fence_token < 1
    ):
        raise RuntimeError("mapping publication claim is malformed")
    decoded = json.loads(request_json)
    if not isinstance(decoded, dict):
        raise RuntimeError("mapping census request is malformed")
    request = parse_stored_census_request(decoded)
    if not isinstance(request, (MappingPrepareCensusRequest, MappingRollbackCensusRequest)):
        raise RuntimeError("mapping child received a source census")
    candidate_id, _ = mapping_candidate_identity(request.authority)
    if envelope.revision_id != candidate_id:
        raise RuntimeError("mapping publication candidate conflicts with census")
    return MappingChildClaim(envelope, request, fence_token)


def activation_command(claim: MappingChildClaim) -> CrmTenantActivationCommand:
    authority = claim.request.authority
    if authority.completed_release_id is None or authority.completed_release_fingerprint is None:
        raise RuntimeError("mapping activation requires v2 exact release authority")
    mapping = _mapping_head(authority)
    projection = _projection_head(authority)
    candidate_id, candidate_digest = mapping_candidate_identity(authority)
    return CrmTenantActivationCommand(
        CrmTenantMappingScope(
            claim.request.source_key,
            claim.request.source_instance_id,
            claim.request.control_instance_id,
        ),
        CrmTenantProjectionScope(
            claim.request.source_key,
            claim.request.source_instance_id,
            claim.request.control_instance_id,
        ),
        CrmTenantActivationCandidate(candidate_id, candidate_digest),
        CrmTenantActivationRelease(
            authority.completed_release_id, authority.completed_release_fingerprint
        ),
        mapping,
        projection,
        claim.envelope.census_id,
        claim.envelope.generation,
        claim.envelope.task_id,
    )


def _mapping_head(authority: object) -> CrmTenantMappingExpectedHead | None:
    head_id = getattr(authority, "expected_current_head_id", None)
    revision_id = getattr(authority, "expected_mapping_active_revision_id", None)
    number = getattr(authority, "expected_mapping_active_revision_number", None)
    digest = getattr(authority, "expected_mapping_active_manifest_digest", None)
    if (
        isinstance(head_id, str)
        and isinstance(revision_id, str)
        and isinstance(number, int)
        and isinstance(digest, str)
    ):
        return CrmTenantMappingExpectedHead(head_id, revision_id, number, digest)
    if revision_id is None and number is None and digest is None:
        return None
    raise RuntimeError("mapping activation requires complete mapping predecessor")


def _projection_head(authority: object) -> CrmTenantProjectionExpectedHead | None:
    head_id = getattr(authority, "expected_projection_head_id", None)
    release_id = getattr(authority, "expected_projection_active_release_id", None)
    number = getattr(authority, "expected_projection_active_release_number", None)
    fingerprint = getattr(authority, "expected_projection_active_release_fingerprint", None)
    if (
        isinstance(head_id, str)
        and isinstance(release_id, str)
        and isinstance(number, int)
        and isinstance(fingerprint, str)
    ):
        return CrmTenantProjectionExpectedHead(head_id, release_id, number, fingerprint)
    if release_id is None and number is None and fingerprint is None:
        return None
    raise RuntimeError("mapping activation requires complete projection predecessor")


def _texts(raw: Mapping[str, object], *keys: str) -> tuple[str, str, str, str]:
    values = tuple(raw.get(key) for key in keys)
    if len(values) != 4 or not all(isinstance(value, str) and value for value in values):
        raise ValueError("mapping child payload task identity is invalid")
    census_id, task_id, queue, payload_version = values
    assert isinstance(census_id, str) and isinstance(task_id, str)
    assert isinstance(queue, str) and isinstance(payload_version, str)
    return census_id, task_id, queue, payload_version
