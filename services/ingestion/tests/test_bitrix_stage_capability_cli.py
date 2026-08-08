from __future__ import annotations

import argparse
import json
import re
from contextlib import nullcontext
from pathlib import Path

import pytest
import src.bitrix_stage_capability as capability_cli
from src.bitrix_stage_capability import (
    _collect_manifests,
    _positive_float,
    _recommendation,
    _write_json,
    build_parser,
)
from src.connectors.bitrix_openlines.crm_status_catalog import (
    CrmStageCatalogSemanticContractError,
)
from src.connectors.bitrix_stage_history.models import ProbeLimits
from src.connectors.bitrix_stage_history.probe import PassManifest
from src.connectors.bitrix_stage_history.spool import RestrictedSpool


@pytest.mark.parametrize("value", ["nan", "inf", "+inf", "-inf"])
def test_positive_float_rejects_non_finite_values(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="finite"):
        _positive_float(value)


def test_recommendation_requires_every_machine_predicate() -> None:
    assert _recommendation(True, True, True, "verified_keyset") == "verified_keyset"
    assert _recommendation(True, True, True, "bounded_spool_reconcile") == (
        "bounded_spool_reconcile"
    )
    assert _recommendation(True, True, False, "verified_keyset") == "unsupported"
    assert _recommendation(False, True, True, "verified_keyset") == "unsupported"


def test_parser_names_resource_limits_as_per_pass(tmp_path: Path) -> None:
    arguments = [
        "--source-contract-id",
        "123E4567-E89B-12D3-A456-426614174000",
        "--restricted-output-dir",
        str(tmp_path / "restricted"),
        "--max-calls-per-pass",
        "3",
        "--max-rows-per-pass",
        "100",
        "--max-spool-bytes-per-pass",
        "1000000",
        "--max-runtime-seconds-per-pass",
        "5",
    ]

    parsed = build_parser().parse_args(arguments)

    assert parsed.source_contract_id == "123e4567-e89b-12d3-a456-426614174000"
    assert parsed.max_calls_per_pass == 3
    assert parsed.max_rows_per_pass == 100
    assert parsed.max_spool_bytes_per_pass == 1_000_000
    assert parsed.max_runtime_seconds_per_pass == 5


def test_evidence_writer_creates_restricted_file_exclusively(tmp_path: Path) -> None:
    path = tmp_path / "evidence-manifest.json"

    _write_json(path, {"status": "first"})

    assert path.read_text(encoding="utf-8") == '{"status":"first"}\n'
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        _write_json(path, {"status": "second"})
    assert path.read_text(encoding="utf-8") == '{"status":"first"}\n'


def test_evidence_writer_does_not_follow_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("unchanged", encoding="utf-8")
    path = tmp_path / "evidence-manifest.json"
    path.symlink_to(target)

    with pytest.raises(FileExistsError):
        _write_json(path, {"status": "replacement"})

    assert target.read_text(encoding="utf-8") == "unchanged"


def test_managed_artifact_client_closes_after_collection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        closed = False

        def close(self) -> None:
            self.closed = True

    client = Client()
    monkeypatch.setattr(capability_cli, "_client", lambda: client)

    with pytest.raises(RuntimeError, match="collection failed"):
        with capability_cli._managed_client():
            raise RuntimeError("collection failed")

    assert client.closed is True


def test_manifest_collection_cleans_prior_spools_when_later_pass_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_directory = tmp_path / "restricted"
    output_directory.mkdir(mode=0o700)
    first_spool = RestrictedSpool(output_directory, 1)
    first_manifest = PassManifest(
        traversal_mode="offset",
        calls=1,
        raw_rows=0,
        unique_identity_rows=0,
        duplicate_same_hash_rows=0,
        duplicate_conflict_rows=0,
        pages=1,
        source_total=0,
        source_total_consistent=True,
        source_total_matches_rows=True,
        history_id_ordering=None,
        minimum_history_id=None,
        maximum_history_id=None,
        identity_hash_digest="sha256:empty",
        runtime_seconds=0.1,
        spool_bytes=first_spool.path.stat().st_size,
    )
    calls = 0

    def collect_once_then_fail(
        *_args: object, **_kwargs: object
    ) -> tuple[PassManifest, RestrictedSpool]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return first_manifest, first_spool
        raise RuntimeError("simulated second-pass failure")

    monkeypatch.setattr(capability_cli, "collect_stage_history_pass", collect_once_then_fail)

    with pytest.raises(RuntimeError, match="second-pass"):
        _collect_manifests(
            object(),
            source_contract_id="123e4567-e89b-12d3-a456-426614174000",
            entity_type_id=2,
            filters={},
            limits=ProbeLimits(2, 10, 1_000_000, 5, 2, 2),
            output_directory=output_directory,
            traversal_mode="offset",
        )

    assert not first_spool.path.exists()


def test_run_emits_redacted_v2_combined_re_gate_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, datetime

    from src.connectors.bitrix_openlines.models import (
        CrmDealCapabilityItem,
        CrmDealCapabilityPage,
        CrmDealStageCatalogItem,
        CrmDealStageCatalogPage,
    )
    from src.connectors.bitrix_stage_history.models import StageHistoryItem, StageHistoryPage

    class Client:
        def __init__(self) -> None:
            self.deal_calls: list[dict[str, object]] = []
            self.stage_calls: list[dict[str, object]] = []

        def list_crm_deal_capability_page(self, **kwargs: object) -> CrmDealCapabilityPage:
            self.deal_calls.append(kwargs)
            item = CrmDealCapabilityItem("501", "2", "C2:NEW")
            pages = (
                CrmDealCapabilityPage((item,), None, 1, None, None),
                CrmDealCapabilityPage((item,), None, 1, None, None),
                CrmDealCapabilityPage((item,), None, 1, None, None),
            )
            return pages[len(self.deal_calls) - 1]

        def list_crm_deal_stage_catalog_page(self, **_kwargs: object) -> CrmDealStageCatalogPage:
            return CrmDealStageCatalogPage(
                (CrmDealStageCatalogItem("2", "C2:NEW", "process"),),
                None,
                1,
                None,
                None,
            )

        def list_stage_history_page(self, **kwargs: object) -> StageHistoryPage:
            self.stage_calls.append(kwargs)
            item = StageHistoryItem(
                history_id="11",
                entity_type_id="2",
                owner_id="501",
                type_id="1",
                created_time=datetime(2026, 8, 7, tzinfo=UTC),
                created_time_source="2026-08-07T00:00:00+00:00",
                category_id="2",
                stage_semantic_id="P",
                stage_id="C2:NEW",
                raw_payload={"ID": "11"},
            )
            pages = (
                StageHistoryPage((item,), None, 1, None, None),
                StageHistoryPage((item,), None, 1, None, None),
                StageHistoryPage((item,), None, 1, None, None),
            )
            return pages[len(self.stage_calls) - 1]

        def close(self) -> None:
            return None

    client = Client()
    monkeypatch.setattr(capability_cli, "_client", lambda: client)
    monkeypatch.setattr(capability_cli, "_capability_run_lock", nullcontext)
    monkeypatch.setattr(capability_cli, "portal_fingerprint", lambda *_args: "hmac-sha256:portal")
    monkeypatch.setattr(
        capability_cli,
        "effective_config_fingerprint",
        lambda *_args: "hmac-sha256:config",
    )

    result = capability_cli.run(
        [
            "--source-contract-id",
            "123E4567-E89B-12D3-A456-426614174000",
            "--category-id",
            "2",
            "--restricted-output-dir",
            str(tmp_path / "restricted"),
            "--max-calls-per-pass",
            "3",
            "--max-rows-per-pass",
            "100",
            "--max-spool-bytes-per-pass",
            "1000000",
            "--max-runtime-seconds-per-pass",
            "5",
            "--retain-spool",
        ]
    )

    assert result == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["report_schema_version"] == "bitrix-source-capability-v3"
    assert summary["human_approval_required"] is True
    assert summary["approved_traversal_outcome"] is None
    # This fixture intentionally omits image, cadence, and operating metadata.
    assert summary["deal_owner_census"]["recommended_traversal_outcome"] == "unsupported"
    assert summary["global_stage_history"]["recommended_traversal_outcome"] == "unsupported"
    assert summary["current_stage_catalog"]["machine_qualified"] is True
    assert summary["deal_owner_census"]["owner_manifest_digest"].startswith("hmac-sha256:")
    assert summary["global_stage_history"]["frozen_owner_manifest_digest"].startswith(
        "hmac-sha256:"
    )
    assert summary["global_stage_history"]["pass_manifests"][0][
        "upper_history_id_digest"
    ].startswith("hmac-sha256:")
    assert re.search(r"(?<![0-9A-Za-z])501(?![0-9A-Za-z])", json.dumps(summary)) is None
    assert client.stage_calls[0]["order_direction"] == "DESC"
    assert client.stage_calls[0]["filters"] == {}
    assert client.stage_calls[1]["filters"] == {"<=ID": "11"}
    assert (tmp_path / "restricted" / "final-evidence-summary.json").exists()


@pytest.mark.parametrize(
    ("preflight_error", "expected_reason"),
    [
        (
            CrmStageCatalogSemanticContractError("source-value-which-must-not-leak"),
            "current_stage_catalog_semantic_contract_violation",
        ),
        (
            RuntimeError("different-source-value-which-must-not-leak"),
            "current_stage_catalog_preflight_failed",
        ),
    ],
)
def test_catalog_preflight_writes_a_redacted_failure_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preflight_error: RuntimeError,
    expected_reason: str,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.deal_calls = 0
            self.stage_calls = 0

        def list_crm_deal_capability_page(self, **_kwargs: object) -> object:
            self.deal_calls += 1
            raise AssertionError("deal census must not start after catalog preflight failure")

        def list_stage_history_page(self, **_kwargs: object) -> object:
            self.stage_calls += 1
            raise AssertionError("stage traversal must not start after catalog preflight failure")

        def close(self) -> None:
            return None

    client = Client()
    monkeypatch.setattr(capability_cli, "_client", lambda: client)
    monkeypatch.setattr(capability_cli, "_capability_run_lock", nullcontext)
    monkeypatch.setattr(capability_cli, "portal_fingerprint", lambda *_args: "hmac-sha256:portal")
    monkeypatch.setattr(
        capability_cli,
        "effective_config_fingerprint",
        lambda *_args: "hmac-sha256:config",
    )

    def fail_catalog_preflight(*_args: object, **_kwargs: object) -> object:
        raise preflight_error

    monkeypatch.setattr(capability_cli, "collect_current_stage_catalog", fail_catalog_preflight)
    output_directory = tmp_path / "restricted"

    with pytest.raises(type(preflight_error)):
        capability_cli.run(
            [
                "--source-contract-id",
                "123E4567-E89B-12D3-A456-426614174000",
                "--category-id",
                "2",
                "--restricted-output-dir",
                str(output_directory),
                "--max-calls-per-pass",
                "3",
                "--max-rows-per-pass",
                "100",
                "--max-spool-bytes-per-pass",
                "1000000",
                "--max-runtime-seconds-per-pass",
                "5",
                "--deployment-image-digest",
                "sha256:" + ("a" * 64),
            ]
        )

    manifest_path = output_directory / "failure-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    encoded = json.dumps(manifest, sort_keys=True)
    assert manifest["failure_phase"] == "current_stage_catalog_preflight"
    assert manifest["failure_reason"] == expected_reason
    assert manifest["exception_type"] == type(preflight_error).__name__
    assert manifest["traversal_outcome"] == "unsupported"
    assert manifest["included_deal_category_count"] == 1
    assert manifest["provenance"]["portal_origin_digest"] == "hmac-sha256:portal"
    assert manifest["provenance"]["effective_ingestion_config_digest"] == "hmac-sha256:config"
    assert manifest["provenance"]["deployment_image_digest"] == "sha256:" + ("a" * 64)
    assert "source-value-which-must-not-leak" not in encoded
    assert "different-source-value-which-must-not-leak" not in encoded
    assert manifest_path.stat().st_mode & 0o077 == 0
    assert not (output_directory / "capability-redaction-key.bin").exists()
    assert not (output_directory / "final-evidence-summary.json").exists()
    assert client.deal_calls == 0
    assert client.stage_calls == 0
