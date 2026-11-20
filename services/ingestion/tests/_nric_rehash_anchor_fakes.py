"""Small fake responses and durable-state transitions for A4 query paths."""

from __future__ import annotations

from collections.abc import Mapping

from src.graph.queries.nric_rehash import (
    NRIC_REHASH_ADVANCE_ANCHOR_BATCH,
    NRIC_VERIFY_READINESS,
)
from src.graph.queries.nric_rehash_anchor import NRIC_REHASH_ANCHOR_PAGE


def anchor_query_response(
    query: str,
    parameters: Mapping[str, object],
    state: dict[str, object] | None,
) -> tuple[Mapping[str, object], ...] | None:
    """Return fake result rows for A4-only query constants, when recognized."""
    if query == NRIC_REHASH_ANCHOR_PAGE:
        return ()
    if query == NRIC_VERIFY_READINESS:
        return (
            {
                "anchored_persons": 0,
                "identifiers_without_assertion": 0,
                "identifiers_without_projection": 0,
            },
        )
    if query != NRIC_REHASH_ADVANCE_ANCHOR_BATCH:
        return None
    if (
        state is None
        or state.get("lease_owner") != parameters.get("owner")
        or state.get("phase") != "anchor"
    ):
        return ()
    cursor = parameters.get("cursor")
    added = parameters.get("anchored")
    current = state.get("anchored", 0)
    if (
        not isinstance(cursor, str)
        or not isinstance(added, int)
        or isinstance(added, bool)
        or not isinstance(current, int)
        or isinstance(current, bool)
    ):
        return ()
    state["anchor_cursor"] = cursor
    state["anchored"] = current + added
    return ({"anchored": current + added},)


def add_anchor_state_defaults(state: dict[str, object]) -> None:
    state.setdefault("anchor_cursor", None)
    state.setdefault("anchored", 0)


def anchor_state_fields(state: Mapping[str, object]) -> dict[str, object]:
    return {"anchor_cursor": None, "anchored": 0, **state}


def apply_anchor_acquire_state(
    state: dict[str, object],
    parameters: Mapping[str, object],
) -> None:
    add_anchor_state_defaults(state)
    target_phase = parameters.get("target_phase")
    if parameters.get("restart_completed") is True:
        state["phase"] = target_phase
        state["cursor"] = None
        state["anchor_cursor"] = None
    elif target_phase == "anchor":
        phase_complete = state.get("phase") in {"convert_complete", "anchor_complete"}
        state["phase"] = "anchor"
        if phase_complete:
            state["anchor_cursor"] = None
