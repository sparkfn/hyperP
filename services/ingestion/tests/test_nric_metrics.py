"""Tests for NRIC metrics calculation and CLI wiring."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

import pytest
from src.normalizers.nric import nric_digest
from src.nric_metrics import compute_nric_metrics, main

TEST_KEY = "test-nric-hmac-key-v1"


def test_compute_nric_metrics_pure_aggregator() -> None:
    d1 = nric_digest("S1234567D", TEST_KEY)
    d2 = nric_digest("T1234567J", TEST_KEY)

    # 3 active persons
    # Person 1 has 2 valid NRICs (alias)
    # Person 2 has 1 invalid NRIC (invalid_only)
    # Person 3 shares valid NRIC d1 with Person 1 (shared_valid)
    rows = [
        {
            "person_id": "p1",
            "normalized_value": d1,
            "checksum_valid": True,
            "source_system_key": "fundbox",
        },
        {
            "person_id": "p1",
            "normalized_value": d2,
            "checksum_valid": True,
            "source_system_key": "fundbox",
        },
        {
            "person_id": "p2",
            "normalized_value": "S0000000J",
            "checksum_valid": False,
            "source_system_key": "speedzone_phppos",
        },
        {
            "person_id": "p3",
            "normalized_value": "S1234567D",  # legacy plaintext resolving to d1
            "checksum_valid": None,  # dynamically recomputed
            "source_system_key": "fundbox",
        },
    ]

    metrics = compute_nric_metrics(
        rows=rows,
        active_person_count=3,
        reveal_count=5,
        hmac_key=TEST_KEY,
    )

    assert metrics["coverage_pct"] == 100.0  # 3 of 3 covered
    assert metrics["reveal_count"] == 5
    assert metrics["alias_count"] == 1
    assert metrics["conflict_classes"]["alias"] == 1
    assert metrics["conflict_classes"]["invalid_only"] == 1
    assert metrics["conflict_classes"]["shared_valid"] == 1
    assert metrics["checksum_pass_rate_by_source"]["fundbox"] == 100.0
    assert metrics["checksum_pass_rate_by_source"]["speedzone_phppos"] == 0.0


def test_main_cli_wiring_with_injected_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_session = MagicMock()
    mock_client = MagicMock()
    mock_client.session.return_value.__enter__.return_value = mock_session

    d1 = nric_digest("S1234567D", TEST_KEY)
    mock_tx = MagicMock()

    def mock_run(query: str, **params: object) -> MagicMock | list[dict[str, object]]:
        if "active_person_count" in query:
            return MagicMock(single=lambda: {"active_person_count": 2})
        if "reveal_count" in query:
            return MagicMock(single=lambda: {"reveal_count": 3})
        if "FETCH_ACTIVE_PERSON_NRIC_ROWS" in query or "IDENTIFIED_BY" in query:
            return [
                {
                    "person_id": "p1",
                    "normalized_value": d1,
                    "checksum_valid": True,
                    "source_system_key": "fundbox",
                }
            ]
        return MagicMock()

    mock_tx.run = mock_run
    mock_session.execute_read = lambda fn: fn(mock_tx)

    stdout_buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout_buf)

    code = main(argv=[], client_factory=lambda: mock_client)
    assert code == 0

    output_json = json.loads(stdout_buf.getvalue())
    assert output_json["coverage_pct"] == 50.0  # 1 covered out of 2 active
    assert output_json["reveal_count"] == 3
    assert output_json["alias_count"] == 0
    assert output_json["conflict_classes"] == {
        "alias": 0,
        "invalid_only": 0,
        "shared_valid": 0,
    }
    assert output_json["checksum_pass_rate_by_source"] == {"fundbox": 100.0}


def test_main_cli_default_client_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_session = MagicMock()
    mock_client = MagicMock()
    mock_client.session.return_value.__enter__.return_value = mock_session

    mock_tx = MagicMock()
    mock_tx.run.return_value = MagicMock(single=lambda: None)
    mock_session.execute_read = lambda fn: fn(mock_tx)

    mock_client_cls = MagicMock(return_value=mock_client)
    monkeypatch.setattr("src.nric_metrics.Neo4jClient", mock_client_cls)

    stdout_buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout_buf)

    code = main(argv=[])
    assert code == 0
    assert mock_client_cls.called
    assert mock_client.close.called
