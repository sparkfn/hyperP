"""Classify and select Bitrix Open Channel configurations."""

from __future__ import annotations

from src.ingestion_config import BitrixOpenLinesChannelType, BitrixOpenLinesConfig


def classify_channel(connector_id: str) -> BitrixOpenLinesChannelType:
    """Map a Bitrix dialog origin connector to a stable ingestion family."""
    normalized = connector_id.strip().lower().replace("-", "_")
    if "whatsapp_business_api_connector" in normalized:
        return "whatsapp_business_api"
    if "sparkfn_whatsapp" in normalized or "whatsapp_device" in normalized:
        return "whatsapp_device"
    if "facebookcomments" in normalized or "facebook_comments" in normalized:
        return "facebook_comments"
    if normalized == "facebook" or normalized.startswith("facebook_"):
        return "facebook_direct"
    if "instagram" in normalized:
        return "instagram"
    if "telegram" in normalized:
        return "telegram"
    if "carousell" in normalized:
        return "carousell"
    if "bitrix" in normalized:
        return "bitrix_chat"
    return "other"


def mapped_entity(
    config_id: str,
    channel_type: BitrixOpenLinesChannelType,
    config: BitrixOpenLinesConfig,
) -> str | None:
    """Return the explicitly mapped entity when a configuration is selected."""
    if config_id in config.excluded_config_ids:
        return None
    selected = (
        config_id in config.included_config_ids or channel_type in config.included_channel_types
    )
    if not selected:
        return None
    return config.entity_by_config_id.get(config_id)
