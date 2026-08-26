"""Public and internal types for the machine identity-link revision stream."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from pydantic import BaseModel, Field, model_validator

IdentityLinkStatus = Literal[
    "resolved", "unresolved", "pending_review", "blocked", "rejected", "retired"
]
IdentityLinkResolutionKind = Literal[
    "baseline",
    "automatic_activation",
    "reviewed_activation",
    "review_rejection",
    "manual_no_match",
    "source_supersession",
    "person_merge",
    "person_unmerge",
    "person_retirement",
    "source_retirement",
]


class IdentityLinkRevision(BaseModel):  # type: ignore[explicit-any]  # Pydantic BaseModel metaclass
    """Privacy-safe immutable external identity-link revision."""

    event_id: str
    global_revision: int = Field(ge=1)
    source_system: str = Field(min_length=1)
    source_instance_id: str = Field(min_length=1)
    source_entity_type: Literal["deal", "contact", "lead", "company"]
    source_entity_id: str = Field(min_length=1)
    identity_policy_version: str = Field(min_length=1)
    link_status: IdentityLinkStatus
    hyperp_person_id: str | None = None
    resolution_kind: IdentityLinkResolutionKind
    resolution_revision: int = Field(ge=1)
    effective_at: datetime
    match_decision_id: str | None = None
    review_case_id: str | None = None
    supersedes_event_id: str | None = None

    @model_validator(mode="after")
    def _check_person_visibility(self) -> IdentityLinkRevision:
        if self.link_status == "resolved" and not self.hyperp_person_id:
            raise ValueError("resolved identity links require hyperp_person_id")
        if self.link_status != "resolved" and self.hyperp_person_id is not None:
            raise ValueError("only resolved identity links may contain hyperp_person_id")
        return self


@dataclass(frozen=True)
class IdentityLinkDesiredRevision:
    """Internal desired current-state projection supplied by one write transaction."""

    source_system: str
    source_instance_id: str
    source_entity_type: str
    source_entity_id: str
    identity_policy_version: str
    link_status: IdentityLinkStatus
    hyperp_person_id: str | None
    resolution_kind: IdentityLinkResolutionKind
    effective_at: str
    cause_key: str
    match_decision_id: str | None = None
    review_case_id: str | None = None


def identity_link_key(
    source_system: str,
    source_instance_id: str,
    source_entity_type: str,
    source_entity_id: str,
    identity_policy_version: str,
) -> str:
    """Return an unambiguous private key for one complete source identity."""
    parts = (
        source_system,
        source_instance_id,
        source_entity_type,
        source_entity_id,
        identity_policy_version,
    )
    if any(not part.strip() for part in parts):
        raise ValueError("identity-link provenance fields must not be blank")
    return "ilk1:" + "".join(f"{len(part)}:{part}" for part in parts)


def validate_desired_revision(row: IdentityLinkDesiredRevision) -> IdentityLinkDesiredRevision:
    """Validate policy allowlist and public Person/status invariant before allocation."""
    if row.source_system != "bitrix_chat":
        raise ValueError("identity-link source system is not exportable")
    if row.source_entity_type not in {"deal", "contact", "lead", "company"}:
        raise ValueError("identity-link source entity type is not exportable")
    if row.source_entity_type == "deal" and row.identity_policy_version != "crm_deal_identity_v2":
        raise ValueError("CRM deal identity policy is invalid")
    expected = {
        "contact": "crm_contact_identity_v1",
        "lead": "crm_lead_identity_v1",
        "company": "crm_company_reference_v1",
    }
    if (
        row.source_entity_type in expected
        and row.identity_policy_version != expected[row.source_entity_type]
    ):
        raise ValueError("standalone CRM identity policy is invalid")
    if row.link_status == "resolved" and not row.hyperp_person_id:
        raise ValueError("resolved identity links require hyperp_person_id")
    if row.link_status != "resolved" and row.hyperp_person_id is not None:
        raise ValueError("only resolved identity links may contain hyperp_person_id")
    try:
        datetime.fromisoformat(row.effective_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("identity-link effective_at must be ISO-8601") from exc
    _ = cast(Literal["deal", "contact", "lead", "company"], row.source_entity_type)
    if not row.cause_key.strip():
        raise ValueError("identity-link cause_key must not be blank")
    return row
