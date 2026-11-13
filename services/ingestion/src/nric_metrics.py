"""CLI and pure aggregators for NRIC coverage, validity, and conflict metrics."""

from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import TypedDict

from neo4j import ManagedTransaction

from src.config import get_settings
from src.graph.client import Neo4jClient
from src.graph.queries.nric_metrics import (
    FETCH_ACTIVE_PERSON_COUNT,
    FETCH_ACTIVE_PERSON_NRIC_ROWS,
    FETCH_NRIC_REVEAL_EVENT_COUNT,
)
from src.normalizers.nric import is_nric_digest, normalize_nric, nric_checksum_valid, nric_digest
from src.nric_anchor import NricValue, classify_person_nrics

logger = logging.getLogger(__name__)


class ConflictClasses(TypedDict):
    """Counts of NRIC conflict classifications across active persons."""

    alias: int
    invalid_only: int
    shared_valid: int


class NricMetrics(TypedDict):
    """Aggregate NRIC coverage, validity, and conflict metrics."""

    coverage_pct: float
    checksum_pass_rate_by_source: dict[str, float]
    conflict_classes: ConflictClasses
    reveal_count: int
    alias_count: int


def compute_nric_metrics(
    rows: Sequence[Mapping[str, object]],
    active_person_count: int,
    reveal_count: int,
    hmac_key: str = "",
) -> NricMetrics:
    """Compute NRIC metrics from person NRIC rows and graph counts.

    Emits:
      - coverage_pct: float
      - checksum_pass_rate_by_source: dict[str, float]
      - conflict_classes: dict[str, int] with keys alias, invalid_only, shared_valid
      - reveal_count: int
      - alias_count: int
    """
    person_nrics: dict[str, list[NricValue]] = defaultdict(list)
    source_stats: dict[str, list[bool]] = defaultdict(list)
    digest_to_persons: dict[str, set[str]] = defaultdict(set)

    for row in rows:
        person_id = str(row["person_id"])
        norm_val = str(row["normalized_value"])
        source_key = str(row.get("source_system_key") or "unknown")

        # Resolve to digest and checksum_valid
        if is_nric_digest(norm_val):
            digest = norm_val
            chk = row.get("checksum_valid")
            checksum_is_valid = bool(chk) if chk is not None else None
        else:
            canonical, _ = normalize_nric(norm_val)
            if canonical:
                digest = nric_digest(canonical, hmac_key) if hmac_key else canonical
                checksum_is_valid = nric_checksum_valid(canonical)
            else:
                digest = norm_val
                checksum_is_valid = False

        if checksum_is_valid is not None:
            source_stats[source_key].append(checksum_is_valid)

        nv = NricValue(digest=digest, checksum_valid=checksum_is_valid)
        person_nrics[person_id].append(nv)

        if checksum_is_valid is True:
            digest_to_persons[digest].add(person_id)

    # Coverage
    covered_persons = len(person_nrics)
    coverage_pct = round(
        (covered_persons / active_person_count * 100.0) if active_person_count > 0 else 0.0,
        2,
    )

    # Checksum pass rate by source
    checksum_pass_rate: dict[str, float] = {}
    for s_key, results in source_stats.items():
        if results:
            pass_rate = round(sum(results) / len(results) * 100.0, 2)
            checksum_pass_rate[s_key] = pass_rate

    # Classify conflicts per person
    alias_count = 0
    invalid_only_count = 0
    for _pid, vals in person_nrics.items():
        classification = classify_person_nrics(vals)
        if classification == "alias":
            alias_count += 1
        elif classification == "invalid_only":
            invalid_only_count += 1

    # Shared valid: valid digest held by >1 active Person
    shared_valid_count = sum(1 for pids in digest_to_persons.values() if len(pids) > 1)

    return {
        "coverage_pct": coverage_pct,
        "checksum_pass_rate_by_source": checksum_pass_rate,
        "conflict_classes": {
            "alias": alias_count,
            "invalid_only": invalid_only_count,
            "shared_valid": shared_valid_count,
        },
        "reveal_count": reveal_count,
        "alias_count": alias_count,
    }


def main(
    argv: Sequence[str] | None = None,
    client_factory: Callable[[], Neo4jClient] | None = None,
) -> int:
    """CLI entry point for emitting NRIC metrics as JSON."""
    settings = get_settings()
    created_locally = client_factory is None
    client = Neo4jClient(settings) if created_locally else client_factory()  # type: ignore[misc]

    try:
        with client.session() as session:

            def _work(tx: ManagedTransaction) -> NricMetrics:
                count_rec = tx.run(FETCH_ACTIVE_PERSON_COUNT).single()
                active_count = int(count_rec["active_person_count"]) if count_rec else 0

                reveal_rec = tx.run(FETCH_NRIC_REVEAL_EVENT_COUNT).single()
                reveal_count = int(reveal_rec["reveal_count"]) if reveal_rec else 0

                rows = list(tx.run(FETCH_ACTIVE_PERSON_NRIC_ROWS))
                return compute_nric_metrics(
                    rows,
                    active_count,
                    reveal_count,
                    hmac_key=settings.nric_hmac_key,
                )

            metrics = session.execute_read(_work)

        out = json.dumps(metrics, indent=2)
        print(out)
        logger.info("NRIC metrics: %s", out)
        return 0
    finally:
        if created_locally:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
