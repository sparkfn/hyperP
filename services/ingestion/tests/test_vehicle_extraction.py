from __future__ import annotations

from src.vehicle_extraction import (
    observations_from_chat_inquiries,
    observations_from_sales_lines,
)


def test_eko_sales_vehicle_line_extracts_product_sku_from_item_number() -> None:
    """Case 1: Eko vehicle line with serial emits the item number as product SKU."""
    observations = observations_from_sales_lines(
        source_system_key="eko_phppos",
        source_record_id="eko-sale-1",
        observed_at="2026-05-14T00:00:00",
        lines=[
            {
                "source_line_item_id": "eko-line-1",
                "metadata": {"serial_number": "SN-100", "lta_tag": "LTA-200"},
                "product": {
                    "name": "City E-Bike",
                    "category": "Electric Bicycles",
                    "item_number": "EKO-ITEM-9",
                },
            }
        ],
    )

    assert len(observations) == 1
    obs = observations[0]
    assert obs.source_kind == "sales"
    assert obs.product_sku == "EKO-ITEM-9"
    assert obs.serial_number == "SN-100"
    assert obs.lta_tag == "LTA-200"
    assert obs.confidence == 1.0
    assert obs.raw_context == "eko-line-1"


def test_non_vehicle_category_line_is_skipped() -> None:
    """Case 2: Eko non-vehicle categories are skipped even when a serial is present."""
    observations = observations_from_sales_lines(
        source_system_key="eko_phppos",
        source_record_id="eko-sale-2",
        observed_at="2026-05-14T00:00:00",
        lines=[
            {
                "source_line_item_id": "eko-line-2",
                "metadata": {"serial_number": "SN-LOCK-1"},
                "product": {
                    "name": "U-Lock",
                    "category": "Bicycle Locks",
                    "item_number": "EKO-LOCK-1",
                },
            }
        ],
    )

    assert observations == []


def test_eko_vehicle_line_without_serial_or_lta_is_invalid() -> None:
    """Case 3: eko vehicle line WITHOUT serial/lta -> no observation (invalid)."""
    observations = observations_from_sales_lines(
        source_system_key="eko_phppos",
        source_record_id="eko-sale-3",
        observed_at="2026-05-14T00:00:00",
        lines=[
            {
                "source_line_item_id": "eko-line-3",
                "metadata": {},
                "product": {
                    "name": "Mountain Bike",
                    "category": "Mountain Bikes",
                    "sku": "EKO-MTB-1",
                },
            }
        ],
    )

    assert observations == []


def test_fundbox_vehicle_line_carries_sku_lta_manufacturer_model() -> None:
    """Case 4: Fundbox vehicle details carry into the observation."""
    observations = observations_from_sales_lines(
        source_system_key="fundbox",
        source_record_id="fundbox-sale-1",
        observed_at="2026-05-14T00:00:00",
        lines=[
            {
                "source_line_item_id": "fb-line-1",
                "metadata": {"serial_no": "FB-SN-7", "lta_tag": "LTA-FB-1"},
                "product": {
                    "name": "Mobility Scooter Pro",
                    "category": "Mobility Scooters",
                    "sku": "FB-SKU-7",
                    "manufacturer": "Acme Wheels",
                    "model": "Pro-2025",
                },
            }
        ],
    )

    assert len(observations) == 1
    obs = observations[0]
    assert obs.product_sku == "FB-SKU-7"
    assert obs.serial_number == "FB-SN-7"
    assert obs.lta_tag == "LTA-FB-1"
    assert obs.manufacturer == "Acme Wheels"
    assert obs.model == "Pro-2025"


def test_speedzone_line_with_serial_and_customer_plate_as_lta_tag() -> None:
    """Case 5: SpeedZone customer plates are emitted as observation LTA tags."""
    observations = observations_from_sales_lines(
        source_system_key="speedzone_phppos",
        source_record_id="speedzone-sale-1",
        observed_at="2026-05-14T00:00:00",
        lines=[
            {
                "source_line_item_id": "sz-line-1",
                "metadata": {"serial_number": "SZ-CHASSIS-9", "lta_tag": "SGX1234Z"},
                "product": {
                    "name": "Scrambler 400",
                    "category": "Scrambler",
                    "sku": "SZ-SKU-9",
                },
            }
        ],
    )

    assert len(observations) == 1
    obs = observations[0]
    assert obs.serial_number == "SZ-CHASSIS-9"
    assert obs.lta_tag == "SGX1234Z"


def test_chat_inquiry_naming_a_vehicle_creates_observation() -> None:
    """Case 6: chat inquiry naming a vehicle -> observation (chat_inquiry, confidence 0.6).

    Real chat inquiries carry a free-text product NAME from LLM extraction, not a
    source-internal SKU; the chat validator keys on product + identifier, so no
    product_sku is supplied or required.
    """
    observations = observations_from_chat_inquiries(
        source_system_key="whatsapp_chat",
        source_record_id="whatsapp-chat-1",
        observed_at="2026-05-14T00:00:00",
        inquiries=[
            {
                "vehicle_product": "Honda scooter",
                "serial_number": "SN-CHAT-1",
                "notes": "Asked availability",
            }
        ],
    )

    assert len(observations) == 1
    obs = observations[0]
    assert obs.source_kind == "chat_inquiry"
    assert obs.confidence == 0.6
    assert obs.serial_number == "SN-CHAT-1"
    assert obs.product_sku is None
    assert obs.product == "Honda scooter"


def test_chat_inquiry_naming_a_non_vehicle_product_is_skipped() -> None:
    """Case 6 (negative): chat inquiry naming 'helmet' -> skipped (no observation)."""
    observations = observations_from_chat_inquiries(
        source_system_key="whatsapp_chat",
        source_record_id="whatsapp-chat-2",
        observed_at="2026-05-14T00:00:00",
        inquiries=[
            {
                "vehicle_product": "Open-face helmet",
                "serial_number": "SN-HELM-1",
                "product_sku": "HELM-SKU-1",
                "notes": "Asked for helmet",
            }
        ],
    )

    assert observations == []


def test_onediver_line_never_produces_a_vehicle_observation() -> None:
    """Case 7: onediver line (any category) -> never a vehicle observation (no allowlist entry)."""
    observations = observations_from_sales_lines(
        source_system_key="onediver:sales",
        source_record_id="onediver-sale-1",
        observed_at="2026-05-14T00:00:00",
        lines=[
            {
                "source_line_item_id": "od-line-1",
                "metadata": {"serial_number": "OD-SN-1", "lta_tag": "LTA-OD-1"},
                "product": {
                    "name": "Dive Scooter",
                    "category": "Scooter",
                    "sku": "OD-SKU-1",
                    "manufacturer": "DiveCo",
                    "model": "Pro",
                },
            }
        ],
    )

    assert observations == []
