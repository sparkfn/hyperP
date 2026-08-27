"""Control-scoped durable IDs for stage-history graph artifacts."""

from __future__ import annotations

from src.source_instances import (
    LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
    effective_control_instance_id,
    scope_control_identity,
)


def scope_stage_history_identity(identity: str, control_instance_id: str) -> str:
    """Preserve legacy IDs exactly and namespace nonlegacy graph identities."""
    control = effective_control_instance_id(control_instance_id)
    prefix = f"ci1:{len(control)}:{control}:"
    if control != LEGACY_DEFAULT_CONTROL_INSTANCE_ID and identity.startswith(prefix):
        return identity
    return scope_control_identity(identity, control)
