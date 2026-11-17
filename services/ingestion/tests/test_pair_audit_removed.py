"""Guard that the person-pair auditor is fully removed (#492 step 5)."""

from __future__ import annotations

import importlib.util
import re

_CREATE = re.compile(r"\bCREATE\b")
_PAIR_AUDIT_LITERAL = "'pair_audit'"


def test_pair_auditor_modules_are_absent() -> None:
    assert importlib.util.find_spec("src.pipeline_person_pairs") is None
    assert importlib.util.find_spec("src.graph.queries.person_pairs") is None


def test_no_cypher_constant_creates_pair_audit_decisions() -> None:
    """No query constant may CREATE an engine_type 'pair_audit' decision.

    Reads of legacy pair-audit cases (the CRM-repair verifier) and the
    recalculation module only MATCH existing decisions, so they never trip
    this guard.
    """
    from src.graph import queries

    creating: list[str] = []
    for name in dir(queries):
        if name.startswith("__"):
            continue
        value = getattr(queries, name)
        if (
            isinstance(value, str)
            and _CREATE.search(value) is not None
            and _PAIR_AUDIT_LITERAL in value
        ):
            creating.append(name)
    assert creating == []
