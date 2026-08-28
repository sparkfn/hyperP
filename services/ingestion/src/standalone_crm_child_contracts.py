"""Strict source-child execution authorities for bounded standalone CRM units.

These contracts sit beside the v1 ``StandaloneCrmChildEnvelope``. That existing
envelope remains an immutable publication transport payload; these authorities
are the richer execution inputs required by future source writers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.source_instances import effective_control_instance_id, effective_source_instance_id
from src.standalone_crm_census_types import StandaloneCrmStreamKind, _integer, _text, _utc

SOURCE_CHILD_AVAILABILITY_CONTRACT_VERSION = "standalone-crm-source-availability-v1"


@dataclass(frozen=True)
class StandaloneCrmSourceChildScope:
    """Canonical Bitrix source/control context for one source-child authority."""

    source_key: str
    source_instance_id: str
    control_instance_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_key", _text(self.source_key, "source_key"))
        if self.source_key != "bitrix_chat":
            raise ValueError("source_key must be bitrix_chat")
        source_instance_id = effective_source_instance_id(self.source_instance_id)
        control_instance_id = effective_control_instance_id(self.control_instance_id)
        if source_instance_id != self.source_instance_id:
            raise ValueError("source_instance_id must be canonical")
        if control_instance_id != self.control_instance_id:
            raise ValueError("control_instance_id must be canonical")


@dataclass(frozen=True)
class StandaloneCrmSourceChildUnitAuthority:
    """Fenced unit/publication identity reasserted before every domain commit."""

    census_id: str
    stream_kind: StandaloneCrmStreamKind
    generation: int
    fence_token: int
    fence_owner_id: str
    task_name: str
    task_id: str
    payload_digest: str

    def __post_init__(self) -> None:
        for field in ("census_id", "fence_owner_id", "task_name", "task_id"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        object.__setattr__(
            self,
            "payload_digest",
            _canonical_sha256(self.payload_digest, "payload_digest"),
        )
        if self.stream_kind not in {"contact", "lead", "company"}:
            raise ValueError("invalid source child stream kind")
        _integer(self.generation, "generation", 1)
        _integer(self.fence_token, "fence_token", 1)


@dataclass(frozen=True)
class StandaloneCrmSourceAvailability:
    """Deterministic source-fact availability from the persisted parent census."""

    available_at: str
    contract_version: str = SOURCE_CHILD_AVAILABILITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "available_at", _utc(self.available_at, "available_at"))
        if self.contract_version != SOURCE_CHILD_AVAILABILITY_CONTRACT_VERSION:
            raise ValueError("unsupported source availability contract version")


@dataclass(frozen=True)
class StandaloneCrmSourceChildBudgetAuthorization:
    """Immutable bounded row/call authority tied to one execution identity."""

    authorization_id: str
    authorization_digest: str
    census_id: str
    stream_kind: StandaloneCrmStreamKind
    generation: int
    fence_token: int
    fence_owner_id: str
    task_name: str
    task_id: str
    payload_digest: str
    max_calls_per_attempt: int
    max_rows_per_attempt: int
    max_calls_per_occurrence: int
    max_rows_per_occurrence: int
    attempt_deadline: str
    occurrence_deadline: str

    def __post_init__(self) -> None:
        for field in ("authorization_id", "census_id", "fence_owner_id", "task_name", "task_id"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        object.__setattr__(
            self,
            "authorization_digest",
            _canonical_sha256(self.authorization_digest, "authorization_digest"),
        )
        object.__setattr__(
            self,
            "payload_digest",
            _canonical_sha256(self.payload_digest, "payload_digest"),
        )
        if self.stream_kind not in {"contact", "lead", "company"}:
            raise ValueError("invalid budget authorization stream kind")
        _integer(self.generation, "generation", 1)
        _integer(self.fence_token, "fence_token", 1)
        for field in (
            "max_calls_per_attempt",
            "max_rows_per_attempt",
            "max_calls_per_occurrence",
            "max_rows_per_occurrence",
        ):
            _integer(getattr(self, field), field, 1)
        if self.max_calls_per_attempt > self.max_calls_per_occurrence:
            raise ValueError("attempt calls cannot exceed occurrence calls")
        if self.max_rows_per_attempt > self.max_rows_per_occurrence:
            raise ValueError("attempt rows cannot exceed occurrence rows")
        object.__setattr__(
            self, "attempt_deadline", _utc(self.attempt_deadline, "attempt_deadline")
        )
        object.__setattr__(
            self,
            "occurrence_deadline",
            _utc(self.occurrence_deadline, "occurrence_deadline"),
        )
        if _instant(self.attempt_deadline) > _instant(self.occurrence_deadline):
            raise ValueError("attempt deadline cannot exceed occurrence deadline")


@dataclass(frozen=True)
class ContactBindingSubposition:
    """A resumable position within complete company-binding processing for one contact."""

    binding_subject_id: int
    binding_offset: int

    def __post_init__(self) -> None:
        _integer(self.binding_subject_id, "binding_subject_id", 1)
        _integer(self.binding_offset, "binding_offset")


@dataclass(frozen=True)
class _SourceChildEnvelope:
    """Common immutable source-child authority; subclasses fix the unit domain."""

    scope: StandaloneCrmSourceChildScope
    unit: StandaloneCrmSourceChildUnitAuthority
    frozen_upper_id: int
    last_committed_id: int
    availability: StandaloneCrmSourceAvailability
    budget_authorization: StandaloneCrmSourceChildBudgetAuthorization

    def _validate_common(self, stream_kind: StandaloneCrmStreamKind) -> None:
        if not isinstance(self.scope, StandaloneCrmSourceChildScope):
            raise ValueError("source child envelope requires a canonical source scope")
        if not isinstance(self.unit, StandaloneCrmSourceChildUnitAuthority):
            raise ValueError("source child envelope requires a fenced unit authority")
        if not isinstance(self.availability, StandaloneCrmSourceAvailability):
            raise ValueError("source child envelope requires deterministic availability")
        if not isinstance(
            self.budget_authorization,
            StandaloneCrmSourceChildBudgetAuthorization,
        ):
            raise ValueError("source child envelope requires bounded budget authorization")
        _integer(self.frozen_upper_id, "frozen_upper_id")
        _integer(self.last_committed_id, "last_committed_id")
        if self.last_committed_id > self.frozen_upper_id:
            raise ValueError("last_committed_id cannot exceed frozen_upper_id")
        if self.unit.stream_kind != stream_kind:
            raise ValueError("source child envelope stream kind does not match its domain")
        authorization = self.budget_authorization
        if (
            authorization.census_id != self.unit.census_id
            or authorization.stream_kind != self.unit.stream_kind
            or authorization.generation != self.unit.generation
            or authorization.fence_token != self.unit.fence_token
            or authorization.fence_owner_id != self.unit.fence_owner_id
            or authorization.task_name != self.unit.task_name
            or authorization.task_id != self.unit.task_id
            or authorization.payload_digest != self.unit.payload_digest
        ):
            raise ValueError("budget authorization does not match source child authority")


@dataclass(frozen=True)
class ContactSourceChildEnvelope(_SourceChildEnvelope):
    """Execution/commit authority for exactly one bounded contact unit."""

    binding_subposition: ContactBindingSubposition | None = None

    def __post_init__(self) -> None:
        self._validate_common("contact")
        if (
            self.binding_subposition is not None
            and self.binding_subposition.binding_subject_id > self.frozen_upper_id
        ):
            raise ValueError("binding_subject_id cannot exceed frozen_upper_id")


@dataclass(frozen=True)
class LeadSourceChildEnvelope(_SourceChildEnvelope):
    """Execution/commit authority for exactly one bounded lead unit."""

    def __post_init__(self) -> None:
        self._validate_common("lead")


@dataclass(frozen=True)
class CompanySourceChildEnvelope(_SourceChildEnvelope):
    """Execution/commit authority for exactly one bounded company unit."""

    def __post_init__(self) -> None:
        self._validate_common("company")


type StandaloneCrmSourceChildEnvelope = (
    ContactSourceChildEnvelope | LeadSourceChildEnvelope | CompanySourceChildEnvelope
)


def _canonical_sha256(value: str, field: str) -> str:
    normalized = _text(value, field)
    hex_value = normalized.removeprefix("sha256:")
    if (
        normalized != value
        or not normalized.startswith("sha256:")
        or len(hex_value) != 64
        or any(character not in "0123456789abcdef" for character in hex_value)
    ):
        raise ValueError(f"{field} must be canonical sha256")
    return normalized


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
