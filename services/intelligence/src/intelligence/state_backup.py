"""Atomic backup-bundle creation and strict verification helpers."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Sequence
from pathlib import Path

from intelligence.artifacts import canonical_json, sha256_file
from intelligence.models import OutputInventory, WorkspaceLayout
from intelligence.state_publication import _validate_output
from intelligence.state_schema import SCHEMA_VERSION


def create_backup(
    layout: WorkspaceLayout, database_path: Path, connection: sqlite3.Connection, destination: Path
) -> None:
    """Create an immutable atomic bundle from a durable state connection."""
    final = _safe_backup_destination(layout.backups, destination)
    temporary = layout.backups / f".{final.name}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir(mode=0o700)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            snapshot = temporary / "state.sqlite3"
            _backup_database(connection, database_path, snapshot)
            evidence = temporary / "evidence"
            entries = _backup_evidence(layout.root, connection)
            if entries:
                evidence.mkdir(mode=0o700)
            inventory: list[dict[str, object]] = []
            for source, relative in entries:
                target = evidence / relative
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                inventory.append(_inventory_item(target, f"evidence/{relative.as_posix()}"))
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "state_snapshot": _inventory_item(snapshot, "state.sqlite3"),
                "evidence": inventory,
            }
            _write_backup_manifest(temporary / "manifest.json", manifest)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        if final.exists() or final.is_symlink():
            raise FileExistsError("backup bundle already exists")
        os.rename(temporary, final)
        if os.name != "nt":
            descriptor = os.open(final.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def verify_backup_bundle(backup: Path) -> None:
    """Reject missing, partial, symlinked, overwritten, or corrupt backup evidence."""
    if backup.is_symlink() or not backup.is_dir():
        raise ValueError("backup bundle is missing or unsafe")
    _assert_tree_safe(backup)
    manifest_path = backup / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("backup bundle manifest is missing or unsafe")
    snapshot, evidence = _read_backup_manifest(manifest_path)
    _verify_inventory_item(backup, snapshot)
    _verify_backup_database(backup / "state.sqlite3")
    for item in evidence:
        _verify_inventory_item(backup, item)
    _assert_bundle_contents(backup, evidence)
    _verify_bundle_evidence(backup, evidence)


def _backup_database(
    connection: sqlite3.Connection, database_path: Path, destination: Path
) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("backup snapshot already exists")
    source = connection
    reader: sqlite3.Connection | None = None
    if connection.in_transaction:
        reader = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        source = reader
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        _verify_connection(target)
    finally:
        target.close()
        if reader is not None:
            reader.close()


def _verify_backup_database(backup: Path) -> None:
    if backup.is_symlink() or not backup.is_file():
        raise ValueError("backup must be a regular file")
    connection = sqlite3.connect(f"file:{backup}?mode=ro&immutable=1", uri=True)
    try:
        _verify_connection(connection)
    finally:
        connection.close()


def _verify_connection(connection: sqlite3.Connection) -> None:
    row = connection.execute("PRAGMA integrity_check").fetchone()
    if row is None or str(row[0]).lower() != "ok":
        raise RuntimeError("SQLite integrity check failed")


def _safe_backup_destination(backups: Path, destination: Path) -> Path:
    name = destination.name
    if (
        destination.parent not in {Path("."), backups}
        or name in {"", ".", ".."}
        or name.endswith(".sqlite3")
    ):
        raise ValueError("backup bundle name must be one safe path component")
    return backups / name


def _backup_evidence(root: Path, connection: sqlite3.Connection) -> tuple[tuple[Path, Path], ...]:
    invalid = connection.execute(
        "SELECT 1 FROM accepted_outputs AS output "
        "JOIN runs AS run ON run.id = output.run_id WHERE run.state != 'completed'"
    ).fetchone()
    if invalid is not None:
        raise RuntimeError("accepted output belongs to a non-completed run")
    rows = connection.execute(
        "SELECT r.id, r.command FROM runs AS r WHERE r.state = 'completed' ORDER BY r.id"
    )
    evidence: list[tuple[Path, Path]] = []
    for row in rows:
        run_id = str(row[0])
        manifest = root / "runs" / "manifests" / f"{run_id}.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise RuntimeError("accepted run manifest is missing or unsafe")
        outputs = tuple(
            OutputInventory(str(output[0]), str(output[1]), int(output[2]))
            for output in connection.execute(
                "SELECT relative_path, sha256, byte_count FROM accepted_outputs "
                "WHERE run_id = ? ORDER BY relative_path",
                (run_id,),
            )
        )
        _verify_run_manifest(manifest, run_id, str(row[1]), outputs)
        evidence.append((manifest, Path("manifests") / manifest.name))
        output_root = root / "outputs" / run_id
        expected = {
            Path(item.relative_path).relative_to(Path("outputs") / run_id) for item in outputs
        }
        _assert_directory_inventory(output_root, expected)
        for output in outputs:
            _validate_output(output)
            path = root / output.relative_path
            if path.stat().st_size != output.byte_count or sha256_file(path) != output.sha256:
                raise RuntimeError("accepted output evidence checksum is invalid")
            evidence.append((path, Path(output.relative_path)))
    return tuple(evidence)


def _inventory_item(path: Path, relative_path: str) -> dict[str, object]:
    return {"path": relative_path, "sha256": sha256_file(path), "byte_count": path.stat().st_size}


def _write_backup_manifest(path: Path, manifest: dict[str, object]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(canonical_json(manifest))
        handle.flush()
        os.fsync(handle.fileno())


def _read_backup_manifest(
    manifest_path: Path,
) -> tuple[dict[object, object], tuple[dict[object, object], ...]]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("backup bundle manifest is invalid") from error
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "state_snapshot", "evidence"}:
        raise ValueError("backup bundle schema is invalid")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("backup bundle schema is invalid")
    snapshot, evidence = raw.get("state_snapshot"), raw.get("evidence")
    if not isinstance(snapshot, dict) or not isinstance(evidence, list):
        raise ValueError("backup bundle inventory is invalid")
    if snapshot.get("path") != "state.sqlite3":
        raise ValueError("backup state snapshot inventory is invalid")
    items: list[dict[object, object]] = []
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("backup evidence inventory is invalid")
        path = item.get("path")
        if not isinstance(path, str) or not path.startswith("evidence/"):
            raise ValueError("backup evidence inventory path is invalid")
        items.append(item)
    return snapshot, tuple(items)


def _verify_inventory_item(root: Path, item: dict[object, object]) -> None:
    path, digest, count = item.get("path"), item.get("sha256"), item.get("byte_count")
    if not isinstance(path, str) or not isinstance(digest, str) or not isinstance(count, int):
        raise ValueError("backup inventory entry is invalid")
    relative = Path(path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("backup inventory path is invalid")
    target = root / path
    if (
        target.is_symlink()
        or not target.is_file()
        or target.stat().st_size != count
        or sha256_file(target) != digest
    ):
        raise ValueError("backup inventory checksum is invalid")


def _assert_bundle_contents(backup: Path, evidence: Sequence[dict[object, object]]) -> None:
    expected_files = {Path("manifest.json"), Path("state.sqlite3")}
    for item in evidence:
        path = item.get("path")
        if not isinstance(path, str):
            raise ValueError("backup evidence inventory is invalid")
        relative = Path(path)
        if relative in expected_files:
            raise ValueError("backup evidence inventory is duplicated")
        expected_files.add(relative)
    actual_files: set[Path] = set()
    actual_directories: set[Path] = {Path(".")}
    for candidate in backup.rglob("*"):
        relative = candidate.relative_to(backup)
        if candidate.is_dir():
            actual_directories.add(relative)
        elif candidate.is_file():
            actual_files.add(relative)
        else:
            raise ValueError("backup bundle contains unsafe filesystem evidence")
    expected_directories = {Path(".")}
    if evidence:
        expected_directories.add(Path("evidence"))
    for path in expected_files:
        parent = path.parent
        while parent != Path("."):
            expected_directories.add(parent)
            parent = parent.parent
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ValueError("backup bundle contains missing or extra evidence")


def _verify_bundle_evidence(backup: Path, inventory: Sequence[dict[object, object]]) -> None:
    connection = sqlite3.connect(f"file:{backup / 'state.sqlite3'}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        invalid = connection.execute(
            "SELECT 1 FROM accepted_outputs AS output "
            "JOIN runs AS run ON run.id = output.run_id WHERE run.state != 'completed'"
        ).fetchone()
        if invalid is not None:
            raise ValueError("backup accepted output belongs to a non-completed run")
        expected_evidence: set[Path] = set()
        rows = connection.execute(
            "SELECT id, command FROM runs WHERE state = 'completed' ORDER BY id"
        )
        for row in rows:
            run_id = str(row["id"])
            outputs = tuple(
                OutputInventory(str(output[0]), str(output[1]), int(output[2]))
                for output in connection.execute(
                    "SELECT relative_path, sha256, byte_count FROM accepted_outputs "
                    "WHERE run_id = ? ORDER BY relative_path",
                    (run_id,),
                )
            )
            for item in outputs:
                _validate_output(item)
            manifest = backup / "evidence" / "manifests" / f"{run_id}.json"
            expected_evidence.add(Path("evidence") / "manifests" / f"{run_id}.json")
            _verify_run_manifest(manifest, run_id, str(row["command"]), outputs)
            if outputs:
                output_root = backup / "evidence" / "outputs" / run_id
                expected = {
                    Path(item.relative_path).relative_to(Path("outputs") / run_id)
                    for item in outputs
                }
                _assert_directory_inventory(output_root, expected)
            for item in outputs:
                evidence = backup / "evidence" / item.relative_path
                expected_evidence.add(Path("evidence") / item.relative_path)
                if (
                    evidence.stat().st_size != item.byte_count
                    or sha256_file(evidence) != item.sha256
                ):
                    raise ValueError("backup accepted output checksum is invalid")
        actual_evidence = {
            Path(path)
            for item in inventory
            for path in (item.get("path"),)
            if isinstance(path, str)
        }
        if actual_evidence != expected_evidence:
            raise ValueError("backup evidence inventory does not match durable state")
    except sqlite3.Error as error:
        raise ValueError("backup state evidence is invalid") from error
    finally:
        connection.close()


def _verify_run_manifest(
    path: Path, run_id: str, command: str, outputs: Sequence[OutputInventory]
) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("accepted run manifest is missing or unsafe")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("accepted run manifest is invalid") from error
    if not isinstance(raw, dict):
        raise ValueError("accepted run manifest is invalid")
    if (
        raw.get("run_id") != run_id
        or raw.get("command") != command
        or raw.get("state") != "completed"
    ):
        raise ValueError("accepted run manifest does not match durable state")
    entries = raw.get("outputs")
    if not isinstance(entries, list):
        raise ValueError("accepted run manifest output inventory is invalid")
    manifest_outputs: list[OutputInventory] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("accepted run manifest output inventory is invalid")
        relative, digest, count = entry.get("path"), entry.get("sha256"), entry.get("byte_count")
        if (
            not isinstance(relative, str)
            or not isinstance(digest, str)
            or not isinstance(count, int)
        ):
            raise ValueError("accepted run manifest output inventory is invalid")
        item = OutputInventory(relative, digest, count)
        _validate_output(item)
        manifest_outputs.append(item)
    if tuple(sorted(manifest_outputs, key=lambda item: item.relative_path)) != tuple(outputs):
        raise ValueError("accepted run manifest does not match accepted outputs")


def _assert_directory_inventory(directory: Path, expected: set[Path]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("accepted output directory is missing or unsafe")
    actual: set[Path] = set()
    for candidate in directory.rglob("*"):
        relative = candidate.relative_to(directory)
        if candidate.is_symlink():
            raise ValueError("accepted output directory contains a symbolic link")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ValueError("accepted output directory contains unsafe evidence")
        actual.add(relative)
    if actual != expected:
        raise ValueError("accepted output directory contains missing or extra evidence")


def _contains_secret(value: str) -> bool:
    return any(
        marker in value.lower()
        for marker in ("secret", "token", "password", "credential", "authorization")
    )


def _assert_tree_safe(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("backup bundle contains a symbolic link")
