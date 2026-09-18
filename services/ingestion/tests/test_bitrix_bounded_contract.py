"""Bounded Bitrix change-feed contract parsing and validation tests."""

from __future__ import annotations

import pytest
from pydantic.types import JsonValue
from src.connectors.bitrix_openlines.bounded_contract import (
    CAPABILITY_ENDPOINT,
    CHANGED_CONVERSATIONS_ENDPOINT,
    CHANGED_DEALS_ENDPOINT,
    CONTRACT_VERSION,
    BitrixBoundedCapabilityError,
    BitrixBoundedContractError,
    CapabilitySnapshot,
    ChangedConversationsPage,
    ChangedDealsPage,
    ConversationChangeOrder,
    DealChangeOrder,
    parse_capability_snapshot,
    parse_changed_conversations_page,
    parse_changed_deals_page,
    required_capability,
    validate_capability_snapshot,
)

_SOURCE_SCOPE = "scope-a"
_SNAPSHOT_ID = "snap-a"
_MAX_CHANGES = 10
_DEAL_PAYLOAD: dict[str, JsonValue] = {"id": 10, "title": "deal"}
_MESSAGE_PAYLOAD: dict[str, JsonValue] = {"chat_id": 20, "text": "hello"}


def capability_payload(
    *,
    contract_version: JsonValue = CONTRACT_VERSION,
    source_scope: JsonValue = _SOURCE_SCOPE,
    snapshot_id: JsonValue = _SNAPSHOT_ID,
    capabilities: JsonValue = None,
    cursor_ttl_seconds: JsonValue = 600,
) -> dict[str, JsonValue]:
    """Build one capability response; ``None`` capabilities means a single deal capability."""
    selected: JsonValue = capabilities
    if selected is None:
        selected = ["changed_deals_v1"]
    return {
        "contract_version": contract_version,
        "source_scope": source_scope,
        "snapshot_id": snapshot_id,
        "capabilities": selected,
        "cursor_ttl_seconds": cursor_ttl_seconds,
    }


def deal_change(
    *,
    change_version: JsonValue = 1,
    deal_id: JsonValue = 10,
    event_id: JsonValue = "evt-1",
    kind: JsonValue = "upsert",
    revision: JsonValue = "rev-1",
    category_id: JsonValue = "cat-1",
    payload: JsonValue = _DEAL_PAYLOAD,
) -> dict[str, JsonValue]:
    """Build one changed-deal item; pass ``None`` to exercise the kind body rules."""
    return {
        "change_version": change_version,
        "deal_id": deal_id,
        "event_id": event_id,
        "kind": kind,
        "revision": revision,
        "category_id": category_id,
        "payload": payload,
    }


def deal_page(
    *,
    snapshot_id: JsonValue = _SNAPSHOT_ID,
    has_more: JsonValue = False,
    next_cursor: JsonValue = None,
    changes: JsonValue = None,
) -> dict[str, JsonValue]:
    """Build one changed-deal page; ``None`` changes means a single upsert."""
    selected: JsonValue = changes
    if selected is None:
        selected = [deal_change()]
    return {
        "snapshot_id": snapshot_id,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "changes": selected,
    }


def conversation_change(
    *,
    change_version: JsonValue = 1,
    chat_id: JsonValue = 20,
    event_id: JsonValue = "evt-1",
    kind: JsonValue = "upsert",
    revision: JsonValue = "rev-1",
    message_cursor: JsonValue = "msg-1",
    payload: JsonValue = _MESSAGE_PAYLOAD,
) -> dict[str, JsonValue]:
    """Build one changed-conversation item; pass ``None`` to exercise the body rules."""
    return {
        "change_version": change_version,
        "chat_id": chat_id,
        "event_id": event_id,
        "kind": kind,
        "revision": revision,
        "message_cursor": message_cursor,
        "payload": payload,
    }


def conversation_page(
    *,
    snapshot_id: JsonValue = _SNAPSHOT_ID,
    has_more: JsonValue = False,
    next_cursor: JsonValue = None,
    changes: JsonValue = None,
) -> dict[str, JsonValue]:
    """Build one changed-conversation page; ``None`` changes means a single upsert."""
    selected: JsonValue = changes
    if selected is None:
        selected = [conversation_change()]
    return {
        "snapshot_id": snapshot_id,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "changes": selected,
    }


def parse_deal_page(value: JsonValue, *, max_changes: int = _MAX_CHANGES) -> ChangedDealsPage:
    """Parse one changed-deal page against the fixed test snapshot."""
    return parse_changed_deals_page(
        value,
        expected_snapshot_id=_SNAPSHOT_ID,
        max_changes=max_changes,
    )


def parse_conversation_page(
    value: JsonValue,
    *,
    max_changes: int = _MAX_CHANGES,
) -> ChangedConversationsPage:
    """Parse one changed-conversation page against the fixed test snapshot."""
    return parse_changed_conversations_page(
        value,
        expected_snapshot_id=_SNAPSHOT_ID,
        max_changes=max_changes,
    )


def test_frozen_contract_constants_are_stable() -> None:
    assert CONTRACT_VERSION == "bitrix_bounded_v1"
    assert CAPABILITY_ENDPOINT == "hyperp.capabilities.get"
    assert CHANGED_DEALS_ENDPOINT == "hyperp.deals.changed.list"
    assert CHANGED_CONVERSATIONS_ENDPOINT == "hyperp.openlines.changed.list"


def test_bounded_contract_errors_have_the_documented_base_types() -> None:
    assert issubclass(BitrixBoundedContractError, ValueError)
    assert not issubclass(BitrixBoundedContractError, RuntimeError)
    assert issubclass(BitrixBoundedCapabilityError, RuntimeError)
    assert not issubclass(BitrixBoundedCapabilityError, ValueError)


@pytest.mark.parametrize(
    ("stream_key", "capability"),
    [
        ("crm_deals", "changed_deals_v1"),
        ("openlines_conversations", "changed_conversations_v1"),
        ("crm_stage_history", "stage_history_artifact_v1"),
    ],
)
def test_required_capability_names_the_exact_proxy_capability(
    stream_key: str,
    capability: str,
) -> None:
    assert required_capability(stream_key) == capability  # type: ignore[arg-type]


def test_required_capability_rejects_an_unknown_stream_key() -> None:
    with pytest.raises(KeyError):
        required_capability("not_a_stream")  # type: ignore[arg-type]


def test_parse_capability_snapshot_returns_a_frozen_capability_set() -> None:
    snapshot = parse_capability_snapshot(
        capability_payload(capabilities=["changed_deals_v1", "changed_conversations_v1"]),
    )
    assert snapshot == CapabilitySnapshot(
        source_scope=_SOURCE_SCOPE,
        snapshot_id=_SNAPSHOT_ID,
        capabilities=frozenset({"changed_deals_v1", "changed_conversations_v1"}),
        cursor_ttl_seconds=600,
    )


def test_parse_capability_snapshot_rejects_a_non_object_response() -> None:
    with pytest.raises(BitrixBoundedContractError, match="capability response must be an object"):
        parse_capability_snapshot([])


@pytest.mark.parametrize("contract_version", ["bitrix_bounded_v2", "BITRIX_BOUNDED_V1", "v2"])
def test_parse_capability_snapshot_rejects_an_unsupported_contract_version(
    contract_version: JsonValue,
) -> None:
    with pytest.raises(BitrixBoundedContractError, match="unsupported contract version"):
        parse_capability_snapshot(capability_payload(contract_version=contract_version))


@pytest.mark.parametrize("contract_version", ["", "   ", None, 1])
def test_parse_capability_snapshot_rejects_a_blank_contract_version(
    contract_version: JsonValue,
) -> None:
    with pytest.raises(BitrixBoundedContractError, match="contract_version must be"):
        parse_capability_snapshot(capability_payload(contract_version=contract_version))


def test_parse_capability_snapshot_rejects_an_unexpected_field() -> None:
    payload = capability_payload()
    payload["extra"] = "unexpected"
    with pytest.raises(BitrixBoundedContractError, match="unexpected or missing fields"):
        parse_capability_snapshot(payload)


@pytest.mark.parametrize(
    "field",
    ["contract_version", "source_scope", "snapshot_id", "capabilities", "cursor_ttl_seconds"],
)
def test_parse_capability_snapshot_rejects_a_missing_field(field: str) -> None:
    payload = capability_payload()
    del payload[field]
    with pytest.raises(BitrixBoundedContractError, match="unexpected or missing fields"):
        parse_capability_snapshot(payload)


def test_parse_capability_snapshot_rejects_duplicate_capabilities() -> None:
    payload = capability_payload(capabilities=["changed_deals_v1", "changed_deals_v1"])
    with pytest.raises(BitrixBoundedContractError, match="capabilities contains duplicates"):
        parse_capability_snapshot(payload)


@pytest.mark.parametrize("capabilities", ["changed_deals_v1", {"changed_deals_v1": 1}, 1])
def test_parse_capability_snapshot_rejects_non_list_capabilities(
    capabilities: JsonValue,
) -> None:
    with pytest.raises(BitrixBoundedContractError, match="capabilities must be a list"):
        parse_capability_snapshot(capability_payload(capabilities=capabilities))


@pytest.mark.parametrize("capabilities", [[1], [""], ["  "], [None], [["nested"]]])
def test_parse_capability_snapshot_rejects_a_non_string_capability(
    capabilities: JsonValue,
) -> None:
    with pytest.raises(BitrixBoundedContractError, match="must be a non-empty string"):
        parse_capability_snapshot(capability_payload(capabilities=capabilities))


@pytest.mark.parametrize("ttl", [0, -1, True, "600", 1.5, None])
def test_parse_capability_snapshot_rejects_a_non_positive_cursor_ttl(ttl: JsonValue) -> None:
    with pytest.raises(BitrixBoundedContractError, match="cursor_ttl_seconds"):
        parse_capability_snapshot(capability_payload(cursor_ttl_seconds=ttl))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_scope", ""),
        ("source_scope", "   "),
        ("source_scope", 5),
        ("snapshot_id", ""),
        ("snapshot_id", "   "),
        ("snapshot_id", None),
    ],
)
def test_parse_capability_snapshot_rejects_blank_scope_or_snapshot(
    field: str,
    value: JsonValue,
) -> None:
    payload = capability_payload()
    payload[field] = value
    with pytest.raises(BitrixBoundedContractError, match="must be a non-empty string"):
        parse_capability_snapshot(payload)


def test_validate_capability_snapshot_accepts_a_matching_snapshot() -> None:
    snapshot = parse_capability_snapshot(capability_payload())
    assert (
        validate_capability_snapshot(
            snapshot,
            source_scope=_SOURCE_SCOPE,
            snapshot_id=_SNAPSHOT_ID,
            stream_key="crm_deals",
        )
        is None
    )


def test_validate_capability_snapshot_rejects_a_source_scope_mismatch() -> None:
    snapshot = parse_capability_snapshot(capability_payload())
    with pytest.raises(BitrixBoundedCapabilityError, match="source scope mismatch"):
        validate_capability_snapshot(
            snapshot,
            source_scope="other-scope",
            snapshot_id=_SNAPSHOT_ID,
            stream_key="crm_deals",
        )


def test_validate_capability_snapshot_rejects_a_snapshot_mismatch() -> None:
    snapshot = parse_capability_snapshot(capability_payload())
    with pytest.raises(BitrixBoundedCapabilityError, match="capability snapshot mismatch"):
        validate_capability_snapshot(
            snapshot,
            source_scope=_SOURCE_SCOPE,
            snapshot_id="other-snapshot",
            stream_key="crm_deals",
        )


def test_validate_capability_snapshot_rejects_a_missing_stream_capability() -> None:
    snapshot = parse_capability_snapshot(capability_payload(capabilities=["other_v1"]))
    with pytest.raises(BitrixBoundedCapabilityError, match="missing for stream crm_deals"):
        validate_capability_snapshot(
            snapshot,
            source_scope=_SOURCE_SCOPE,
            snapshot_id=_SNAPSHOT_ID,
            stream_key="crm_deals",
        )


def test_parse_changed_deals_page_returns_order_and_continuation() -> None:
    page = parse_deal_page(
        deal_page(
            has_more=True,
            next_cursor="cursor-2",
            changes=[deal_change(deal_id=10, event_id="evt-1"), deal_change(deal_id=11)],
        ),
    )
    assert isinstance(page, ChangedDealsPage)
    assert page.snapshot_id == _SNAPSHOT_ID
    assert page.next_cursor == "cursor-2"
    assert [change.order for change in page.changes] == [
        DealChangeOrder(1, 10, "evt-1"),
        DealChangeOrder(1, 11, "evt-1"),
    ]
    assert page.changes[0].kind == "upsert"
    assert page.changes[0].revision == "rev-1"
    assert page.changes[0].category_id == "cat-1"
    assert page.changes[0].payload == _DEAL_PAYLOAD


def test_parse_changed_deals_page_accepts_an_empty_terminal_page() -> None:
    page = parse_deal_page(deal_page(changes=[]))
    assert page.changes == ()
    assert page.next_cursor is None


def test_deal_change_order_is_version_then_deal_then_event() -> None:
    assert DealChangeOrder(1, 10, "evt-1") == DealChangeOrder(
        change_version=1,
        deal_id=10,
        event_id="evt-1",
    )
    assert DealChangeOrder(1, 10, "evt-1") < DealChangeOrder(2, 1, "evt-0")
    assert DealChangeOrder(1, 10, "evt-1") < DealChangeOrder(1, 11, "evt-0")
    assert DealChangeOrder(1, 10, "evt-1") < DealChangeOrder(1, 10, "evt-2")


@pytest.mark.parametrize(
    "orders",
    [
        ((1, 10, "evt-1"), (1, 10, "evt-1")),
        ((2, 10, "evt-1"), (1, 10, "evt-1")),
        ((1, 10, "evt-1"), (1, 9, "evt-1")),
        ((1, 10, "evt-2"), (1, 10, "evt-1")),
    ],
)
def test_parse_changed_deals_page_rejects_orders_that_do_not_advance(
    orders: tuple[tuple[int, int, str], ...],
) -> None:
    changes = [
        deal_change(change_version=version, deal_id=deal_id, event_id=event_id)
        for version, deal_id, event_id in orders
    ]
    with pytest.raises(BitrixBoundedContractError, match="order did not strictly advance"):
        parse_deal_page(deal_page(changes=changes))


def test_parse_changed_deals_page_rejects_a_snapshot_mismatch() -> None:
    with pytest.raises(BitrixBoundedContractError, match="changed-deal page snapshot mismatch"):
        parse_deal_page(deal_page(snapshot_id="other-snapshot"))


def test_parse_changed_deals_page_rejects_a_page_missing_fields() -> None:
    payload = deal_page()
    del payload["has_more"]
    with pytest.raises(BitrixBoundedContractError, match="unexpected or missing fields"):
        parse_deal_page(payload)


def test_parse_changed_deals_page_rejects_a_non_list_changes_field() -> None:
    with pytest.raises(BitrixBoundedContractError, match="changed-deal changes must be a list"):
        parse_deal_page(deal_page(changes="evt-1"))


def test_parse_changed_deals_page_rejects_has_more_without_a_cursor() -> None:
    with pytest.raises(BitrixBoundedContractError, match="continuation fields disagree"):
        parse_deal_page(deal_page(has_more=True, next_cursor=None))


def test_parse_changed_deals_page_rejects_a_cursor_without_has_more() -> None:
    with pytest.raises(BitrixBoundedContractError, match="continuation fields disagree"):
        parse_deal_page(deal_page(has_more=False, next_cursor="cursor-2"))


@pytest.mark.parametrize("has_more", ["true", 1, None])
def test_parse_changed_deals_page_rejects_a_non_boolean_has_more(has_more: JsonValue) -> None:
    with pytest.raises(BitrixBoundedContractError, match="has_more must be boolean"):
        parse_deal_page(deal_page(has_more=has_more))


def test_parse_changed_deals_page_rejects_a_page_over_its_declared_size() -> None:
    page = deal_page(changes=[deal_change(deal_id=10), deal_change(deal_id=11)])
    with pytest.raises(BitrixBoundedContractError, match="exceeded its declared page size"):
        parse_deal_page(page, max_changes=1)


def test_parse_changed_deals_page_rejects_a_non_positive_max_changes() -> None:
    with pytest.raises(ValueError, match="maximum bounded page size must be positive") as error:
        parse_deal_page(deal_page(changes=[]), max_changes=0)
    assert type(error.value) is ValueError


def test_deal_change_rejects_an_unexpected_field() -> None:
    item = deal_change()
    item["extra"] = "unexpected"
    with pytest.raises(BitrixBoundedContractError, match="unexpected or missing fields"):
        parse_deal_page(deal_page(changes=[item]))


@pytest.mark.parametrize("field", ["payload", "revision"])
def test_deal_change_rejects_an_omitted_field(field: str) -> None:
    item = deal_change()
    del item[field]
    with pytest.raises(BitrixBoundedContractError, match="unexpected or missing fields"):
        parse_deal_page(deal_page(changes=[item]))


@pytest.mark.parametrize("kind", ["delete", "UPSERT", "", 1, None, True])
def test_deal_change_rejects_an_invalid_kind(kind: JsonValue) -> None:
    with pytest.raises(BitrixBoundedContractError, match="invalid kind"):
        parse_deal_page(deal_page(changes=[deal_change(kind=kind)]))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("change_version", 0),
        ("change_version", -1),
        ("change_version", True),
        ("change_version", "1"),
        ("deal_id", 0),
        ("deal_id", -2),
        ("deal_id", True),
        ("deal_id", None),
    ],
)
def test_deal_change_rejects_non_positive_identifiers(field: str, value: JsonValue) -> None:
    item = deal_change()
    item[field] = value
    with pytest.raises(BitrixBoundedContractError, match="must be a positive integer"):
        parse_deal_page(deal_page(changes=[item]))


@pytest.mark.parametrize("event_id", ["", "   ", None, 7])
def test_deal_change_rejects_a_blank_event_id(event_id: JsonValue) -> None:
    with pytest.raises(BitrixBoundedContractError, match="changed-deal event_id"):
        parse_deal_page(deal_page(changes=[deal_change(event_id=event_id)]))


@pytest.mark.parametrize("revision", [None, "", "   ", 3])
def test_deal_change_rejects_a_blank_revision(revision: JsonValue) -> None:
    with pytest.raises(BitrixBoundedContractError, match="changed-deal revision"):
        parse_deal_page(deal_page(changes=[deal_change(revision=revision)]))


@pytest.mark.parametrize("category_id", [5, "", "   "])
def test_deal_change_rejects_a_non_string_category_id(category_id: JsonValue) -> None:
    with pytest.raises(BitrixBoundedContractError, match="changed-deal category_id"):
        parse_deal_page(deal_page(changes=[deal_change(category_id=category_id)]))


def test_deal_change_rejects_a_non_object_payload() -> None:
    with pytest.raises(BitrixBoundedContractError, match="changed-deal payload must be an object"):
        parse_deal_page(deal_page(changes=[deal_change(payload=[1, 2])]))


@pytest.mark.parametrize("field", ["payload", "category_id"])
def test_deal_tombstone_rejects_current_data(field: str) -> None:
    item = deal_change(kind="tombstone", category_id=None, payload=None)
    item[field] = _DEAL_PAYLOAD if field == "payload" else "cat-1"
    with pytest.raises(BitrixBoundedContractError, match="tombstone must not include current data"):
        parse_deal_page(deal_page(changes=[item]))


def test_deal_tombstone_keeps_its_required_revision_identity() -> None:
    """A deal tombstone bans current data (payload/category_id) but must keep its revision."""
    page = parse_deal_page(
        deal_page(changes=[deal_change(kind="tombstone", category_id=None, payload=None)]),
    )
    (change,) = page.changes
    assert change.kind == "tombstone"
    assert change.revision == "rev-1"
    assert change.category_id is None
    assert change.payload is None


@pytest.mark.parametrize("field", ["payload", "category_id"])
def test_deal_upsert_requires_pinned_current_data(field: str) -> None:
    item = deal_change()
    item[field] = None
    with pytest.raises(BitrixBoundedContractError, match="upsert omitted its pinned current data"):
        parse_deal_page(deal_page(changes=[item]))


def test_parse_changed_conversations_page_returns_order_and_continuation() -> None:
    page = parse_conversation_page(
        conversation_page(
            has_more=True,
            next_cursor="cursor-2",
            changes=[
                conversation_change(chat_id=20, event_id="evt-1"),
                conversation_change(chat_id=21),
            ],
        ),
    )
    assert isinstance(page, ChangedConversationsPage)
    assert page.snapshot_id == _SNAPSHOT_ID
    assert page.next_cursor == "cursor-2"
    assert [change.order for change in page.changes] == [
        ConversationChangeOrder(1, 20, "evt-1"),
        ConversationChangeOrder(1, 21, "evt-1"),
    ]
    assert page.changes[0].revision == "rev-1"
    assert page.changes[0].message_cursor == "msg-1"
    assert page.changes[0].payload == _MESSAGE_PAYLOAD


def test_parse_changed_conversations_page_accepts_an_empty_terminal_page() -> None:
    page = parse_conversation_page(conversation_page(changes=[]))
    assert page.changes == ()
    assert page.next_cursor is None


def test_conversation_change_order_is_version_then_chat_then_event() -> None:
    assert ConversationChangeOrder(1, 10, "evt-1") == ConversationChangeOrder(
        change_version=1,
        chat_id=10,
        event_id="evt-1",
    )
    assert ConversationChangeOrder(1, 10, "evt-1") < ConversationChangeOrder(2, 1, "evt-0")
    assert ConversationChangeOrder(1, 10, "evt-1") < ConversationChangeOrder(1, 11, "evt-0")
    assert ConversationChangeOrder(1, 10, "evt-1") < ConversationChangeOrder(1, 10, "evt-2")


@pytest.mark.parametrize(
    "orders",
    [
        ((1, 20, "evt-1"), (1, 20, "evt-1")),
        ((2, 20, "evt-1"), (1, 20, "evt-1")),
        ((1, 20, "evt-1"), (1, 19, "evt-1")),
        ((1, 20, "evt-2"), (1, 20, "evt-1")),
    ],
)
def test_parse_changed_conversations_page_rejects_orders_that_do_not_advance(
    orders: tuple[tuple[int, int, str], ...],
) -> None:
    changes = [
        conversation_change(change_version=version, chat_id=chat_id, event_id=event_id)
        for version, chat_id, event_id in orders
    ]
    with pytest.raises(BitrixBoundedContractError, match="order did not strictly advance"):
        parse_conversation_page(conversation_page(changes=changes))


def test_parse_changed_conversations_page_rejects_a_snapshot_mismatch() -> None:
    with pytest.raises(BitrixBoundedContractError, match="conversation page snapshot mismatch"):
        parse_conversation_page(conversation_page(snapshot_id="other-snapshot"))


def test_parse_changed_conversations_page_rejects_a_page_missing_fields() -> None:
    payload = conversation_page()
    payload["extra"] = "unexpected"
    with pytest.raises(BitrixBoundedContractError, match="unexpected or missing fields"):
        parse_conversation_page(payload)


def test_parse_changed_conversations_page_rejects_a_non_list_changes_field() -> None:
    with pytest.raises(BitrixBoundedContractError, match="changes must be a list"):
        parse_conversation_page(conversation_page(changes={"evt": 1}))


def test_parse_changed_conversations_page_rejects_has_more_without_a_cursor() -> None:
    with pytest.raises(BitrixBoundedContractError, match="continuation fields disagree"):
        parse_conversation_page(conversation_page(has_more=True, next_cursor=None))


def test_parse_changed_conversations_page_rejects_a_cursor_without_has_more() -> None:
    with pytest.raises(BitrixBoundedContractError, match="continuation fields disagree"):
        parse_conversation_page(conversation_page(has_more=False, next_cursor="cursor-2"))


def test_parse_changed_conversations_page_rejects_a_page_over_its_declared_size() -> None:
    changes = [conversation_change(chat_id=20), conversation_change(chat_id=21)]
    with pytest.raises(BitrixBoundedContractError, match="exceeded its declared page size"):
        parse_conversation_page(conversation_page(changes=changes), max_changes=1)


def test_parse_changed_conversations_page_rejects_a_non_positive_max_changes() -> None:
    with pytest.raises(ValueError, match="maximum bounded page size must be positive") as error:
        parse_conversation_page(conversation_page(changes=[]), max_changes=0)
    assert type(error.value) is ValueError


def test_conversation_change_rejects_an_unexpected_field() -> None:
    item = conversation_change()
    item["extra"] = "unexpected"
    with pytest.raises(BitrixBoundedContractError, match="unexpected or missing fields"):
        parse_conversation_page(conversation_page(changes=[item]))


@pytest.mark.parametrize("field", ["payload", "revision"])
def test_conversation_change_rejects_an_omitted_field(field: str) -> None:
    item = conversation_change()
    del item[field]
    with pytest.raises(BitrixBoundedContractError, match="unexpected or missing fields"):
        parse_conversation_page(conversation_page(changes=[item]))


@pytest.mark.parametrize("kind", ["deleted", "TOMBSTONE", "", 0, None, False])
def test_conversation_change_rejects_an_invalid_kind(kind: JsonValue) -> None:
    with pytest.raises(BitrixBoundedContractError, match="invalid kind"):
        parse_conversation_page(conversation_page(changes=[conversation_change(kind=kind)]))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("change_version", 0),
        ("change_version", -1),
        ("change_version", True),
        ("chat_id", 0),
        ("chat_id", -4),
        ("chat_id", True),
        ("chat_id", None),
    ],
)
def test_conversation_change_rejects_non_positive_identifiers(
    field: str,
    value: JsonValue,
) -> None:
    item = conversation_change()
    item[field] = value
    with pytest.raises(BitrixBoundedContractError, match="must be a positive integer"):
        parse_conversation_page(conversation_page(changes=[item]))


@pytest.mark.parametrize("event_id", ["", "   ", None, 7])
def test_conversation_change_rejects_a_blank_event_id(event_id: JsonValue) -> None:
    item = conversation_change(event_id=event_id)
    with pytest.raises(BitrixBoundedContractError, match="changed-conversation event_id"):
        parse_conversation_page(conversation_page(changes=[item]))


@pytest.mark.parametrize("revision", [None, "", "   ", 3])
def test_conversation_change_rejects_a_blank_revision(revision: JsonValue) -> None:
    item = conversation_change(revision=revision)
    with pytest.raises(BitrixBoundedContractError, match="changed-conversation revision"):
        parse_conversation_page(conversation_page(changes=[item]))


@pytest.mark.parametrize("message_cursor", [5, "", "   "])
def test_conversation_change_rejects_an_invalid_message_cursor(
    message_cursor: JsonValue,
) -> None:
    item = conversation_change(message_cursor=message_cursor)
    with pytest.raises(BitrixBoundedContractError, match="message_cursor must be"):
        parse_conversation_page(conversation_page(changes=[item]))


def test_conversation_change_rejects_a_non_object_payload() -> None:
    item = conversation_change(payload="text")
    with pytest.raises(BitrixBoundedContractError, match="payload must be an object"):
        parse_conversation_page(conversation_page(changes=[item]))


@pytest.mark.parametrize("field", ["payload", "message_cursor"])
def test_conversation_tombstone_rejects_current_data(field: str) -> None:
    item = conversation_change(kind="tombstone", message_cursor=None, payload=None)
    item[field] = _MESSAGE_PAYLOAD if field == "payload" else "msg-9"
    with pytest.raises(BitrixBoundedContractError, match="tombstone must not include current data"):
        parse_conversation_page(conversation_page(changes=[item]))


def test_conversation_tombstone_keeps_its_required_revision_identity() -> None:
    """A conversation tombstone bans payload/message_cursor but must keep its revision."""
    tombstone = conversation_change(kind="tombstone", message_cursor=None, payload=None)
    page = parse_conversation_page(conversation_page(changes=[tombstone]))
    (change,) = page.changes
    assert change.kind == "tombstone"
    assert change.revision == "rev-1"
    assert change.message_cursor is None
    assert change.payload is None


@pytest.mark.parametrize("field", ["payload", "message_cursor"])
def test_conversation_upsert_requires_pinned_current_data(field: str) -> None:
    item = conversation_change()
    item[field] = None
    with pytest.raises(BitrixBoundedContractError, match="upsert omitted its pinned current data"):
        parse_conversation_page(conversation_page(changes=[item]))
