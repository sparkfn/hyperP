"""Bounded WhatsAdmin adapter: durable continuation and commit ordering."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _whatsadmin_bounded_fixture import (
    CREDENTIAL,
    ENTITY,
    SESSION_ID,
    UPPER_BOUND,
    FakeBoundedClient,
    FakePipeline,
    FakeState,
    RecordingBundleExtraction,
    apply_one,
    chat_page,
    cursor_subphase,
    drive,
    drive_until,
    envelope,
    session_page,
    source_window,
)
from src.connectors.whatsadmin_api.bounded_connector import (
    WhatsAdminBoundedConnector,
    initial_checkpoint,
)
from src.connectors.whatsadmin_api.bounded_state import (
    WhatsAdminBoundedState,
    WhatsAdminCursor,
    WhatsAdminWindow,
    bundle_from_stored,
    window_from_source_window,
)
from src.connectors.whatsadmin_api.bounded_writer import WhatsAdminBoundedWriter
from src.connectors.whatsadmin_api.models import PreparedChat
from src.connectors.whatsadmin_api.watermark import (
    committed_version_key,
    session_watermark_key,
)
from src.resumable import CheckpointDescriptor

CHAT_A = "6581111111@c.us"
CHAT_B = "6582222222@c.us"


def _connector(
    client: FakeBoundedClient,
    state: FakeState,
    extraction: RecordingBundleExtraction,
    *,
    max_bytes_per_unit: int = 2_000_000,
) -> WhatsAdminBoundedConnector:
    return WhatsAdminBoundedConnector(
        entity_key=ENTITY,
        client=client,
        credential=CREDENTIAL,
        state=state,  # type: ignore[arg-type]
        window=_window(),
        legacy_entity=None,
        max_records_per_unit=50,
        max_bytes_per_unit=max_bytes_per_unit,
        max_extraction_calls_per_unit=40,
        clock=_clock,
    )


def _clock() -> datetime:
    """Return the fixture clock inside the bounded occurrence window."""
    return datetime(2026, 9, 17, 5, 30, tzinfo=UTC)


def _window() -> WhatsAdminWindow:
    return window_from_source_window(source_window())


def _writer(state: FakeState, pipeline: FakePipeline) -> WhatsAdminBoundedWriter:
    return WhatsAdminBoundedWriter(
        state_factory=lambda _context: state,  # type: ignore[arg-type,return-value]
        pipeline_factory=lambda _control: pipeline,  # type: ignore[arg-type,return-value]
    )


def _checkpoint() -> CheckpointDescriptor:
    return initial_checkpoint(
        scope_window=source_window(),
        credential=CREDENTIAL,
        connector_version="whatsadmin-bounded-v1",
        schema_version=1,
    )


def _start(monkeypatch: pytest.MonkeyPatch, extraction: RecordingBundleExtraction) -> None:
    monkeypatch.setattr(
        "src.connectors.whatsadmin_api.bounded_connector.process_whatsapp_bundles",
        extraction,
    )


def _two_chat_client() -> FakeBoundedClient:
    return FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,))],
        chat_pages={SESSION_ID: [chat_page((CHAT_A, CHAT_B))]},
    )


def test_walk_commits_each_chat_version_inside_the_commit_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(
        envelopes={CHAT_A: [envelope(CHAT_A)], CHAT_B: [envelope(CHAT_B)]}
    )
    _start(monkeypatch, extraction)
    state = FakeState()
    pipeline = FakePipeline()
    client = _two_chat_client()

    walked = drive(
        _connector(client, state, extraction),
        _writer(state, pipeline),
        checkpoint=_checkpoint(),
    )

    assert walked.finished is True
    assert walked.subphases() == ["sessions", "chats", "extract", "commit", "extract", "commit"]
    assert walked.dispositions == ("committed", "committed")
    assert [item["source_record_id"] for item in pipeline.envelopes] == [
        envelope(CHAT_A)["source_record_id"],
        envelope(CHAT_B)["source_record_id"],
    ]
    # Every envelope was written through the same bounded commit transaction.
    assert len(set(id(tx) for tx in pipeline.transactions)) == 1
    assert cursor_subphase(walked.checkpoint) == "terminal"
    assert extraction.calls == [CHAT_A, CHAT_B]
    assert walked.extraction_calls == 2


def test_walk_records_committed_versions_and_terminal_watermarks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={CHAT_A: [envelope(CHAT_A)]})
    _start(monkeypatch, extraction)
    state = FakeState()
    pipeline = FakePipeline()
    client = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,))],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,))]},
    )

    walked = drive(
        _connector(client, state, extraction),
        _writer(state, pipeline),
        checkpoint=_checkpoint(),
    )

    assert walked.finished is True
    version = state.committed_version(ENTITY, SESSION_ID, CHAT_A)
    assert version is not None
    assert version.startswith("sha256:")
    watermark_key = session_watermark_key(ENTITY, SESSION_ID)
    assert state.entries[watermark_key] == UPPER_BOUND
    assert (watermark_key, UPPER_BOUND, "completed") in state.writes


def test_terminal_watermark_is_refused_without_terminal_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={CHAT_A: [envelope(CHAT_A)]})
    _start(monkeypatch, extraction)
    state = FakeState()
    pipeline = FakePipeline()
    client = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,))],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,))]},
    )
    _ = drive_until(
        _connector(client, state, extraction),
        _writer(state, pipeline),
        checkpoint=_checkpoint(),
        subphase="commit",
    )

    view = state.bind_transaction(object(), terminal_authorized=False)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="terminal authorization"):
        view.set(session_watermark_key(ENTITY, SESSION_ID), UPPER_BOUND, status="completed")


def test_changed_chat_version_is_extracted_and_unchanged_version_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={CHAT_A: [envelope(CHAT_A)]})
    _start(monkeypatch, extraction)
    state = FakeState()
    pipeline = FakePipeline()
    connector = _connector(_two_chat_client(), state, extraction)
    writer = _writer(state, pipeline)

    first = drive(connector, writer, checkpoint=_checkpoint())
    assert first.finished is True
    assert extraction.calls == [CHAT_A]

    # A second window over the same unchanged chat versions must not re-extract.
    second = drive(
        _connector(_two_chat_client(), state, extraction),
        writer,
        checkpoint=_checkpoint(),
    )
    assert second.finished is True
    assert extraction.calls == [CHAT_A]
    assert pipeline.envelopes == [pipeline.envelopes[0]]
    assert "committed" not in second.dispositions


def test_extraction_failure_persists_a_durable_retry_and_holds_the_subphase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(
        envelopes={CHAT_A: [envelope(CHAT_A)]},
        failures={CHAT_A: "malformed_response"},
    )
    _start(monkeypatch, extraction)
    state = FakeState()
    pipeline = FakePipeline()
    client = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,))],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,))]},
    )

    walked = drive(
        _connector(client, state, extraction),
        _writer(state, pipeline),
        checkpoint=_checkpoint(),
        stop_on_obligation=True,
    )

    assert walked.finished is False
    assert walked.subphases()[-1] == "extract"
    last = walked.steps[-1]
    assert last.dispositions == ("durable_retry",)
    assert last.obligations == (last.replay_id,)
    assert last.records == 1
    assert pipeline.envelopes == []
    cursor = WhatsAdminCursor.from_payload(walked.checkpoint.cursor)
    assert cursor.subphase == "extract"
    assert cursor.retry_replay_id == last.replay_id
    assert cursor.extract_attempts == 1


def test_readmission_after_a_retry_resolves_the_obligation_with_a_stable_replay_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(
        envelopes={CHAT_A: [envelope(CHAT_A)]},
        failures={CHAT_A: "malformed_response"},
    )
    _start(monkeypatch, extraction)
    state = FakeState()
    pipeline = FakePipeline()
    client = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,))],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,))]},
    )
    connector = _connector(client, state, extraction)

    failed = drive(
        connector,
        _writer(state, pipeline),
        checkpoint=_checkpoint(),
        stop_on_obligation=True,
    )
    retry_replay_id = failed.steps[-1].replay_id

    extraction.failures.clear()
    resumed = drive(
        _connector(client, state, extraction),
        _writer(state, pipeline),
        checkpoint=failed.checkpoint,
        generation=2,
    )

    assert resumed.finished is True
    extract_step = resumed.steps[0]
    assert extract_step.subphase == "extract"
    assert extract_step.replay_id == retry_replay_id
    assert extract_step.resolutions == (retry_replay_id,)
    assert resumed.dispositions == ("committed",)
    assert pipeline.envelopes == [pipeline.envelopes[0]]


def test_graph_failure_reuses_prepared_output_without_new_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={CHAT_A: [envelope(CHAT_A)]})
    _start(monkeypatch, extraction)
    state = FakeState()
    pipeline = FakePipeline()
    client = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,))],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,))]},
    )
    connector = _connector(client, state, extraction)
    writer = _writer(state, pipeline)

    commit_checkpoint = drive_until(
        connector,
        writer,
        checkpoint=_checkpoint(),
        subphase="commit",
    )
    assert extraction.calls == [CHAT_A]
    pipeline.raise_on_ids.add(str(envelope(CHAT_A)["source_record_id"]))

    with pytest.raises(RuntimeError, match="simulated graph write failure"):
        apply_one(connector, writer, commit_checkpoint)

    # The graph transaction rolled back, so the same prepared output is reused.
    applied, _ = apply_one(connector, writer, commit_checkpoint)
    assert applied.dispositions == ("committed",)
    assert extraction.calls == [CHAT_A]
    assert pipeline.envelopes == [pipeline.envelopes[0]]


def test_zero_person_extraction_records_an_explicit_processed_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={CHAT_A: []})
    _start(monkeypatch, extraction)
    state = FakeState()
    pipeline = FakePipeline()
    client = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,))],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,))]},
    )

    walked = drive(
        _connector(client, state, extraction),
        _writer(state, pipeline),
        checkpoint=_checkpoint(),
    )

    assert walked.finished is True
    assert extraction.calls == [CHAT_A]
    assert pipeline.envelopes == []
    assert state.committed_version(ENTITY, SESSION_ID, CHAT_A) is not None
    prepared = _staged_prepared(state)
    assert prepared.envelopes == []
    assert prepared.outcome == "extracted"


def test_absent_chat_is_not_treated_as_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={CHAT_A: [envelope(CHAT_A)]})
    _start(monkeypatch, extraction)
    state = FakeState()
    pipeline = FakePipeline()
    client = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,))],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,))]},
    )
    connector = _connector(client, state, extraction)
    walker = _writer(state, pipeline)
    drive(connector, walker, checkpoint=_checkpoint())

    # A later page omits CHAT_B entirely; absence must not create state for it.
    absent = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,))],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,))]},
    )
    drive(_connector(absent, state, extraction), walker, checkpoint=_checkpoint())

    assert state.committed_version(ENTITY, SESSION_ID, CHAT_B) is None
    assert all(CHAT_B not in str(item) for item in state.writes)


def test_page_snapshot_drift_and_out_of_window_snapshots_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={CHAT_A: [envelope(CHAT_A)]})
    _start(monkeypatch, extraction)
    state = FakeState()

    drifted = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,))],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,), snapshot_at="2026-09-17T05:45:00Z")]},
    )
    with pytest.raises(RuntimeError, match="snapshotAt changed"):
        drive(
            _connector(drifted, state, extraction),
            _writer(state, FakePipeline()),
            checkpoint=_checkpoint(),
        )

    beyond = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,), snapshot_at="2026-09-17T07:00:00+00:00")],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,))]},
    )
    with pytest.raises(RuntimeError, match="beyond the requested-as-of upper bound"):
        drive(
            _connector(beyond, FakeState(), extraction),
            _writer(state, FakePipeline()),
            checkpoint=_checkpoint(),
        )


def test_session_organization_mismatch_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={CHAT_A: [envelope(CHAT_A)]})
    _start(monkeypatch, extraction)
    client = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,), org_name="SpeedZone")],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,))]},
    )

    with pytest.raises(RuntimeError, match="organization does not match credential entity"):
        drive(
            _connector(client, FakeState(), extraction),
            _writer(FakeState(), FakePipeline()),
            checkpoint=_checkpoint(),
        )


def test_bounded_connector_refuses_an_unbound_or_wrong_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={})
    _start(monkeypatch, extraction)
    state = FakeState()
    checkpoint = _checkpoint()
    other = WhatsAdminBoundedConnector(
        entity_key=ENTITY,
        client=FakeBoundedClient(),
        credential="hk_eko_other_secret",
        state=state,  # type: ignore[arg-type]
        window=_window(),
        legacy_entity=None,
        max_records_per_unit=50,
        max_bytes_per_unit=2_000_000,
        max_extraction_calls_per_unit=40,
        clock=_clock,
    )

    assert other.validate_checkpoint(checkpoint) == "rejected"

    corrupted = CheckpointDescriptor(
        phase=checkpoint.phase,
        cursor={"cursor_version": 1},
        source_window=checkpoint.source_window,
        last_committed_record_id=None,
        connector_version=checkpoint.connector_version,
        schema_version=checkpoint.schema_version,
        replay_boundary=checkpoint.replay_boundary,
    )
    assert other.validate_checkpoint(corrupted) == "corrupted"


def test_stored_chat_page_round_trips_bundle_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={CHAT_A: [envelope(CHAT_A)]})
    _start(monkeypatch, extraction)
    state = FakeState()
    client = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,))],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,))]},
    )
    checkpoint = drive_until(
        _connector(client, state, extraction),
        _writer(state, FakePipeline()),
        checkpoint=_checkpoint(),
        subphase="extract",
    )

    cursor = WhatsAdminCursor.from_payload(checkpoint.cursor)
    assert cursor.bundles_digest is not None
    page = state.read_bundles(ENTITY, SESSION_ID, cursor.bundles_digest)
    assert page is not None
    stored = page.bundles[0]
    assert stored.source_version.startswith("sha256:")
    bundle = bundle_from_stored(stored)
    assert bundle.chat_id == CHAT_A
    assert bundle.msg_text == "Hello"
    assert bundle.session_id == SESSION_ID
    assert bundle.source_version == stored.source_version


def test_staged_prepared_payload_is_a_valid_durable_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={CHAT_A: [envelope(CHAT_A)]})
    _start(monkeypatch, extraction)
    state = FakeState()
    client = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,))],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,))]},
    )
    checkpoint = drive_until(
        _connector(client, state, extraction),
        _writer(state, FakePipeline()),
        checkpoint=_checkpoint(),
        subphase="commit",
    )

    prepared = _staged_prepared(state)
    assert prepared.chat_id == CHAT_A
    assert [item["source_record_id"] for item in prepared.envelopes] == [
        envelope(CHAT_A)["source_record_id"]
    ]
    assert WhatsAdminCursor.from_payload(checkpoint.cursor).prepared_digest is not None


def test_connector_close_releases_client_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={})
    _start(monkeypatch, extraction)
    state = FakeState()
    client = FakeBoundedClient()
    connector = _connector(client, state, extraction)

    connector.close()

    assert client.closed is True
    assert state.closed is True


def test_initial_checkpoint_copies_the_immutable_window() -> None:
    checkpoint = _checkpoint()

    assert checkpoint.phase == "whatsadmin"
    assert checkpoint.source_window == source_window()
    assert cursor_subphase(checkpoint) == "sessions"
    cursor = WhatsAdminCursor.from_payload(checkpoint.cursor)
    assert cursor.window_id == source_window()["window_id"]
    assert cursor.lower_bound == source_window()["completed_lower_bound"]
    assert cursor.upper_bound == UPPER_BOUND


def test_whatsadmin_state_requires_generation_scoped_store() -> None:
    class _Store:
        reset_generation = None

    with pytest.raises(ValueError, match="generation-scoped"):
        WhatsAdminBoundedState(_Store())  # type: ignore[arg-type]


def test_committed_version_key_is_entity_and_session_scoped() -> None:
    key = committed_version_key(ENTITY, SESSION_ID, CHAT_A)

    assert key.endswith(f"{ENTITY}:{SESSION_ID}:version:{CHAT_A}")
    assert "hk_" not in key


def test_empty_chat_page_completes_the_session_and_publishes_the_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={})
    _start(monkeypatch, extraction)
    state = FakeState()
    pipeline = FakePipeline()
    client = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,))],
        chat_pages={SESSION_ID: [chat_page(())]},
    )

    walked = drive(
        _connector(client, state, extraction),
        _writer(state, pipeline),
        checkpoint=_checkpoint(),
    )

    assert walked.finished is True
    assert walked.subphases() == ["sessions", "chats"]
    assert cursor_subphase(walked.checkpoint) == "terminal"
    assert extraction.calls == []
    assert state.entries[session_watermark_key(ENTITY, SESSION_ID)] == UPPER_BOUND


def test_no_visible_sessions_finishes_without_publishing_watermarks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={})
    _start(monkeypatch, extraction)
    state = FakeState()
    pipeline = FakePipeline()
    client = FakeBoundedClient(session_pages=[session_page(())], chat_pages={})

    walked = drive(
        _connector(client, state, extraction),
        _writer(state, pipeline),
        checkpoint=_checkpoint(),
    )

    assert walked.finished is True
    assert walked.subphases() == ["sessions"]
    assert extraction.calls == []
    assert all(":watermark" not in name for name in state.entries)


def test_sessions_page_that_only_declares_more_keeps_the_sessions_subphase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={CHAT_A: [envelope(CHAT_A)]})
    _start(monkeypatch, extraction)
    state = FakeState()
    pipeline = FakePipeline()
    client = FakeBoundedClient(
        session_pages=[
            session_page((), has_more=True, next_cursor="page-2"),
            session_page((SESSION_ID,)),
        ],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,))]},
    )

    walked = drive(
        _connector(client, state, extraction),
        _writer(state, pipeline),
        checkpoint=_checkpoint(),
    )

    assert walked.finished is True
    assert walked.subphases() == ["sessions", "sessions", "chats", "extract", "commit"]
    assert client.calls[1].cursor == "page-2"


def test_oversized_durable_payload_is_refused_instead_of_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={CHAT_A: [envelope(CHAT_A)]})
    _start(monkeypatch, extraction)
    state = FakeState()
    pipeline = FakePipeline()
    client = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,))],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,))]},
    )

    with pytest.raises(RuntimeError, match="exceeds its byte bound"):
        drive(
            _connector(client, state, extraction, max_bytes_per_unit=64),
            _writer(state, pipeline),
            checkpoint=_checkpoint(),
        )


def test_page_units_report_the_response_bytes_the_runner_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = RecordingBundleExtraction(envelopes={CHAT_A: [envelope(CHAT_A)]})
    _start(monkeypatch, extraction)
    state = FakeState()
    pipeline = FakePipeline()
    client = FakeBoundedClient(
        session_pages=[session_page((SESSION_ID,))],
        chat_pages={SESSION_ID: [chat_page((CHAT_A,))]},
    )

    walked = drive(
        _connector(client, state, extraction),
        _writer(state, pipeline),
        checkpoint=_checkpoint(),
        max_units=2,
    )

    page_steps = [step for step in walked.steps if step.subphase in {"sessions", "chats"}]
    assert len(page_steps) == 2
    assert all(step.usage.source_requests == 1 for step in page_steps)
    assert all(step.usage.pages == 1 for step in page_steps)
    assert all(step.usage.bytes_read > 0 for step in page_steps)


def _staged_prepared(state: FakeState) -> PreparedChat:
    payloads = [value for name, value in state.entries.items() if ":prepared:" in name]
    assert len(payloads) == 1
    return PreparedChat.model_validate_json(payloads[0])
