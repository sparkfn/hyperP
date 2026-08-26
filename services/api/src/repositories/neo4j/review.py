"""Neo4j implementation of ReviewRepository."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TypedDict

from neo4j import AsyncManagedTransaction, AsyncSession, Record

from src.celery_client import enqueue_match_recalculation
from src.graph.client import get_session
from src.graph.converters import GraphRecord, to_int, to_optional_str, to_str
from src.graph.crm_deal_count import recompute_person_crm_deal_counts
from src.graph.golden_profile import recompute_golden_profile_tx
from src.graph.mappers import (
    map_possible_match_detail,
    map_review_case_detail,
    map_review_case_summary,
)
from src.graph.queries import (
    ACTIVATE_PENDING_REVIEW_RECORD,
    ASSIGN_REVIEW_CASE,
    CHECK_BOTH_PERSONS_ACTIVE,
    CHECK_NO_MATCH_LOCK,
    CLAIM_PENDING_REVIEW_RESOLUTION,
    CREATE_NO_MATCH_LOCK_FROM_REVIEW,
    EXECUTE_MANUAL_MERGE,
    FINALIZE_STAGED_REVIEW_SALE,
    GET_PENDING_REVIEW_RECORD,
    GET_PERSON_POSSIBLE_MATCH_DETAIL,
    GET_PERSONS_FOR_REVIEW_MERGE,
    GET_REVIEW_CASE,
    GET_REVIEW_CASE_BY_MATCH_DECISION,
    GET_REVIEW_SALES_RECORD,
    LINK_REVIEW_SALES_BOUGHT_VEHICLE,
    LINK_REVIEW_SALES_PURCHASED_ORDER,
    MARK_REVIEW_SALES_RECORD_LINKED,
    MARK_REVIEW_SALES_RECORD_UNRESOLVED,
    PRECHECK_STAGED_REVIEW_SALE,
    PROMOTE_STAGED_REVIEW_SALE,
    RECREATE_REVIEW_CASE,
    REJECT_PENDING_REVIEW_RECORD,
    REJECT_STAGED_REVIEW_SALE,
    RESOLVE_PENDING_REVIEW_RECORD_NO_MATCH,
    build_claimed_review_action_cypher,
    build_count_review_cases_query,
    build_list_review_cases_query,
    build_review_action_cypher,
)
from src.identity_link_revisions import (
    append_identity_link_revisions,
    append_merge_affected_revisions,
)
from src.identity_link_types import (
    IdentityLinkDesiredRevision,
    IdentityLinkResolutionKind,
    IdentityLinkStatus,
)
from src.repositories.neo4j._merge_side_effects import apply_merge_review_side_effects
from src.repositories.neo4j.merge import (
    _apply_golden_profile_selections_tx,
    _ordered_pair,
    are_valid_golden_profile_selections,
)
from src.repositories.neo4j.sales_staging import InvalidSalesStageError, validate_sales_stage
from src.repositories.protocols.merge import GoldenProfileSelection
from src.repositories.protocols.review import ActionResult, AssignResult, ReviewListFilters
from src.types import (
    ApiReviewActionType,
    ReviewCaseDetail,
    ReviewCaseSummary,
    SharedIdentifierGroup,
)

from ._utils import record_to_dict, to_total

# ReviewListFilters keys consumed only when building the query string, never
# bound as Cypher parameters.
_NON_CYPHER_KEYS: frozenset[str] = frozenset({"sort_by", "sort_order"})
logger = logging.getLogger(__name__)


class _ReviewResolutionAbortError(RuntimeError):
    """Force the active Neo4j write transaction to roll back."""


def _action_entry_json(action_type: str, actor_type: str, actor_id: str, notes: str | None) -> str:
    """Serialize one review-action audit entry; rc.actions stores JSON strings."""
    return json.dumps(
        {
            "action_type": action_type,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "notes": notes,
            "created_at": datetime.now(UTC).isoformat(),
        }
    )


class Neo4jReviewRepository:
    async def get_page(
        self, filters: ReviewListFilters, skip: int, limit: int
    ) -> tuple[list[ReviewCaseSummary], int]:
        has_q = filters.get("q") is not None
        has_person = filters.get("person_id") is not None
        active_filters = frozenset(
            key
            for key, value in filters.items()
            if key not in _NON_CYPHER_KEYS
            and key not in {"q", "person_id"}
            and value is not None
            and (key != "overdue_sla" or value is True)
        )
        list_query = build_list_review_cases_query(
            filters.get("sort_by"),
            filters.get("sort_order"),
            has_q=has_q,
            has_person=has_person,
            active_filters=active_filters,
        )
        count_query = build_count_review_cases_query(
            has_q=has_q,
            has_person=has_person,
            active_filters=active_filters,
        )
        cypher_params: dict[str, str | int | float | bool | None] = {
            k: v  # type: ignore[misc]  # TypedDict values are object; known-safe filter keys
            for k, v in filters.items()
            if k not in _NON_CYPHER_KEYS
        }
        list_params = {**cypher_params, "skip": skip, "limit": limit}

        async def _run_list() -> list[GraphRecord]:
            async with get_session() as session:
                result = await session.run(list_query, list_params)
                return [record_to_dict(r.keys(), list(r.values())) async for r in result]

        async def _run_count() -> int:
            async with get_session() as session:
                result = await session.run(count_query, cypher_params)
                record = await result.single()
                return to_total(record)

        records, total = await asyncio.gather(_run_list(), _run_count())
        return [map_review_case_summary(rec) for rec in records], total

    async def get_by_id(self, review_case_id: str) -> ReviewCaseDetail | None:
        async with get_session() as session:
            result = await session.run(GET_REVIEW_CASE, review_case_id=review_case_id)
            record = await result.single()
            if record is None:
                return None
            detail = map_review_case_detail(record_to_dict(record.keys(), list(record.values())))
            detail.shared_identifier_groups = await self._fetch_evidence(session, detail)
            return detail

    async def _fetch_evidence(
        self, session: AsyncSession, detail: ReviewCaseDetail
    ) -> list[SharedIdentifierGroup]:
        left = detail.comparison_left
        right = detail.comparison_right
        if left is None or right is None or left.person_id is None:
            return []
        candidate_person_id = (
            right.person_id
            if right.entity_kind == "person" and right.person_id is not None
            else right.linked_person_id
        )
        if candidate_person_id is None or candidate_person_id == left.person_id:
            return []
        return await session.execute_read(
            self._read_shared_identifier_groups,
            person_id=left.person_id,
            candidate_person_id=candidate_person_id,
        )

    async def _read_shared_identifier_groups(
        self,
        tx: AsyncManagedTransaction,
        *,
        person_id: str,
        candidate_person_id: str,
    ) -> list[SharedIdentifierGroup]:
        result = await tx.run(
            GET_PERSON_POSSIBLE_MATCH_DETAIL,
            person_id=person_id,
            candidate_person_id=candidate_person_id,
        )
        records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        if not records:
            return []
        return map_possible_match_detail(records).shared_identifier_groups

    async def get_by_match_decision_id(self, match_decision_id: str) -> ReviewCaseDetail | None:
        async with get_session() as session:
            id_result = await session.run(
                GET_REVIEW_CASE_BY_MATCH_DECISION,
                match_decision_id=match_decision_id,
            )
            id_record = await id_result.single()
        if id_record is None:
            return None
        return await self.get_by_id(to_str(id_record["review_case_id"]))

    async def recreate(self, review_case_id: str, actor_id: str) -> ReviewCaseDetail | None:
        action_json = _action_entry_json("recreate", "user", actor_id, None)
        async with get_session(write=True) as session:
            result = await session.run(
                RECREATE_REVIEW_CASE,
                review_case_id=review_case_id,
                action_json=action_json,
            )
            record = await result.single()
        if record is None:
            return None
        return await self.get_by_id(to_str(record["review_case_id"]))

    async def assign(self, review_case_id: str, assigned_to: str) -> AssignResult | None:
        async with get_session(write=True) as session:
            record = await session.execute_write(_assign_tx, review_case_id, assigned_to)
        if record is None:
            return None
        return AssignResult(
            review_case_id=to_str(record.get("review_case_id")),
            queue_state=to_str(record.get("queue_state")),
            assigned_to=to_str(record.get("assigned_to")),
        )

    async def submit_action(
        self,
        review_case_id: str,
        action_type: str,
        new_state: str,
        resolution: str | None,
        notes: str | None,
        follow_up_at: str | None,
        actor_id: str,
        survivor_person_id: str | None,
        golden_profile_selections: list[GoldenProfileSelection],
    ) -> ActionResult | None:
        if not are_valid_golden_profile_selections(golden_profile_selections):
            return ActionResult(merge_not_applicable=True)
        async with get_session(write=True) as session:
            try:
                result = await session.execute_write(
                    _action_tx,
                    review_case_id,
                    action_type,
                    new_state,
                    resolution,
                    notes,
                    follow_up_at,
                    actor_id,
                    survivor_person_id,
                    golden_profile_selections,
                )
            except _ReviewResolutionAbortError:
                return ActionResult(merge_not_applicable=True)

        if result is None:
            return None

        enqueue_match_recalculation(result.get("redirected_review_case_ids", []))

        return result


async def _assign_tx(
    tx: AsyncManagedTransaction, review_case_id: str, assigned_to: str
) -> GraphRecord | None:
    result = await tx.run(
        ASSIGN_REVIEW_CASE,
        review_case_id=review_case_id,
        assigned_to=assigned_to,
        action_json=_action_entry_json("assign", "system", assigned_to, None),
    )
    record = await result.single()
    if record is None:
        return None
    return dict(record["review_case"])


class _ReviewClaim(TypedDict):
    claim_token: str
    claim_version: int
    claim_status: str


async def _claim_review_action(
    tx: AsyncManagedTransaction, review_case_id: str, actor_id: str
) -> _ReviewClaim | None:
    result = await tx.run(
        CLAIM_PENDING_REVIEW_RESOLUTION,
        review_case_id=review_case_id,
        actor_id=actor_id,
    )
    record = await result.single()
    if record is None or to_str(record.get("claimed_by")) != actor_id:
        return None
    token = to_str(record.get("claim_token"))
    status = to_str(record.get("claim_status"))
    version = to_int(record.get("claim_version"))
    if not token or not status or version < 1:
        raise _ReviewResolutionAbortError("invalid review action claim")
    return {"claim_token": token, "claim_version": version, "claim_status": status}


async def _sales_link_merge_tx(
    tx: AsyncManagedTransaction,
    review_case_id: str,
    new_state: str,
    resolution: str | None,
    follow_up_at: str | None,
    action_type: str,
    actor_id: str,
    notes: str | None,
) -> ActionResult:
    """Approve a sales-record review case: link Order+Units to the candidate Person."""
    sales_result = await tx.run(GET_REVIEW_SALES_RECORD, review_case_id=review_case_id)
    sales_record = await sales_result.single()
    if sales_record is None:
        return ActionResult(merge_not_applicable=True)
    claim = await _claim_review_action(tx, review_case_id, actor_id)
    if claim is None:
        return ActionResult(merge_not_applicable=True)
    if sales_record.get("lifecycle_status") == "pending_review":
        if not bool(sales_record.get("staged_sales_ready", False)):
            raise _ReviewResolutionAbortError("pending sales review has no complete staging graph")
        precheck_result = await tx.run(
            PRECHECK_STAGED_REVIEW_SALE,
            review_case_id=review_case_id,
            actor_id=actor_id,
            **claim,
        )
        precheck = await precheck_result.single()
        if precheck is None:
            raise _ReviewResolutionAbortError("staged sales promotion failed precheck")
        try:
            validated_stage = validate_sales_stage(precheck)
        except InvalidSalesStageError as exc:
            raise _ReviewResolutionAbortError("staged sales integrity validation failed") from exc
        promoted = await tx.run(
            PROMOTE_STAGED_REVIEW_SALE,
            review_case_id=review_case_id,
            actor_id=actor_id,
            source_lock_version=validated_stage.source_lock_version,
            stage_lock_version=validated_stage.lock_version,
            stage_hash=validated_stage.stage_hash,
            expected_line_count=validated_stage.line_count,
            expected_observation_count=validated_stage.observation_count,
            points_used=validated_stage.points_used,
            points_gained=validated_stage.points_gained,
            **claim,
        )
        promoted_record = await promoted.single()
        if (
            promoted_record is None
            or promoted_record.get("promoted_line_count") != precheck.get("expected_line_count")
            or promoted_record.get("promoted_observation_count")
            != precheck.get("expected_observation_count")
        ):
            raise _ReviewResolutionAbortError("staged sales promotion failed validation")
        finalized = await tx.run(
            FINALIZE_STAGED_REVIEW_SALE,
            review_case_id=review_case_id,
            actor_id=actor_id,
            promoted_line_count=promoted_record.get("promoted_line_count"),
            promoted_observation_count=promoted_record.get("promoted_observation_count"),
            source_lock_version=validated_stage.source_lock_version,
            stage_lock_version=validated_stage.lock_version,
            stage_hash=validated_stage.stage_hash,
            **claim,
        )
        if await finalized.single() is None:
            raise _ReviewResolutionAbortError("staged sales lifecycle transition lost")
    else:
        await tx.run(LINK_REVIEW_SALES_PURCHASED_ORDER, review_case_id=review_case_id)
        await tx.run(LINK_REVIEW_SALES_BOUGHT_VEHICLE, review_case_id=review_case_id)
        linked_result = await tx.run(MARK_REVIEW_SALES_RECORD_LINKED, review_case_id=review_case_id)
        if await linked_result.single() is None:
            return ActionResult(merge_not_applicable=True)
    cypher = build_claimed_review_action_cypher(resolution, follow_up_at)
    rc_result = await tx.run(
        cypher,
        review_case_id=review_case_id,
        new_state=new_state,
        resolution=resolution,
        follow_up_at=follow_up_at,
        action_json=_action_entry_json(action_type, "reviewer", actor_id, notes),
        actor_id=actor_id,
        **claim,
    )
    rc_record = await rc_result.single()
    if rc_record is None:
        raise _ReviewResolutionAbortError("review close lost after sales activation")
    rc = dict(rc_record["review_case"])
    return ActionResult(
        review_case_id=to_str(rc.get("review_case_id")),
        queue_state=to_str(rc.get("queue_state")),
        resolution=to_optional_str(rc.get("resolution")),
    )


def _projection_items(
    payload: Mapping[str, object], key: str, required_strings: tuple[str, ...]
) -> list[dict[str, object]]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"normalized_payload.{key} must be a list")
    items: list[dict[str, object]] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError(f"normalized_payload.{key} entries must be objects")
        item = {str(k): v for k, v in raw.items()}
        if any(not isinstance(item.get(field), str) for field in required_strings):
            raise ValueError(f"normalized_payload.{key} entry has invalid fields")
        items.append(item)
    return items


def _normalized_datetime(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.isoformat()


def _optional_normalized_datetime(value: object, field: str) -> str | None:
    return None if value is None else _normalized_datetime(value, field)


def _pending_projection_params(
    value: object,
    *,
    source_system_key: str,
    source_record_id: str,
    source_record_pk: str,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    parsed: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ValueError("normalized_payload must be a JSON object")
    payload = {str(k): v for k, v in parsed.items()}
    identifiers = _projection_items(
        payload, "identifiers", ("identifier_type", "normalized_value", "quality_flag")
    )
    for identifier in identifiers:
        if not isinstance(identifier.get("is_verified"), bool):
            raise ValueError("normalized_payload identifier is_verified must be boolean")
    identifiers = [
        item
        for item in identifiers
        if item["quality_flag"] not in {"invalid_format", "placeholder_value"}
    ]
    addresses = _projection_items(
        payload,
        "addresses",
        ("country_code", "postal_code", "street_name", "street_number", "quality_flag"),
    )
    for address in addresses:
        unit = address.get("unit_number")
        if unit is not None and not isinstance(unit, str):
            raise ValueError("normalized_payload address unit_number must be a string or null")
        address["unit_number"] = unit or ""
        normalized = address.get("normalized_full")
        if normalized is not None and not isinstance(normalized, str):
            raise ValueError("normalized_payload address normalized_full must be a string or null")
    addresses = [
        item
        for item in addresses
        if item["quality_flag"] not in {"invalid_format", "placeholder_value"}
    ]
    attributes = _projection_items(
        payload, "attributes", ("attribute_name", "attribute_value", "quality_flag")
    )
    bankruptcy_cases: list[dict[str, object]] = []
    bankruptcy = payload.get("bankruptcy_case")
    if bankruptcy is not None:
        if not isinstance(bankruptcy, dict):
            raise ValueError("normalized_payload.bankruptcy_case must be an object")
        item = {str(k): v for k, v in bankruptcy.items()}
        required = ("source_system_key", "source_case_id", "observed_at", "raw_payload")
        if any(not isinstance(item.get(field), str) for field in required):
            raise ValueError("bankruptcy_case required fields must be strings")
        if item["source_system_key"] != source_system_key:
            raise ValueError("bankruptcy_case source provenance mismatch")
        bankruptcy_optional = (
            "case_number",
            "document_type",
            "document_date",
            "event_type",
            "event_date",
            "trustee_name",
            "trustee_firm",
            "source_url",
            "first_seen_at",
            "last_seen_at",
        )
        if any(
            item.get(field) is not None and not isinstance(item.get(field), str)
            for field in bankruptcy_optional
        ):
            raise ValueError("bankruptcy_case optional fields must be strings or null")
        item["observed_at"] = _normalized_datetime(
            item["observed_at"], "bankruptcy_case.observed_at"
        )
        item["first_seen_at"] = _optional_normalized_datetime(
            item.get("first_seen_at"), "bankruptcy_case.first_seen_at"
        )
        item["last_seen_at"] = _optional_normalized_datetime(
            item.get("last_seen_at"), "bankruptcy_case.last_seen_at"
        )
        bankruptcy_cases.append(item)
    vehicle_mentions = _projection_items(
        payload, "vehicle_mentions", ("source_system_key", "source_record_id", "quality_flag")
    )
    for mention in vehicle_mentions:
        if (
            mention["source_system_key"] != source_system_key
            or mention["source_record_id"] != source_record_id
            or (
                mention.get("source_record_pk") is not None
                and mention.get("source_record_pk") != source_record_pk
            )
        ):
            raise ValueError("vehicle mention source provenance mismatch")
        vehicle_optional = (
            "normalized_lta_tag",
            "normalized_serial_number",
            "product",
            "raw_context",
            "observed_at",
        )
        if any(
            mention.get(field) is not None and not isinstance(mention.get(field), str)
            for field in vehicle_optional
        ):
            raise ValueError("vehicle mention optional fields must be strings or null")
        confidence = mention.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise ValueError("vehicle mention confidence must be numeric")
        if mention.get("observed_at") is not None:
            mention["observed_at"] = _normalized_datetime(
                mention["observed_at"], "vehicle_mentions.observed_at"
            )
    knows_relationships = _projection_items(
        payload,
        "knows_relationships",
        (
            "declarer_source_record_id",
            "declarer_source_system_key",
            "relationship_category",
            "status",
            "source_system_key",
        ),
    )
    for relationship in knows_relationships:
        if relationship["source_system_key"] != source_system_key:
            raise ValueError("KNOWS relationship source provenance mismatch")
        for field in ("relationship_label", "approved_at"):
            if relationship.get(field) is not None and not isinstance(relationship.get(field), str):
                raise ValueError(f"KNOWS relationship {field} must be a string or null")
    return (
        identifiers,
        addresses,
        attributes,
        bankruptcy_cases,
        vehicle_mentions,
        knows_relationships,
    )


def _required_str(record: Mapping[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"pending review record has invalid {key}")
    return value


def _optional_str_value(record: Mapping[str, object], key: str) -> str | None:
    value = record.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"pending review record has invalid {key}")
    return value


def _str_list_value(record: Mapping[str, object], key: str) -> list[str]:
    value = record.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"pending review record has invalid {key}")
    return list(value)


def _pending_link_revision(
    pending: Mapping[str, object],
    *,
    link_status: IdentityLinkStatus,
    resolution_kind: IdentityLinkResolutionKind,
    cause_key: str,
    match_decision_id: str | None = None,
    review_case_id: str | None = None,
    person_id: str | None = None,
) -> IdentityLinkDesiredRevision | None:
    source_system = pending.get("source_system_key")
    source_instance_id = pending.get("source_instance_id")
    source_entity_type = pending.get("source_entity_type")
    source_entity_id = pending.get("source_entity_id")
    identity_policy_version = pending.get("identity_policy_version")
    effective_at = pending.get("observed_at")
    values = (
        source_system,
        source_instance_id,
        source_entity_type,
        source_entity_id,
        identity_policy_version,
        effective_at,
    )
    if not all(isinstance(value, str) and value for value in values):
        return None
    assert isinstance(source_system, str)
    assert isinstance(source_instance_id, str)
    assert isinstance(source_entity_type, str)
    assert isinstance(source_entity_id, str)
    assert isinstance(identity_policy_version, str)
    assert isinstance(effective_at, str)
    if link_status != "resolved":
        person_id = None
    if link_status == "resolved" and person_id is None:
        return None
    return IdentityLinkDesiredRevision(
        source_system=source_system,
        source_instance_id=source_instance_id,
        source_entity_type=source_entity_type,
        source_entity_id=source_entity_id,
        identity_policy_version=identity_policy_version,
        link_status=link_status,
        hyperp_person_id=person_id,
        resolution_kind=resolution_kind,
        effective_at=effective_at,
        cause_key=cause_key,
        match_decision_id=match_decision_id,
        review_case_id=review_case_id,
    )


async def _pending_record_merge_tx(
    tx: AsyncManagedTransaction,
    review_case_id: str,
    pending_record: Mapping[str, object],
    survivor_person_id: str | None,
    new_state: str,
    resolution: str | None,
    follow_up_at: str | None,
    actor_id: str,
    notes: str | None,
) -> ActionResult | None:
    try:
        proposed_person_id = _required_str(pending_record, "proposed_person_id")
        review_candidate_person_ids = _str_list_value(pending_record, "review_candidate_person_ids")
        pending_source_record_pk = _required_str(pending_record, "pending_source_record_pk")
        source_system_key = _required_str(pending_record, "source_system_key")
        source_record_id = _required_str(pending_record, "source_record_id")
        expected_active_source_record_pk = _optional_str_value(
            pending_record, "expected_active_source_record_pk"
        )
        observed_at = _normalized_datetime(pending_record.get("observed_at"), "observed_at")
        (
            identifiers,
            addresses,
            attributes,
            bankruptcy_cases,
            vehicle_mentions,
            knows_relationships,
        ) = _pending_projection_params(
            pending_record.get("normalized_payload"),
            source_system_key=source_system_key,
            source_record_id=source_record_id,
            source_record_pk=pending_source_record_pk,
        )
    except (TypeError, ValueError):
        logger.warning(
            "Invalid pending review blueprint review_case_id=%s source_record_pk=%s",
            review_case_id,
            pending_record.get("pending_source_record_pk"),
        )
        return ActionResult(merge_not_applicable=True)
    if review_candidate_person_ids:
        selected_person_id = survivor_person_id or proposed_person_id
        if selected_person_id not in review_candidate_person_ids:
            return ActionResult(merge_not_applicable=True)
        proposed_person_id = selected_person_id
    elif survivor_person_id is not None and survivor_person_id != proposed_person_id:
        return ActionResult(merge_not_applicable=True)
    claim = await _claim_review_action(tx, review_case_id, actor_id)
    if claim is None:
        return ActionResult(merge_not_applicable=True)
    activated_result = await tx.run(
        ACTIVATE_PENDING_REVIEW_RECORD,
        review_case_id=review_case_id,
        pending_source_record_pk=pending_source_record_pk,
        source_system_key=source_system_key,
        expected_active_source_record_pk=expected_active_source_record_pk,
        approved_person_id=proposed_person_id,
        observed_at=observed_at,
        identifiers=identifiers,
        addresses=addresses,
        attributes=attributes,
        bankruptcy_cases=bankruptcy_cases,
        vehicle_mentions=vehicle_mentions,
        knows_relationships=knows_relationships,
    )
    activated = await activated_result.single()
    if activated is None:
        raise _ReviewResolutionAbortError("pending activation lost after review claim")
    affected = activated.get("affected_person_ids", [])
    if isinstance(affected, list):
        affected_person_ids = sorted({value for value in affected if isinstance(value, str)})
        await recompute_person_crm_deal_counts(tx, affected_person_ids)
        for person_id in affected_person_ids:
            await recompute_golden_profile_tx(tx, person_id, False)
    revision = _pending_link_revision(
        pending_record,
        link_status="resolved",
        resolution_kind="reviewed_activation",
        cause_key=f"reviewed-activation:{review_case_id}:{pending_source_record_pk}",
        match_decision_id=_optional_str_value(pending_record, "match_decision_id"),
        review_case_id=review_case_id,
        person_id=proposed_person_id,
    )
    if revision is not None:
        await append_identity_link_revisions(tx, [revision])
    action_result = await tx.run(
        build_claimed_review_action_cypher(resolution, follow_up_at),
        review_case_id=review_case_id,
        new_state=new_state,
        resolution=resolution,
        follow_up_at=follow_up_at,
        action_json=_action_entry_json("merge", "reviewer", actor_id, notes),
        actor_id=actor_id,
        **claim,
    )
    action_record = await action_result.single()
    if action_record is None:
        raise _ReviewResolutionAbortError("review close lost after lifecycle activation")
    review_case = dict(action_record["review_case"])
    return ActionResult(
        review_case_id=to_str(review_case.get("review_case_id")),
        queue_state=to_str(review_case.get("queue_state")),
        resolution=to_optional_str(review_case.get("resolution")),
        redirected_review_case_ids=[],
    )


async def _action_tx(
    tx: AsyncManagedTransaction,
    review_case_id: str,
    action_type: str,
    new_state: str,
    resolution: str | None,
    notes: str | None,
    follow_up_at: str | None,
    actor_id: str,
    survivor_person_id: str | None,
    golden_profile_selections: list[GoldenProfileSelection],
) -> ActionResult | None:
    absorbed_id: str | None = None
    survivor_id: str | None = None
    lifecycle_mutated = False
    claim: _ReviewClaim | None = None
    preloaded_action_record: Record | None = None
    record: Record | GraphRecord | None

    if action_type == ApiReviewActionType.MANUAL_NO_MATCH.value:
        pending_result = await tx.run(GET_PENDING_REVIEW_RECORD, review_case_id=review_case_id)
        pending_record = await pending_result.single()
        if pending_record is not None and isinstance(
            pending_record.get("pending_source_record_pk"), str
        ):
            claim = await _claim_review_action(tx, review_case_id, actor_id)
            if claim is None:
                return ActionResult(merge_not_applicable=True)
            no_match_result = await tx.run(
                RESOLVE_PENDING_REVIEW_RECORD_NO_MATCH,
                review_case_id=review_case_id,
            )
            no_match_record = await no_match_result.single()
            if no_match_record is None:
                raise _ReviewResolutionAbortError("pending source no-match lost after review claim")
            revision = _pending_link_revision(
                pending_record,
                link_status="unresolved",
                resolution_kind="manual_no_match",
                cause_key=(
                    f"manual-no-match:{review_case_id}:"
                    f"{no_match_record['pending_source_record_pk']}"
                ),
                review_case_id=review_case_id,
            )
            if revision is not None:
                await append_identity_link_revisions(tx, [revision])
            lifecycle_mutated = True
        elif pending_record is not None and isinstance(pending_record.get("review_case"), Mapping):
            preloaded_action_record = pending_record

    if action_type == ApiReviewActionType.REJECT.value:
        pending_result = await tx.run(GET_PENDING_REVIEW_RECORD, review_case_id=review_case_id)
        pending_record = await pending_result.single()
        if pending_record is not None:
            claim = await _claim_review_action(tx, review_case_id, actor_id)
            if claim is None:
                return ActionResult(merge_not_applicable=True)
            rejected_result = await tx.run(
                REJECT_PENDING_REVIEW_RECORD,
                review_case_id=review_case_id,
                reason=notes or "Rejected by reviewer",
            )
            if await rejected_result.single() is None:
                raise _ReviewResolutionAbortError("pending rejection lost after review claim")
            revision = _pending_link_revision(
                pending_record,
                link_status="rejected",
                resolution_kind="review_rejection",
                cause_key=f"review-rejection:{review_case_id}",
                review_case_id=review_case_id,
            )
            if revision is not None:
                await append_identity_link_revisions(tx, [revision])
            lifecycle_mutated = True
        else:
            sales_result = await tx.run(GET_REVIEW_SALES_RECORD, review_case_id=review_case_id)
            sales_record = await sales_result.single()
            if sales_record is not None:
                claim = await _claim_review_action(tx, review_case_id, actor_id)
                if claim is None:
                    return ActionResult(merge_not_applicable=True)
                if sales_record.get("lifecycle_status") == "pending_review":
                    rejected = await tx.run(
                        REJECT_STAGED_REVIEW_SALE,
                        review_case_id=review_case_id,
                        actor_id=actor_id,
                        **claim,
                    )
                    if await rejected.single() is None:
                        raise _ReviewResolutionAbortError("staged sales rejection failed")
                else:
                    await tx.run(
                        MARK_REVIEW_SALES_RECORD_UNRESOLVED,
                        review_case_id=review_case_id,
                    )
                lifecycle_mutated = True

    if action_type == ApiReviewActionType.MERGE.value:
        persons_result = await tx.run(GET_PERSONS_FOR_REVIEW_MERGE, review_case_id=review_case_id)
        persons_record = await persons_result.single()
        if persons_record is None:
            pending_result = await tx.run(GET_PENDING_REVIEW_RECORD, review_case_id=review_case_id)
            pending_record = await pending_result.single()
            if pending_record is None:
                return await _sales_link_merge_tx(
                    tx,
                    review_case_id,
                    new_state,
                    resolution,
                    follow_up_at,
                    action_type,
                    actor_id,
                    notes,
                )
            return await _pending_record_merge_tx(
                tx,
                review_case_id,
                pending_record,
                survivor_person_id,
                new_state,
                resolution,
                follow_up_at,
                actor_id,
                notes,
            )

        left_id = to_str(persons_record["left_person_id"])
        right_id = to_str(persons_record["right_person_id"])

        if survivor_person_id == right_id:
            survivor_id, absorbed_id = right_id, left_id
        elif survivor_person_id == left_id:
            survivor_id, absorbed_id = left_id, right_id
        elif survivor_person_id is None:
            # Default to whichever person has more golden fields filled in;
            # fall back to left on a tie.
            left_score = to_int(persons_record["left_completion"])
            right_score = to_int(persons_record["right_completion"])
            if right_score > left_score:
                survivor_id, absorbed_id = right_id, left_id
            else:
                survivor_id, absorbed_id = left_id, right_id
        else:
            return ActionResult(merge_not_applicable=True)

        active_result = await tx.run(
            CHECK_BOTH_PERSONS_ACTIVE, from_id=absorbed_id, to_id=survivor_id
        )
        if await active_result.single() is None:
            return ActionResult(merge_not_applicable=True)

        lock_left, lock_right = (
            (absorbed_id, survivor_id) if absorbed_id < survivor_id else (survivor_id, absorbed_id)
        )
        lock_result = await tx.run(CHECK_NO_MATCH_LOCK, left=lock_left, right=lock_right)
        lock_record = await lock_result.single()
        if lock_record is not None and bool(lock_record["is_locked"]):
            return ActionResult(merge_blocked=True)

    cypher = (
        build_claimed_review_action_cypher(resolution, follow_up_at)
        if lifecycle_mutated
        else build_review_action_cypher(resolution, follow_up_at)
    )
    if preloaded_action_record is not None:
        record = preloaded_action_record
    elif lifecycle_mutated:
        if claim is None:
            raise _ReviewResolutionAbortError("missing review action claim")
        result = await tx.run(
            cypher,
            review_case_id=review_case_id,
            new_state=new_state,
            resolution=resolution,
            follow_up_at=follow_up_at,
            action_json=_action_entry_json(action_type, "reviewer", actor_id, notes),
            actor_id=actor_id,
            claim_token=claim["claim_token"],
            claim_version=claim["claim_version"],
            claim_status=claim["claim_status"],
        )
        record = await result.single()
    else:
        result = await tx.run(
            cypher,
            review_case_id=review_case_id,
            new_state=new_state,
            resolution=resolution,
            follow_up_at=follow_up_at,
            action_json=_action_entry_json(action_type, "reviewer", actor_id, notes),
        )
        record = await result.single()
    if record is None:
        if lifecycle_mutated:
            raise _ReviewResolutionAbortError("review close lost after lifecycle rejection")
        return None

    raw_review_case = record["review_case"]
    if not isinstance(raw_review_case, Mapping):
        raise _ReviewResolutionAbortError("review close returned invalid review case")
    rc = dict(raw_review_case)
    out = ActionResult(
        review_case_id=to_str(rc.get("review_case_id")),
        queue_state=to_str(rc.get("queue_state")),
        resolution=to_optional_str(rc.get("resolution")),
        redirected_review_case_ids=[],
    )

    if action_type == ApiReviewActionType.MANUAL_NO_MATCH.value and not lifecycle_mutated:
        await tx.run(
            CREATE_NO_MATCH_LOCK_FROM_REVIEW,
            review_case_id=review_case_id,
            notes=notes or "Manual no-match from review",
            actor_id=actor_id,
        )
    elif action_type == ApiReviewActionType.MERGE.value and absorbed_id and survivor_id:
        left, right = _ordered_pair(absorbed_id, survivor_id)
        merge_result = await tx.run(
            EXECUTE_MANUAL_MERGE,
            from_id=absorbed_id,
            to_id=survivor_id,
            left=left,
            right=right,
            reason=notes or "Review merge",
            actor_id=actor_id,
        )
        merge_record = await merge_result.single()
        if merge_record is None:
            raise _ReviewResolutionAbortError("person merge lost after review close")
        merge_event_id = to_str(merge_record["merge_event_id"])
        await recompute_person_crm_deal_counts(tx, [absorbed_id, survivor_id])
        redirected_ids = await apply_merge_review_side_effects(
            tx, merge_event_id, absorbed_id, survivor_id
        )
        await append_merge_affected_revisions(
            tx,
            merge_event_id=merge_event_id,
            survivor_person_id=survivor_id,
            resolution_kind="person_merge",
            cause_prefix=f"person-merge:{merge_event_id}",
            effective_at=to_str(merge_record["created_at"]),
        )
        out["redirected_review_case_ids"] = redirected_ids
        await recompute_golden_profile_tx(tx, survivor_id, False)
        if golden_profile_selections:
            await _apply_golden_profile_selections_tx(
                tx,
                survivor_id,
                golden_profile_selections,
            )
        out["survivor_person_id"] = survivor_id
        out["golden_profile_selections"] = golden_profile_selections

    if (
        action_type in (ApiReviewActionType.MANUAL_NO_MATCH.value, ApiReviewActionType.REJECT.value)
        and not lifecycle_mutated
    ):
        await tx.run(MARK_REVIEW_SALES_RECORD_UNRESOLVED, review_case_id=review_case_id)

    return out
