from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from src.connectors.bitrix_stage_history.canonical import (
    canonical_stage_hash_v1,
    decode_stage_source_record_id,
    encode_stage_source_record_id,
    normalize_source_contract_id,
)
from src.connectors.bitrix_stage_history.models import StageHistoryItem

_CONTRACT_ID = "123e4567-e89b-12d3-a456-426614174000"


def _item(
    *,
    stage_id: str | None = "C2:NEW",
    created_at: datetime | None = None,
) -> StageHistoryItem:
    return StageHistoryItem(
        history_id="001",
        entity_type_id="2",
        owner_id="501",
        type_id="1",
        created_time=created_at or datetime(2026, 8, 6, 4, 0, tzinfo=UTC),
        created_time_source="2026-08-06T04:00:00+00:00",
        category_id="2",
        stage_semantic_id="P",
        stage_id=stage_id,
        raw_payload={"ID": "001"},
    )


def test_stage_source_record_identity_is_injective() -> None:
    first = encode_stage_source_record_id(_CONTRACT_ID, "2", "34")
    second = encode_stage_source_record_id(_CONTRACT_ID, "23", "4")

    assert first != second
    assert first.startswith("bitrix-crm-stagehistory-v1:")


def test_stage_source_record_identity_round_trips_frozen_golden_vector() -> None:
    encoded = encode_stage_source_record_id(_CONTRACT_ID, "2", "001")

    assert encoded == ("bitrix-crm-stagehistory-v1:36:123e4567-e89b-12d3-a456-4266141740001:23:001")
    assert decode_stage_source_record_id(encoded) == (_CONTRACT_ID, "2", "001")


@pytest.mark.parametrize(
    "value",
    [
        "bitrix-crm-stagehistory-v1:36:123e4567-e89b-12d3-a456-4266141740001:2",
        "bitrix-crm-stagehistory-v1:x:value",
        "bitrix-crm-stagehistory-v2:1:a1:b1:c",
    ],
)
def test_stage_source_record_decoder_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError, match="stage source record identity"):
        decode_stage_source_record_id(value)


def test_canonical_hash_preserves_opaque_identifier_lexemes() -> None:
    leading_zero = canonical_stage_hash_v1(_CONTRACT_ID, _item())
    normalized = canonical_stage_hash_v1(
        _CONTRACT_ID,
        StageHistoryItem(
            history_id="1",
            entity_type_id="2",
            owner_id="501",
            type_id="1",
            created_time=datetime(2026, 8, 6, 4, 0, tzinfo=UTC),
            created_time_source="2026-08-06T04:00:00+00:00",
            category_id="2",
            stage_semantic_id="P",
            stage_id="C2:NEW",
            raw_payload={},
        ),
    )

    assert leading_zero != normalized


def test_canonical_hash_normalizes_equivalent_unicode_and_offsets() -> None:
    composed = _item(stage_id="C2:Café")
    decomposed = _item(
        stage_id="C2:Cafe\u0301",
        created_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone(timedelta(hours=8))),
    )

    assert canonical_stage_hash_v1(_CONTRACT_ID, composed) == canonical_stage_hash_v1(
        _CONTRACT_ID, decomposed
    )


def test_canonical_hash_rejects_control_characters() -> None:
    with pytest.raises(ValueError, match="control"):
        canonical_stage_hash_v1(_CONTRACT_ID, _item(stage_id="C2:\x01"))


@pytest.mark.parametrize("control", ["\n", "\r", "\t", "\x7f"])
def test_canonical_hash_rejects_all_control_characters(control: str) -> None:
    with pytest.raises(ValueError, match="control"):
        canonical_stage_hash_v1(_CONTRACT_ID, _item(stage_id=f"C2:{control}"))


def test_source_contract_id_is_normalized_in_identity_and_hash() -> None:
    uppercase = _CONTRACT_ID.upper()

    assert normalize_source_contract_id(uppercase) == _CONTRACT_ID
    assert encode_stage_source_record_id(uppercase, "2", "34") == encode_stage_source_record_id(
        _CONTRACT_ID, "2", "34"
    )
    assert canonical_stage_hash_v1(uppercase, _item()) == canonical_stage_hash_v1(
        _CONTRACT_ID, _item()
    )


@pytest.mark.parametrize("value", ["", "contract", "123e4567-e89b-12d3-a456"])
def test_source_contract_id_rejects_non_uuid_values(value: str) -> None:
    with pytest.raises(ValueError, match="UUID"):
        encode_stage_source_record_id(value, "2", "34")


def test_canonical_hash_rejects_surrogate_code_points() -> None:
    with pytest.raises(ValueError, match="surrogate"):
        canonical_stage_hash_v1(_CONTRACT_ID, _item(stage_id="C2:\ud800"))


def test_source_identity_rejects_whitespace_only_required_components() -> None:
    with pytest.raises(ValueError, match="entity_type_id"):
        encode_stage_source_record_id(_CONTRACT_ID, "   ", "34")
