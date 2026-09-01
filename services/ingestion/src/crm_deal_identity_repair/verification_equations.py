"""Validated accounting contracts for CRM repair verification."""

from __future__ import annotations

from dataclasses import dataclass

from src.crm_deal_identity_repair.digests import inventory_digest, run_equation_digest
from src.crm_deal_identity_repair.models import RepairInventoryItem


def _nonnegative(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"repair verification {label} must be non-negative")


@dataclass(frozen=True)
class RepairUnitEquation:
    allocated_unit_count: int
    complete_applied_count: int
    complete_review_required_count: int
    incomplete_count: int
    first_commit_attempt_count: int
    replay_no_op_count: int
    drift_count: int
    failure_count: int
    expected_active_replacement_links: int
    observed_active_replacement_links: int
    active_provisional_links: int
    forbidden_projection_count: int
    expected_secondary_count: int
    observed_secondary_count: int
    reconciled_secondary_count: int
    review_required_secondary_count: int
    failed_secondary_count: int
    pending_secondary_count: int
    unexplained_secondary_count: int

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            _nonnegative(value, name)

    @property
    def balanced(self) -> bool:
        terminal = self.allocated_unit_count == (
            self.complete_applied_count
            + self.complete_review_required_count
            + self.incomplete_count
        )
        secondary = (
            self.expected_secondary_count
            == self.observed_secondary_count
            == (
                self.reconciled_secondary_count
                + self.review_required_secondary_count
                + self.failed_secondary_count
                + self.pending_secondary_count
                + self.unexplained_secondary_count
            )
        )
        attempt = 1 == (
            self.first_commit_attempt_count
            + self.replay_no_op_count
            + self.drift_count
            + self.failure_count
        )
        links = (
            self.expected_active_replacement_links == self.complete_applied_count
            and self.expected_active_replacement_links == self.observed_active_replacement_links
        )
        provisional = self.active_provisional_links <= self.complete_review_required_count
        return (
            terminal
            and attempt
            and secondary
            and links
            and provisional
            and self.incomplete_count == 0
            and self.drift_count == 0
            and self.failure_count == 0
            and (
                self.failed_secondary_count
                == self.pending_secondary_count
                == self.unexplained_secondary_count
                == 0
            )
            and self.forbidden_projection_count == 0
        )


@dataclass(frozen=True)
class RepairRunEquationCommand:
    repair_id: str
    run_id: str
    boundary_digest: str
    inventory: tuple[RepairInventoryItem, ...]
    inventory_digest_expected: str
    source_instance_id: str
    control_instance_id: str

    def __post_init__(self) -> None:
        keys = tuple(item.inventory_key for item in self.inventory)
        source_record_pks = tuple(item.source_record_pk for item in self.inventory)
        if (
            not self.repair_id
            or not self.run_id
            or not self.source_instance_id
            or not self.control_instance_id
        ):
            raise ValueError("run equation identity is invalid")
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("run equation inventory must be canonical and unique")
        if source_record_pks != tuple(sorted(source_record_pks)):
            raise ValueError("run equation source PKs must be canonical")
        if len(source_record_pks) != len(set(source_record_pks)):
            raise ValueError("run equation source PKs must be unique")
        if inventory_digest(self.inventory) != self.inventory_digest_expected:
            raise ValueError("run equation inventory digest differs")
        if any(item.source_system != "bitrix_chat" for item in self.inventory):
            raise ValueError("run equation inventory source is invalid")


@dataclass(frozen=True)
class RepairRunEquationResult:
    qualified_inventory_rows: int
    executable_inventory_rows: int
    negative_control_rows: int
    applied_units: int
    review_required_units: int
    incomplete_units: int
    verified_units: int
    drifted_units: int
    failed_units: int
    committed_attempts: int
    replay_no_op_attempts: int
    active_links: int
    unsupported_multi_links: int
    active_deal_origin_phone_projections: int
    active_deal_origin_email_projections: int
    active_deal_origin_g_us_projections: int
    reconciled_secondaries: int
    review_required_secondaries: int
    failed_secondaries: int
    pending_secondaries: int
    expected_secondary_count: int
    observed_secondary_count: int
    unexplained_secondary_remainder: int
    unchanged_negative_controls: int
    drifted_negative_controls: int
    missing_negative_controls: int
    stamped_negative_controls: int
    evidence_digest: str

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if name != "evidence_digest":
                _nonnegative(value, name)

    @property
    def balanced(self) -> bool:
        return (
            self.qualified_inventory_rows
            == self.executable_inventory_rows + self.negative_control_rows
            and self.executable_inventory_rows
            == self.applied_units + self.review_required_units + self.incomplete_units
            and self.verified_units == self.executable_inventory_rows
            and self.committed_attempts == self.verified_units
            and self.replay_no_op_attempts == 0
            and self.active_links == self.applied_units
            and (self.incomplete_units == self.drifted_units == self.failed_units == 0)
            and self.unsupported_multi_links == 0
            and (
                self.active_deal_origin_phone_projections
                == self.active_deal_origin_email_projections
                == self.active_deal_origin_g_us_projections
                == 0
            )
            and self.expected_secondary_count
            == self.observed_secondary_count
            == (
                self.reconciled_secondaries
                + self.review_required_secondaries
                + self.failed_secondaries
                + self.pending_secondaries
                + self.unexplained_secondary_remainder
            )
            and self.failed_secondaries == self.pending_secondaries == 0
            and self.unexplained_secondary_remainder == 0
            and (
                self.unchanged_negative_controls == self.negative_control_rows
                and self.drifted_negative_controls
                == self.missing_negative_controls
                == self.stamped_negative_controls
                == 0
            )
        )

    @property
    def digest(self) -> str:
        return run_equation_digest({key: value for key, value in self.__dict__.items()})
