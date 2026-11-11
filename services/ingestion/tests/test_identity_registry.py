"""Regression checks for identity policy and bootstrap-derived source policy."""

from __future__ import annotations

import ast
from pathlib import Path

from src.graph.bootstrap import _SOURCE_SYSTEMS
from src.identity_registry import (
    IDENTIFIER_TYPE_REGISTRY,
    IdentifierTypePolicy,
    identifier_policy,
    source_promotes_person,
)
from src.pipeline_normalization import fanout_cap_for

_CONNECTORS = Path(__file__).parents[1] / "src" / "connectors"


def _emitted_identifier_types() -> set[str]:
    emitted: set[str] = set()
    for path in _CONNECTORS.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bag_names = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "IdentifierBag"
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                receiver = node.func.value
                if node.func.attr == "add" and isinstance(receiver, ast.Name):
                    if receiver.id in bag_names and node.args:
                        _add_type_argument(node.args[0], emitted)
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "type"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                    ):
                        emitted.add(value.value)
    return emitted


def _add_type_argument(argument: ast.expr, emitted: set[str]) -> None:
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        emitted.add(argument.value)
    elif isinstance(argument, ast.JoinedStr):
        prefix = "".join(part.value for part in argument.values if isinstance(part, ast.Constant))
        if prefix.startswith("social:"):
            emitted.add("social:*")


def test_fanout_caps_match_the_previous_behavior() -> None:
    assert fanout_cap_for("phone") == 50
    assert fanout_cap_for("email") == 100
    assert fanout_cap_for("nric") == 5
    assert fanout_cap_for("device_id") == 25
    assert fanout_cap_for("social:facebook") == 25
    assert fanout_cap_for("social:other-unknown") == 25
    assert fanout_cap_for("unknown") is None


def test_promotion_and_single_value_flags_follow_existing_matching_rules() -> None:
    promoting = {name for name, policy in IDENTIFIER_TYPE_REGISTRY.items() if policy.promotes}
    assert promoting == {"nric", "membership_id", "external_customer_id"}
    single_valued = {
        name for name, policy in IDENTIFIER_TYPE_REGISTRY.items() if policy.single_valued
    }
    assert single_valued == {"nric"}


def test_every_bootstrap_source_has_an_explicit_promotion_answer() -> None:
    for source in _SOURCE_SYSTEMS:
        should_promote = not source.get("match_only", False)
        assert source_promotes_person(source["source_key"]) is should_promote
    assert source_promotes_person("sgbankruptcy") is False


def test_every_connector_identifier_type_is_registered_or_social_wildcard() -> None:
    emitted = _emitted_identifier_types()

    assert emitted
    assert all(
        identifier_type in IDENTIFIER_TYPE_REGISTRY
        or (identifier_type.startswith("social:") and "social:*" in IDENTIFIER_TYPE_REGISTRY)
        for identifier_type in emitted
    )
    assert identifier_policy("future_unknown") == IdentifierTypePolicy(
        candidate_eligible=True,
        single_valued=False,
        fanout_cap=None,
        half_life_days=None,
        promotes=False,
    )


def test_all_registered_types_have_none_half_life_days() -> None:
    for identifier_type, policy in IDENTIFIER_TYPE_REGISTRY.items():
        assert policy.half_life_days is None, f"{identifier_type} must not invent half_life_days"
    assert identifier_policy("unknown").half_life_days is None


def test_all_registered_types_and_default_are_candidate_eligible() -> None:
    for identifier_type, policy in IDENTIFIER_TYPE_REGISTRY.items():
        assert policy.candidate_eligible is True, f"{identifier_type} must be candidate_eligible"
    assert identifier_policy("unknown").candidate_eligible is True
