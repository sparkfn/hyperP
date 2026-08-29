"""Mypy-checked protocol compatibility contracts for disabled platform seams."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass

from sqlalchemy.orm import Session

from deal_intelligence.platform.database import Database
from deal_intelligence.platform.extensions import (
    ComponentPlugin,
    ComponentRegistry,
    ComponentRegistryProvider,
)
from deal_intelligence.platform.protocols import (
    PlatformStore,
    TransactionalParticipant,
    TransactionBoundary,
)
from deal_intelligence.platform.store import SqlAlchemyPlatformStore
from deal_intelligence.platform.types import ComponentDescriptor


@dataclass(frozen=True, slots=True)
class _ComponentPluginFake:
    descriptor: ComponentDescriptor = ComponentDescriptor("fake", "fake")


@dataclass(frozen=True, slots=True)
class _ComponentRegistryProviderFake:
    value: ComponentRegistry = ComponentRegistry()

    def registry(self) -> ComponentRegistry:
        return self.value


class _TransactionalParticipantFake:
    def prepare(self, session: Session) -> None:
        del session

    def commit(self, session: Session) -> None:
        del session

    def rollback(self, session: Session) -> None:
        del session


def protocol_contracts(
    database: Database, store: SqlAlchemyPlatformStore
) -> tuple[
    AbstractContextManager[Session],
    PlatformStore,
    ComponentPlugin,
    ComponentRegistryProvider,
    TransactionalParticipant,
]:
    """Force strict static checking of production classes against public protocols."""
    boundary: TransactionBoundary = database
    platform_store: PlatformStore = store
    plugin: ComponentPlugin = _ComponentPluginFake()
    provider: ComponentRegistryProvider = _ComponentRegistryProviderFake()
    participant: TransactionalParticipant = _TransactionalParticipantFake()
    return boundary.transaction(), platform_store, plugin, provider, participant
