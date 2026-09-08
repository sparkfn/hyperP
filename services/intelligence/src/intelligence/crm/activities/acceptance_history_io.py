"""Budgeted low-level candidate-history reads for archive admission."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from intelligence.artifacts import canonical_json
from intelligence.crm.activities.acceptance_parsing import candidate_name
from intelligence.crm.activities.bounded import ReadBudget, checkpoint_root, read_evidence
from intelligence.models import OutputInventory, Run


class CandidateState(Protocol):
    def inspect(self, run_id: str) -> Run | None: ...

    def accepted_outputs(self, run_id: str) -> tuple[OutputInventory, ...]: ...


def candidate_history(
    workspace: Path,
    checkpoint_id: str,
    prefix: str,
    budget: ReadBudget,
) -> tuple[tuple[str, dict[str, object]], ...]:
    """Enumerate and read immutable candidate files through one shared budget."""
    root = checkpoint_root(workspace, checkpoint_id)
    if root is None:
        return ()
    names: list[str] = []
    try:
        for path in sorted(root.iterdir(), key=lambda item: item.name):
            budget.add_entry()
            if path.name.startswith(prefix):
                names.append(path.name)
    except OSError as error:
        raise ValueError("candidate history could not be read") from error
    values: list[tuple[str, dict[str, object]]] = []
    for name in names:
        if not candidate_name(name, prefix):
            raise ValueError("candidate history name is invalid")
        value = read_evidence(root, name, budget)
        if value is None:
            raise ValueError("candidate history disappeared during read")
        _add_inventory_rows(value, budget)
        values.append((name, value))
    return tuple(values)


def inspect(state: CandidateState, run_id: str, budget: ReadBudget) -> Run | None:
    budget.add_entry()
    return state.inspect(run_id)


def accepted_outputs(
    state: CandidateState,
    run_id: str,
    budget: ReadBudget,
) -> tuple[OutputInventory, ...]:
    budget.add_entry()
    values = state.accepted_outputs(run_id)
    budget.add_rows(len(values))
    return values


def inventory_key(values: tuple[OutputInventory, ...]) -> str:
    return canonical_json(
        [
            {
                "relative_path": item.relative_path,
                "sha256": item.sha256,
                "byte_count": item.byte_count,
            }
            for item in values
        ]
    )


def _add_inventory_rows(value: Mapping[str, object], budget: ReadBudget) -> None:
    candidate_inventory = value.get("inventory")
    if isinstance(candidate_inventory, list):
        budget.add_rows(len(candidate_inventory))
