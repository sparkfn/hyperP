"""Shared exception types for the ingestion service."""

from __future__ import annotations


class SourceNotConfiguredError(Exception):
    """Raised when an ingestion source is dispatched before its env is set.

    This is a pre-provisioning state, not a code defect: the service is allowed
    to boot with empty source config, and the failure surfaces at dispatch time
    so the Celery task can log a clean warning and reject the run without a
    traceback or a retry loop.
    """
