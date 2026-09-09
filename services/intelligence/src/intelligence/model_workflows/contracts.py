"""Strict value contracts for the reviewed offline categorical baseline."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from intelligence.artifacts import canonical_json

RECIPE = "categorical_frequency_v1"
RECIPE_VERSION = "1"
MODEL_SCHEMA = "intelligence-model-candidate-v1"
EVALUATION_SCHEMA = "intelligence-model-evaluation-v1"
COMPARISON_SCHEMA = "intelligence-model-comparison-v1"
ACTIVITY_PROVENANCE = {
    "bitrix_completeness_asserted": False,
    "completeness": "legacy_partial_snapshot",
    "source_population": "neo4j_existing_records",
}
FEATURES = (
    "category_id",
    "stage_id",
    "stage_semantic_id",
    "activity_missingness_reason",
    "included_activity_join_corroboration",
)
CHILD_LIMITS = {"max_cpu_seconds": 10, "max_address_space_bytes": 512 * 1024 * 1024}
RUNTIME_LIMITS = {
    "max_log_bytes": 100_000,
    "max_output_bytes": 5_000_000,
    "max_output_entries": 32,
    "max_runtime_seconds": 30,
}


def digest(value: object) -> str:
    """Return a stable SHA-256 digest for canonical JSON logical content."""
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def safe_id(value: str, field: str) -> str:
    """Accept one conservative artifact identifier, never a path."""
    if not value or len(value) > 200 or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{field} is invalid")
    return value


@dataclass(frozen=True)
class TrainRequest:
    dataset_id: str
    accepted_run_id: str
    recipe: str
    seed: int

    def __post_init__(self) -> None:
        safe_id(self.dataset_id, "dataset id")
        safe_id(self.accepted_run_id, "accepted run")
        if self.recipe != RECIPE or not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ValueError("training request is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted_run_id": self.accepted_run_id,
            "dataset_id": self.dataset_id,
            "recipe": self.recipe,
            "seed": self.seed,
        }


@dataclass(frozen=True)
class EvaluationRequest:
    model_id: str
    model_run_id: str
    dataset_id: str
    accepted_run_id: str

    def __post_init__(self) -> None:
        for value, field in (
            (self.model_id, "model id"),
            (self.model_run_id, "model run"),
            (self.dataset_id, "dataset id"),
            (self.accepted_run_id, "accepted run"),
        ):
            safe_id(value, field)

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted_run_id": self.accepted_run_id,
            "dataset_id": self.dataset_id,
            "model_id": self.model_id,
            "model_run_id": self.model_run_id,
        }


@dataclass(frozen=True)
class VerifyRequest:
    """Exact inactive candidate verification needs no unrelated evaluation dataset."""

    model_id: str
    model_run_id: str

    def __post_init__(self) -> None:
        safe_id(self.model_id, "model id")
        safe_id(self.model_run_id, "model run")

    def as_dict(self) -> dict[str, object]:
        return {"model_id": self.model_id, "model_run_id": self.model_run_id}


def code_fingerprint() -> str:
    """Fingerprint reviewed workflow bytes with platform-independent newlines."""
    root = Path(__file__).parent
    content: list[dict[str, str]] = []
    for path in sorted(root.glob("*.py"), key=lambda item: item.name):
        content.append(
            {"name": path.name, "text": path.read_text(encoding="utf-8").replace("\r\n", "\n")}
        )
    return digest(content)
