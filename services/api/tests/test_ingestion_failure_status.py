from src.graph.queries.admin import LIST_SOURCE_SYSTEMS


def test_source_system_status_keeps_latest_run_and_latest_failure_separate() -> None:
    assert "head(collect(run)) AS latest_run" in LIST_SOURCE_SYSTEMS
    assert "head(collect(failure)) AS latest_failure" in LIST_SOURCE_SYSTEMS
    assert "latest_failure {" in LIST_SOURCE_SYSTEMS
    assert ".failure_mode" in LIST_SOURCE_SYSTEMS
