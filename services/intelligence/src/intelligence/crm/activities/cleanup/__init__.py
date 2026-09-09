"""Manifest-gated CRM activity cleanup capability."""

from intelligence.crm.activities.cleanup.status import CleanupStatus, read_status

__all__ = ("CleanupStatus", "read_status")
