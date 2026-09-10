"""Privacy-safe deterministic training and held-out population selection."""

from __future__ import annotations

from intelligence.model_workflows.contracts import digest

LABELS = ("lost", "open", "won")


def membership_hash(row: dict[str, object]) -> str:
    """Return a domain-separated hash of full source identity, never a raw artifact ID."""
    source = {
        "entity": _text(row.get("source_entity_id"), "source entity"),
        "instance": _text(row.get("source_instance_id"), "source instance"),
        "system": _text(row.get("source_system"), "source system"),
    }
    return digest({"domain": "intelligence-model-membership-v1", "source": source})


def eligible_row(row: dict[str, object]) -> bool:
    """Return whether one dataset row is safe for model training or evaluation."""
    return (
        row.get("disposition") == "labeled"
        and row.get("label") in LABELS
        and isinstance(row.get("person_id"), str)
        and bool(row.get("person_id"))
        and isinstance(row.get("feature_source_record_pk"), str)
        and bool(row.get("feature_source_record_pk"))
    )


def split(
    rows: tuple[dict[str, object], ...], seed: int
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...], dict[str, int]]:
    """Split only eligible labeled rows, keeping a transient Person group intact."""
    groups: dict[str, list[dict[str, object]]] = {}
    exclusions: dict[str, int] = {}
    for row in rows:
        person = row.get("person_id")
        if not eligible_row(row) or not isinstance(person, str):
            exclusions["not_eligible_labeled_person"] = (
                exclusions.get("not_eligible_labeled_person", 0) + 1
            )
            continue
        groups.setdefault(person, []).append(row)
    training: list[dict[str, object]] = []
    held_out: list[dict[str, object]] = []
    for person in sorted(groups):
        target = (
            held_out
            if int(digest({"person_id": person, "seed": seed})[:8], 16) % 5 == 0
            else training
        )
        target.extend(sorted(groups[person], key=_source_entity))
    if not training or not held_out:
        raise ValueError("insufficient deterministic held-out partitions")
    return tuple(training), tuple(held_out), dict(sorted(exclusions.items()))


def _source_entity(row: dict[str, object]) -> str:
    return _text(row.get("source_entity_id"), "source entity")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is invalid")
    return value
