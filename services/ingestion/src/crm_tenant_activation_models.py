"""Errors and small validation helpers for the #307 activation boundary."""

from __future__ import annotations


class CrmTenantActivationError(RuntimeError):
    """Base error for mapping/projection activation."""


class CrmTenantActivationConflictError(CrmTenantActivationError):
    """The immutable command or one of its CAS boundaries is stale."""


class CrmTenantActivationIntegrityError(CrmTenantActivationError):
    """Persisted activation state is malformed or internally inconsistent."""
