from __future__ import annotations

import argparse
import json
from typing import Protocol

from src.config import get_settings
from src.graph.client import Neo4jClient
from src.graph.standalone_crm_census import (
    StandaloneCrmCensusAdmission,
    StandaloneCrmCensusRepository,
    StandaloneCrmCensusStatus,
)
from src.ingestion_config import BitrixOpenLinesConfig, get_ingestion_config
from src.standalone_crm_census_authority import (
    StandaloneCrmCensusAuthority,
    UnavailableStandaloneCrmCensusAuthority,
)
from src.standalone_crm_census_http import StandaloneCrmCensusBitrixProbeFactory
from src.standalone_crm_census_models import StandaloneCrmCensusRequest, parse_census_request
from src.standalone_crm_census_runtime import (
    StandaloneCrmCensusRuntime,
    StandaloneCrmChildPublisher,
    StandaloneCrmRuntimeResult,
)


class StandaloneCrmCensusControl(Protocol):
    def start(self, request: StandaloneCrmCensusRequest) -> StandaloneCrmCensusAdmission: ...

    def status(self, census_id: str) -> StandaloneCrmCensusStatus | None: ...

    def cancel(self, census_id: str, actor: str, reason: str) -> bool: ...

    def resume(self, census_id: str) -> StandaloneCrmRuntimeResult: ...


class StandaloneCrmCensusService:
    def __init__(
        self,
        repository: StandaloneCrmCensusRepository,
        authority: StandaloneCrmCensusAuthority | None = None,
        config: BitrixOpenLinesConfig | None = None,
        publisher: StandaloneCrmChildPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._authority = authority or UnavailableStandaloneCrmCensusAuthority()
        self._config = config or get_ingestion_config().bitrix_openlines
        self._publisher = publisher

    def start(self, request: StandaloneCrmCensusRequest) -> StandaloneCrmCensusAdmission:
        if not self._config.standalone_crm_identity_enabled:
            raise RuntimeError("standalone CRM identity is disabled")
        self._authority.verify(request)
        return self._repository.admit(request)

    def status(self, census_id: str) -> StandaloneCrmCensusStatus | None:
        return self._repository.status(census_id)

    def cancel(self, census_id: str, actor: str, reason: str) -> bool:
        return self._repository.request_cancellation(census_id, actor, reason)

    def resume(self, census_id: str) -> StandaloneCrmRuntimeResult:
        return self._runtime().continue_after_pause(census_id)

    def reconcile(self, census_id: str) -> StandaloneCrmRuntimeResult:
        return self._runtime().reconcile(census_id)

    def run_parent(self, census_id: str) -> StandaloneCrmRuntimeResult:
        snapshot = self._repository.runtime_snapshot(census_id)
        if snapshot is None:
            return StandaloneCrmRuntimeResult(census_id, "missing", 0, "census not found")
        return self._runtime().run_parent(census_id, snapshot.request)

    def repair(self, census_id: str) -> StandaloneCrmRuntimeResult:
        return self._runtime().repair_publications(census_id)

    def classify(self, census_id: str) -> int:
        snapshot = self._repository.runtime_snapshot(census_id)
        if snapshot is None:
            return 0
        if not self._config.standalone_crm_identity_enabled:
            raise RuntimeError("standalone CRM identity is disabled")
        self._authority.verify(snapshot.request)
        self._repository.require_active_source(snapshot.request)
        return self._repository.classify_unresolved_calls(census_id)

    def _runtime(self) -> StandaloneCrmCensusRuntime:
        return StandaloneCrmCensusRuntime(
            self._repository,
            self._authority,
            self._config,
            probe_factory=StandaloneCrmCensusBitrixProbeFactory(get_settings()),
            publisher=self._publisher,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Operate standalone CRM census control without source I/O."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("request_json")
    for name in ("status", "reconcile", "repair", "classify"):
        command = commands.add_parser(name)
        command.add_argument("census_id")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("census_id")
    cancel.add_argument("actor")
    cancel.add_argument("reason")
    resume = commands.add_parser("resume")
    resume.add_argument("census_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = Neo4jClient(get_settings())
    service = StandaloneCrmCensusService(StandaloneCrmCensusRepository(client))
    try:
        if args.command == "start":
            raw = json.loads(args.request_json)
            if not isinstance(raw, dict):
                raise ValueError("request_json must be an object")
            print(service.start(parse_census_request(raw)).census_id)
        elif args.command == "cancel":
            print(service.cancel(args.census_id, args.actor, args.reason))
        elif args.command == "resume":
            print(service.resume(args.census_id).state)
        elif args.command == "reconcile":
            print(service.reconcile(args.census_id).state)
        elif args.command == "repair":
            print(service.repair(args.census_id).state)
        elif args.command == "classify":
            print(service.classify(args.census_id))
        else:
            status = service.status(args.census_id)
            print("missing" if status is None else status.state)
        return 0
    finally:
        client.close()
