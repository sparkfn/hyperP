"""Repository-facing activation aliases and failures.

The public protocol and value objects deliberately live in
``crm_tenant_activation_contracts`` so the census worker and graph adapter share
one frozen boundary.
"""

from __future__ import annotations

from src.crm_tenant_activation_contracts import CrmTenantActivationRepository

__all__ = ("CrmTenantActivationRepository",)
