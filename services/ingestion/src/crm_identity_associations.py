"""Deterministic standalone Bitrix CRM company-membership source facts.

This module deliberately contains no graph access.  It validates the bounded
source contract at the ingestion boundary so the graph writer can treat a
``CrmCompanyMembershipSnapshot`` as complete immutable source evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from src.connectors.bitrix_openlines.models import CrmCompanyBindingPayload
from src.models import JsonValue

CrmIdentitySubjectType = Literal["contact", "lead"]
CRM_COMPANY_MEMBERSHIP_CONTRACT_VERSION = "crm-company-membership-snapshot-v1"
_MAX_SIGNED_INT32 = 2_147_483_647


@dataclass(frozen=True, order=True)
class CrmCompanyBinding:
    """One normalized company binding in a complete CRM membership snapshot."""

    company_id: str
    sort: int | None
    role_id: str | None
    is_primary: bool


@dataclass(frozen=True)
class CrmCompanyMembershipSnapshot:
    """Complete normalized source membership evidence for one CRM subject."""

    subject_type: CrmIdentitySubjectType
    subject_id: str
    bindings: tuple[CrmCompanyBinding, ...]
    contract_version: str = CRM_COMPANY_MEMBERSHIP_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.subject_type not in {"contact", "lead"}:
            raise ValueError("CRM membership subject_type must be contact or lead")
        if _positive_decimal(self.subject_id, field_name="subject_id") != self.subject_id:
            raise ValueError("CRM membership subject_id must be canonical positive decimal")
        if self.contract_version != CRM_COMPANY_MEMBERSHIP_CONTRACT_VERSION:
            raise ValueError("CRM membership contract_version is unsupported")
        for binding in self.bindings:
            if _positive_decimal(binding.company_id, field_name="company_id") != binding.company_id:
                raise ValueError("CRM membership company_id must be canonical positive decimal")
            if binding.sort is not None and (
                isinstance(binding.sort, bool)
                or not isinstance(binding.sort, int)
                or binding.sort < 0
                or binding.sort > _MAX_SIGNED_INT32
            ):
                raise ValueError("CRM membership sort must be a non-negative 32-bit integer")
            if binding.role_id is not None and (
                _optional_positive_decimal(binding.role_id, field_name="role_id") != binding.role_id
            ):
                raise ValueError("CRM membership role_id must be canonical positive decimal")
            if not isinstance(binding.is_primary, bool):
                raise ValueError("CRM membership is_primary must be boolean")
        if len({binding.company_id for binding in self.bindings}) != len(self.bindings):
            raise ValueError("CRM membership bindings must have unique company IDs")
        if sum(binding.is_primary for binding in self.bindings) > 1:
            raise ValueError("CRM membership bindings contain more than one primary company")
        if tuple(sorted(self.bindings, key=_binding_order)) != self.bindings:
            raise ValueError("CRM membership bindings must use canonical order")

    @property
    def digest(self) -> str:
        """Return the domain-separated stable digest for the complete set."""
        payload: dict[str, JsonValue] = {
            "source_contract_version": self.contract_version,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "bindings": [
                {
                    "company_id": binding.company_id,
                    "sort": binding.sort,
                    "role_id": binding.role_id,
                    "is_primary": binding.is_primary,
                }
                for binding in self.bindings
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(
            b"crm-company-membership-snapshot-v1\x00" + encoded.encode("utf-8")
        ).hexdigest()


def normalize_company_membership_snapshot(
    *,
    subject_type: CrmIdentitySubjectType,
    subject_id: str,
    payloads: tuple[CrmCompanyBindingPayload, ...],
    contract_version: str = CRM_COMPANY_MEMBERSHIP_CONTRACT_VERSION,
) -> CrmCompanyMembershipSnapshot:
    """Validate, deduplicate, and canonically order a complete binding response."""
    normalized_subject_id = _positive_decimal(subject_id, field_name="subject_id")
    by_company: dict[str, CrmCompanyBinding] = {}
    for payload in payloads:
        binding = CrmCompanyBinding(
            company_id=_positive_decimal(payload.company_id, field_name="COMPANY_ID"),
            sort=_optional_sort(payload.sort),
            role_id=_optional_positive_decimal(payload.role_id, field_name="ROLE_ID"),
            is_primary=_primary_flag(payload.is_primary),
        )
        existing = by_company.get(binding.company_id)
        if existing is None:
            by_company[binding.company_id] = binding
        elif existing != binding:
            raise ValueError("CRM company bindings contain conflicting duplicate COMPANY_ID")
    bindings = tuple(sorted(by_company.values(), key=_binding_order))
    if sum(binding.is_primary for binding in bindings) > 1:
        raise ValueError("CRM company bindings contain more than one primary company")
    return CrmCompanyMembershipSnapshot(
        subject_type=subject_type,
        subject_id=normalized_subject_id,
        bindings=bindings,
        contract_version=contract_version,
    )


def lead_membership_snapshot(
    *,
    lead_id: str,
    company_id: str | None,
    contract_version: str = CRM_COMPANY_MEMBERSHIP_CONTRACT_VERSION,
) -> CrmCompanyMembershipSnapshot:
    """Return the complete v1 zero-or-one COMPANY_ID lead snapshot."""
    bindings: tuple[CrmCompanyBindingPayload, ...]
    if company_id is None or not company_id.strip():
        bindings = ()
    else:
        bindings = (
            CrmCompanyBindingPayload(
                company_id=company_id,
                sort=None,
                role_id=None,
                is_primary=True,
            ),
        )
    return normalize_company_membership_snapshot(
        subject_type="lead",
        subject_id=lead_id,
        payloads=bindings,
        contract_version=contract_version,
    )


def _binding_order(binding: CrmCompanyBinding) -> tuple[int, int, int, int, int, int]:
    return (
        0 if binding.is_primary else 1,
        1 if binding.sort is None else 0,
        binding.sort if binding.sort is not None else 0,
        int(binding.company_id),
        1 if binding.role_id is None else 0,
        int(binding.role_id) if binding.role_id is not None else 0,
    )


def _positive_decimal(value: object, *, field_name: str) -> str:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field_name} must be a positive decimal")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        raise ValueError(f"{field_name} must be a positive decimal")
    if parsed < 1:
        raise ValueError(f"{field_name} must be a positive decimal")
    return str(parsed)


def _optional_positive_decimal(value: object, *, field_name: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field_name} must be a positive decimal")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        raise ValueError(f"{field_name} must be a positive decimal")
    if parsed < 1:
        raise ValueError(f"{field_name} must be a positive decimal")
    return str(parsed)


def _optional_sort(value: object) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("SORT must be a positive decimal")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        raise ValueError("SORT must be a positive decimal")
    if parsed < 0 or parsed > _MAX_SIGNED_INT32:
        raise ValueError("SORT must be a non-negative 32-bit decimal")
    return parsed


def _primary_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value in {"N", "Y", "0", "1"}:
        return value in {"Y", "1"}
    raise ValueError("IS_PRIMARY must be bool, 0/1, or Y/N")
