"""API-facing parity checks for repair-retired relationship readers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLASSIFIER_PATH = (
    _REPO_ROOT
    / "services"
    / "ingestion"
    / "src"
    / "crm_deal_identity_repair"
    / "reader_classification.py"
)


def _load_classifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "reader_classification_contract", _CLASSIFIER_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_api_authoritative_reader_parity_excludes_retired_links() -> None:
    classifier = _load_classifier()
    readers = classifier.assert_reader_contract(*classifier.approved_reader_sources(_REPO_ROOT))
    by_key = {reader.identifier: reader for reader in readers}

    for key in (
        "api/graph/queries/sales_prediction_gate.py:GATE_DEAL_VERSIONS_FOR_PARENTS",
        "api/profile_analysis_runtime_queries.py:FETCH_PROFILE_ANALYSIS_SNAPSHOT_ROWS",
    ):
        reader = by_key[key]
        assert reader.classification == "authoritative"
        assert "coalesce(" in reader.query


def test_api_audit_reader_is_explicitly_allowlisted_and_observable() -> None:
    classifier = _load_classifier()
    readers = {
        reader.identifier: reader
        for reader in classifier.discover_relationship_readers(
            *classifier.approved_reader_sources(_REPO_ROOT)
        )
    }
    review = readers["api/graph/queries/review.py:GET_PENDING_REVIEW_RECORD"]

    assert review.classification == "audit"
    assert review.identifier in classifier._AUDIT_READERS
    assert "OPTIONAL MATCH (pending)-[:LINKED_TO]->(prior:Person)" in review.query
