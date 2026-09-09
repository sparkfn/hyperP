"""Synthetic State/artifact integration and tamper contracts for dataset publications."""

from __future__ import annotations

from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json, write_manifest
from intelligence.artifacts_staging import publish_inventory, scan_staged_outputs
from intelligence.datasets.artifact_codec import descriptor_relative_path
from intelligence.datasets.artifact_io import inventory
from intelligence.datasets.artifacts import (
    _descriptor,
    parse_descriptor,
    verify_dataset,
    write_dataset,
)
from intelligence.datasets.catalog import find, find_run, list_entries
from intelligence.datasets.cli import _dataset_id_for_run
from intelligence.datasets.definition import compute
from intelligence.datasets.models import canonical_digest, current_config_compatible, parse_config
from intelligence.state import State
from test_datasets_definition import _inputs


def _publish_dataset(workspace: Path) -> tuple[str, object]:
    state = State(workspace)
    try:
        run = state.create_mutating_run("dataset_build")
        staging = state.layout.staging / run.run_id
        staging.mkdir(mode=0o700, parents=True)
        artifact = write_dataset(staging, _inputs(True), compute(_inputs(True)))
        inventory = scan_staged_outputs(workspace, run.run_id, 100_000_000)
        state.mark_execution_quiescent(run)
        state.begin_publishing(run, inventory)
        published = publish_inventory(workspace, run.run_id, inventory, 100_000_000)
        state.complete_publication(run, published, {"dataset_id": artifact.descriptor.dataset_id})
        write_manifest(
            workspace,
            run.run_id,
            "dataset_build",
            "completed",
            outputs=published,
            created_at=run.created_at,
            started_at=run.started_at,
            limits=dict(run.limits),
        )
        return run.run_id, artifact.descriptor
    finally:
        state.close()


def test_state_registered_artifact_catalog_and_read_only_inspection(tmp_path: Path) -> None:
    run_id, descriptor = _publish_dataset(tmp_path)
    database = tmp_path / "state" / "state.sqlite3"
    before = database.read_bytes()
    entry = find(tmp_path, descriptor.dataset_id, run_id)
    assert entry.manifest["dataset_id"] == descriptor.dataset_id
    assert find_run(tmp_path, run_id).descriptor == descriptor
    assert list_entries(tmp_path, 1, None)[0].run_id == run_id
    assert database.read_bytes() == before


def test_artifact_checksum_and_config_tampering_fail_closed(tmp_path: Path) -> None:
    run_id, descriptor = _publish_dataset(tmp_path)
    root = tmp_path / "outputs" / run_id / "datasets" / descriptor.dataset_id
    assert verify_dataset(root, descriptor)["dataset_id"] == descriptor.dataset_id
    config = root / "config.json"
    config.write_text(canonical_json({"arbitrary": True}), encoding="utf-8")
    with pytest.raises(ValueError):
        verify_dataset(root, descriptor)
    bad = dict(descriptor.as_dict())
    bad["inputs"] = {"arbitrary": True}
    with pytest.raises(ValueError):
        parse_descriptor(bad)


def test_config_contract_rejects_unreviewed_fingerprint_and_input_pins() -> None:
    config = _inputs(False).config()
    assert parse_config(config) == config
    fingerprint = dict(config)
    fingerprint["code_contract_fingerprint"] = "0" * 64
    assert parse_config(fingerprint)["code_contract_fingerprint"] == "0" * 64
    with pytest.raises(ValueError, match="current executable"):
        current_config_compatible(fingerprint)
    pins = dict(config)
    pins["inputs"] = dict(config["inputs"])
    pins["inputs"]["deal_refs"] = {"run_id": "run"}
    with pytest.raises(ValueError, match="pin"):
        parse_config(pins)


def test_build_lookup_uses_exact_run_resolution_not_a_first_hundred_page(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_id, descriptor = _publish_dataset(tmp_path)

    def unexpected_page(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("global first-page catalog lookup is forbidden")

    monkeypatch.setattr("intelligence.datasets.cli.list_entries", unexpected_page)
    assert _dataset_id_for_run(tmp_path, run_id) == descriptor.dataset_id


def test_failed_incomplete_attempt_cannot_hide_prior_accepted_dataset(tmp_path: Path) -> None:
    accepted_run, descriptor = _publish_dataset(tmp_path)
    state = State(tmp_path)
    try:
        failed = state.create_mutating_run("dataset_build")
        staging = state.layout.staging / failed.run_id
        staging.mkdir(mode=0o700, parents=True)
        (staging / "partial.txt").write_text("incomplete", encoding="utf-8")
        state.mark_execution_quiescent(failed)
        state.terminal(failed, "failed", {"reason": "synthetic_incomplete_publication"})
    finally:
        state.close()
    assert find(tmp_path, descriptor.dataset_id, accepted_run).run_id == accepted_run
    assert not (tmp_path / "outputs" / failed.run_id).exists()


def test_rehashed_descriptor_request_and_path_linkage_fail_closed(tmp_path: Path) -> None:
    _run_id, descriptor = _publish_dataset(tmp_path)
    raw = descriptor.as_dict()
    raw["request_digest"] = "0" * 64
    unsigned = dict(raw)
    unsigned.pop("digest")
    raw["digest"] = canonical_digest(unsigned)
    with pytest.raises(ValueError, match="request linkage"):
        parse_descriptor(raw)
    with pytest.raises(ValueError):
        descriptor_relative_path("../outside")


@pytest.mark.parametrize(
    ("filename", "field", "value"),
    (("rows.ndjson", "activity_coverage", "invalid"), ("dispositions.ndjson", "kind", "invalid")),
)
def test_fully_rehashed_schema_invalid_logical_content_is_rejected(
    tmp_path: Path,
    filename: str,
    field: str,
    value: str,
) -> None:
    run_id, descriptor = _publish_dataset(tmp_path)
    old_root = tmp_path / "outputs" / run_id / "datasets" / descriptor.dataset_id
    lines = old_root.joinpath(filename).read_text(encoding="utf-8").splitlines()
    item = __import__("json").loads(lines[0])
    item[field] = value
    old_root.joinpath(filename).write_text(canonical_json(item) + "\n", encoding="utf-8")
    logical = {
        "config": __import__("json").loads((old_root / "config.json").read_text()),
        "definition": __import__("json").loads((old_root / "definition.json").read_text()),
        "dispositions": [
            __import__("json").loads(line)
            for line in (old_root / "dispositions.ndjson").read_text().splitlines()
        ],
        "rows": [
            __import__("json").loads(line)
            for line in (old_root / "rows.ndjson").read_text().splitlines()
        ],
        "schema": __import__("json").loads((old_root / "schema.json").read_text()),
    }
    content = canonical_digest(logical)
    dataset_id = f"crm-deal-state-v1-{content[:24]}"
    root = old_root.with_name(dataset_id)
    old_root.rename(root)
    manifest = __import__("json").loads((root / "manifest.json").read_text())
    manifest["dataset_id"] = dataset_id
    manifest["content_digest"] = content
    manifest["payload_inventory"] = [
        {"byte_count": item.byte_count, "relative_path": item.relative_path, "sha256": item.sha256}
        for item in inventory(root, dataset_id, False)
    ]
    (root / "manifest.json").write_text(canonical_json(manifest), encoding="utf-8")
    rewritten = _descriptor(
        run_id, dataset_id, _inputs(True), manifest, inventory(root, dataset_id, True)
    )
    with pytest.raises(ValueError, match="schema|value"):
        verify_dataset(root, rewritten)


@pytest.mark.parametrize(
    ("primary", "reason"),
    (("feature_included", "unexpected"), ("join_excluded", None), ("ambiguous_version", None)),
)
def test_disposition_reason_semantics_reject_fully_rehashed_invalid_evidence(
    tmp_path: Path,
    primary: str,
    reason: str | None,
) -> None:
    from intelligence.datasets.artifact_validation import logical_schemas

    logical = {
        "rows": [compute(_inputs(True)).rows[0].as_dict()],
        "dispositions": [
            {
                "kind": "activity" if primary != "ambiguous_version" else "deal_version",
                "primary_disposition": primary,
                "reason_code": reason,
                "source_record_pk": "pk",
            }
        ],
    }
    with pytest.raises(ValueError, match="reason"):
        logical_schemas(logical)
