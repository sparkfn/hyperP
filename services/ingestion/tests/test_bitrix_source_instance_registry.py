from __future__ import annotations

import pytest
from src.graph.bitrix_source_instances import (
    BITRIX_SOURCE_KEY,
    BitrixSourceInstanceDisabledError,
    BitrixSourceInstanceMissingError,
    BitrixSourceInstanceRepository,
)
from src.graph.queries.bitrix_source_instances import (
    ADMIT_BITRIX_CONTROL_INSTANCE,
    CREATE_BITRIX_SOURCE_INSTANCE_CONSTRAINTS,
    DISABLE_BITRIX_SOURCE_INSTANCE,
    REGISTER_BITRIX_SOURCE_INSTANCE,
)
from src.source_instances import (
    LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
    effective_control_instance_id,
    scope_control_identity,
)


def test_registry_has_only_composite_source_and_instance_uniqueness() -> None:
    schema = "\n".join(CREATE_BITRIX_SOURCE_INSTANCE_CONSTRAINTS)
    assert "(instance.source_key, instance.source_instance_id)" in schema
    assert "REQUIRE instance.source_instance_id IS UNIQUE" not in schema
    assert "REQUIRE instance.source_key IS UNIQUE" not in schema


def test_registry_queries_keep_identity_immutable_and_do_not_store_secrets() -> None:
    assert "SET instance.source_instance_id" not in REGISTER_BITRIX_SOURCE_INSTANCE
    assert "legacy-default" in DISABLE_BITRIX_SOURCE_INSTANCE
    assert "url" not in REGISTER_BITRIX_SOURCE_INSTANCE.lower()
    assert "authorization" not in REGISTER_BITRIX_SOURCE_INSTANCE.lower()


def test_registry_rejects_other_source_families_before_graph_access() -> None:
    repository = BitrixSourceInstanceRepository(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="source_key='bitrix_chat'"):
        repository.register("whatsapp_chat", "tenant-a")


def test_control_scope_preserves_legacy_and_isolates_future_instances() -> None:
    assert effective_control_instance_id(None) == LEGACY_DEFAULT_CONTROL_INSTANCE_ID
    assert scope_control_identity("bitrix-live:abc", "legacy-default") == "bitrix-live:abc"
    assert scope_control_identity("bitrix-live:abc", "tenant-a") != scope_control_identity(
        "bitrix-live:abc", "tenant-b"
    )
    assert BITRIX_SOURCE_KEY == "bitrix_chat"


def test_registry_admission_and_disable_cover_control_state() -> None:
    assert "INSTANCE_OF" in ADMIT_BITRIX_CONTROL_INSTANCE
    assert "is_active: true" in ADMIT_BITRIX_CONTROL_INSTANCE
    assert "size(controls) = 1" in ADMIT_BITRIX_CONTROL_INSTANCE
    assert "size(sources) = 1" in ADMIT_BITRIX_CONTROL_INSTANCE
    assert "size(dispatches) <= 1" in ADMIT_BITRIX_CONTROL_INSTANCE
    assert "dispatch.active_generation_id IS NOT NULL" in DISABLE_BITRIX_SOURCE_INSTANCE
    assert "dispatch.active_owner IS NOT NULL" in DISABLE_BITRIX_SOURCE_INSTANCE
    assert (
        "generation.status IN ['allocated', 'backfilling', 'activating', 'active']"
        in DISABLE_BITRIX_SOURCE_INSTANCE
    )


def test_disable_rejects_active_api_or_worker_runs_before_lifecycle_change() -> None:
    from pathlib import Path

    assert "OPTIONAL MATCH (run:IngestRun {control_instance_id: $source_instance_id})" in (
        DISABLE_BITRIX_SOURCE_INSTANCE
    )
    assert (
        "run.status IN ['queued', 'started', 'running', 'stop_requested', 'paused_with_checkpoint']"
        in (DISABLE_BITRIX_SOURCE_INSTANCE)
    )
    assert "active_runs = 0" in DISABLE_BITRIX_SOURCE_INSTANCE
    source = (Path(__file__).parents[1] / "src/graph/bitrix_source_instances.py").read_text()
    assert "self.require_active(BITRIX_SOURCE_KEY, slug)" in source


class _RegistryResult:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def single(self) -> dict[str, object]:
        return self._row


class _RegistryTransaction:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def run(self, _query: str, **_params: object) -> _RegistryResult:
        return _RegistryResult(self._row)


class _DisableClassificationClient:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row
        self.write_called = False

    def execute_read(self, work: object) -> object:
        return work(_RegistryTransaction(self._row))  # type: ignore[operator]

    def execute_write(self, _work: object) -> object:
        self.write_called = True
        raise AssertionError(
            "disable mutation must not run after registration classification fails"
        )


@pytest.mark.parametrize(
    ("row", "error"),
    (
        (
            {
                "matches": 0,
                "statuses": [],
                "source_matches": 0,
                "relationship_count": 0,
                "source_keys": [],
                "source_active": [],
            },
            BitrixSourceInstanceMissingError,
        ),
        (
            {
                "matches": 1,
                "statuses": ["disabled"],
                "source_matches": 1,
                "relationship_count": 1,
                "source_keys": ["bitrix_chat"],
                "source_active": [True],
            },
            BitrixSourceInstanceDisabledError,
        ),
    ),
)
def test_disable_preserves_missing_and_disabled_registration_errors_before_mutation(
    row: dict[str, object], error: type[Exception]
) -> None:
    client = _DisableClassificationClient(row)
    repository = BitrixSourceInstanceRepository(client)  # type: ignore[arg-type]

    with pytest.raises(error):
        repository.disable("bitrix_chat", "portal-a", "operator", "retire")

    assert client.write_called is False


def test_admission_checks_exact_schema_before_registry_or_dispatch_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.graph import ingestion_control_instance_migration as migration

    events: list[str] = []

    class Result:
        def single(self) -> dict[str, str]:
            return {
                "control_instance_id": "legacy-default",
                "source_instance_id": "legacy-default",
            }

    class Transaction:
        def run(self, query: str, **_params: object) -> Result:
            assert query == ADMIT_BITRIX_CONTROL_INSTANCE
            events.append("registry")
            return Result()

    class Client:
        def execute_read(self, work: object) -> object:
            return work(Transaction())  # type: ignore[operator]

    monkeypatch.setattr(
        migration,
        "assert_ingestion_control_ready",
        lambda _client: events.append("schema"),
    )
    repository = BitrixSourceInstanceRepository(Client())  # type: ignore[arg-type]

    repository.admit(
        control_instance_id="legacy-default",
        source_instance_id="legacy-default",
    )

    assert events == ["schema", "registry"]


def test_schema_readiness_failure_prevents_registry_or_dispatch_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.graph import ingestion_control_instance_migration as migration

    class Client:
        def execute_read(self, _work: object) -> object:
            raise AssertionError("registry admission must not run after schema failure")

    monkeypatch.setattr(
        migration,
        "assert_ingestion_control_ready",
        lambda _client: (_ for _ in ()).throw(RuntimeError("schema missing")),
    )
    repository = BitrixSourceInstanceRepository(Client())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="schema missing"):
        repository.admit(
            control_instance_id="legacy-default",
            source_instance_id="legacy-default",
        )
