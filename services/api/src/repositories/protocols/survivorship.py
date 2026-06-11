"""Survivorship repository protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class BatchOverrideResult:
    outcome: str
    """'ok', 'person_not_found', 'sr_not_found', 'value_not_found', or 'invalid_field'."""
    failed_field: str | None = field(default=None)
    """Field name that caused the failure; None when outcome is 'ok'."""


@dataclass
class FieldOptionRow:
    """One candidate source value for an editable golden-profile field."""

    field_name: str
    source_kind: str
    identifier_type: str | None
    value: str
    address_id: str | None
    source_record_pk: str
    source_system: str
    entity_display_name: str | None
    observed_at: str | None


@dataclass
class FieldOptionsData:
    """Editable-field options plus the person's current preferred values and overrides."""

    person_id: str
    preferred_full_name: str | None
    preferred_dob: str | None
    preferred_phone: str | None
    preferred_email: str | None
    preferred_nric: str | None
    preferred_address_id: str | None
    overrides: dict[str, dict[str, str]]
    options: list[FieldOptionRow]


class SurvivorshipRepository(Protocol):
    async def recompute_golden_profile(self, person_id: str) -> float | None: ...

    async def get_field_options(self, person_id: str) -> FieldOptionsData | None:
        """Editable golden-profile fields and their selectable source values.

        Returns None if the person is not found or not active.
        """
        ...

    async def create_override(
        self,
        person_id: str,
        field_name: str,
        source_record_pk: str,
        reason: str,
        actor_id: str,
    ) -> str:
        """Returns one of the BatchOverrideResult outcome strings (excluding batch-only ones)."""
        ...

    async def create_custom_override(
        self,
        person_id: str,
        field_name: str,
        custom_value: str,
        reason: str,
        actor_id: str,
    ) -> str:
        """Pin a golden-profile field to a manually entered literal value."""
        ...

    async def create_batch_overrides(
        self,
        person_id: str,
        items: list[tuple[str, str]],
        reason: str,
        actor_id: str,
    ) -> BatchOverrideResult:
        """Apply multiple field overrides atomically in one write transaction.

        Each item is ``(field_name, source_record_pk)``.
        """
        ...
