from __future__ import annotations

import pytest
from src.connectors.bitrix_openlines.selection import (
    classify_channel,
    mapped_entity,
    no_config_selectable,
)
from src.ingestion_config import BitrixOpenLinesChannelType, BitrixOpenLinesConfig


@pytest.mark.parametrize(
    ("connector_id", "expected"),
    [
        ("WHATSAPP_BUSINESS_API_CONNECTOR_AIAPPS_PRO_1", "whatsapp_business_api"),
        ("SPARKFN_WHATSAPP", "whatsapp_device"),
        ("facebook", "facebook_direct"),
        ("facebookcomments", "facebook_comments"),
        ("instagram", "instagram"),
        ("future_connector", "other"),
    ],
)
def test_connector_identifiers_classify_without_using_line_names(
    connector_id: str,
    expected: BitrixOpenLinesChannelType,
) -> None:
    assert classify_channel(connector_id) == expected


def test_exclusion_wins_and_every_selected_config_requires_an_entity_mapping() -> None:
    config = BitrixOpenLinesConfig(
        included_config_ids=["200", "201"],
        excluded_config_ids=["201"],
        entity_by_config_id={"200": "eko", "201": "speedzone"},
    )

    assert mapped_entity("200", "other", config) == "eko"
    assert mapped_entity("201", "facebook_direct", config) is None
    assert mapped_entity("202", "facebook_direct", config) is None



def test_no_config_selectable_when_no_entity_mappings_exist() -> None:
    config = BitrixOpenLinesConfig(included_config_ids=["46"])

    assert no_config_selectable(config) is True


def test_no_config_selectable_when_channel_type_selection_is_active() -> None:
    config = BitrixOpenLinesConfig(
        included_channel_types=["facebook_direct"],
        entity_by_config_id={"46": "speedzone"},
    )

    assert no_config_selectable(config) is False


def test_no_config_selectable_when_no_included_config_has_an_entity_mapping() -> None:
    config = BitrixOpenLinesConfig(
        included_channel_types=[],
        included_config_ids=["99"],
        entity_by_config_id={"46": "speedzone"},
    )

    assert no_config_selectable(config) is True


def test_no_config_selectable_false_when_included_config_is_mapped() -> None:
    config = BitrixOpenLinesConfig(
        included_channel_types=[],
        included_config_ids=["46"],
        entity_by_config_id={"46": "speedzone"},
    )

    assert no_config_selectable(config) is False


def test_no_config_selectable_when_only_excluded_config_is_mapped() -> None:
    config = BitrixOpenLinesConfig(
        included_channel_types=[],
        included_config_ids=["46"],
        excluded_config_ids=["46"],
        entity_by_config_id={"46": "speedzone"},
    )

    assert no_config_selectable(config) is True
