from __future__ import annotations

from src.machine_units import (
    MachineUnitObservation,
    normalize_lta_tag,
    normalize_serial_number,
    valid_machine_unit_observation,
)
from src.models import QualityFlag


def test_normalize_lta_tag_uppercases_and_removes_separators() -> None:
    assert normalize_lta_tag(" lta-123 45 ") == "LTA12345"


def test_normalize_serial_number_preserves_meaningful_punctuation() -> None:
    assert normalize_serial_number(" sn-09/a ") == "SN-09/A"


def test_placeholder_unit_values_are_rejected() -> None:
    obs = MachineUnitObservation(
        lta_tag="n/a",
        serial_number=None,
        machine_product=None,
        unit_label=None,
        source_kind="sales",
        source_system_key="fundbox_consumer_backend",
        source_record_id="order-1",
        observed_at="2026-05-14T00:00:00",
        confidence=1.0,
        quality_flag=QualityFlag.VALID,
        raw_context="line-1",
    )

    assert valid_machine_unit_observation(obs) is False


def test_observation_is_valid_when_one_identifier_normalizes() -> None:
    obs = MachineUnitObservation(
        lta_tag=None,
        serial_number=" sn-09 ",
        machine_product="Model A",
        unit_label="Unit 7",
        source_kind="sales",
        source_system_key="speedzone_phppos",
        source_record_id="sale-1",
        observed_at="2026-05-14T00:00:00",
        confidence=1.0,
        quality_flag=QualityFlag.VALID,
        raw_context="line-1",
    )

    assert valid_machine_unit_observation(obs) is True
