from __future__ import annotations

from src.machine_unit_extraction import (
    observations_from_chat_inquiries,
    observations_from_sales_lines,
)


def test_observations_from_sales_lines_extracts_fundbox_lta_and_serial() -> None:
    observations = observations_from_sales_lines(
        source_system_key="fundbox_consumer_backend",
        source_record_id="fundbox-sale-1",
        observed_at="2026-05-14T00:00:00",
        lines=[
            {
                "source_line_item_id": "line-1",
                "metadata": {"lta_tag": "LTA 123", "serial_no": "SN-9"},
                "product": {"display_name": "Bike A"},
            }
        ],
    )

    assert len(observations) == 1
    assert observations[0].lta_tag == "LTA 123"
    assert observations[0].serial_number == "SN-9"
    assert observations[0].machine_product == "Bike A"


def test_observations_from_sales_lines_extracts_phppos_serialnumber() -> None:
    observations = observations_from_sales_lines(
        source_system_key="speedzone_phppos",
        source_record_id="speedzone-sale-1",
        observed_at="2026-05-14T00:00:00",
        lines=[{"source_line_item_id": "line-1", "metadata": {"serialnumber": "SN-10"}}],
    )

    assert len(observations) == 1
    assert observations[0].serial_number == "SN-10"
    assert observations[0].lta_tag is None


def test_observations_from_chat_inquiries_are_inquiry_evidence() -> None:
    observations = observations_from_chat_inquiries(
        source_system_key="whatsapp_chat",
        source_record_id="whatsapp-chat-1",
        observed_at="2026-05-14T00:00:00",
        inquiries=[{"lta_tag": "LTA123", "serial_number": "SN-9", "notes": "Asked availability"}],
    )

    assert len(observations) == 1
    assert observations[0].source_kind == "chat_inquiry"
