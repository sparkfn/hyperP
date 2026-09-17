"""Trusted descriptor registration with adapter-local module discovery."""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType
from typing import cast

import src.connectors as connectors_package
from src.bounded_ingestion_models import BoundedConnectorDescriptor, BoundedMode

_DESCRIPTOR_MODULE_SUFFIX = ".bounded_descriptor"
_DESCRIPTOR_EXPORT = "DESCRIPTOR"
_DESCRIPTORS_EXPORT = "DESCRIPTORS"


class BoundedConnectorRegistry:
    def __init__(self, *, auto_discover: bool = False) -> None:
        self._items: dict[str, BoundedConnectorDescriptor] = {}
        self._auto_discover = auto_discover
        self._discovered = False

    def register(self, descriptor: BoundedConnectorDescriptor) -> None:
        source_key = descriptor.source_key.strip()
        if not source_key or source_key in self._items:
            raise ValueError("bounded descriptor source key is empty or duplicated")
        versions = (
            descriptor.connector_version,
            descriptor.configuration_version,
        )
        if not all(value.strip() for value in versions):
            raise ValueError("bounded descriptor versions must be non-empty")
        limits = (
            descriptor.checkpoint_schema_version,
            descriptor.max_records_per_unit,
            descriptor.max_source_requests_per_unit,
            descriptor.max_bytes_per_unit,
            descriptor.max_extraction_calls_per_unit,
        )
        if any(value < 1 for value in limits):
            raise ValueError("bounded descriptor limits must be positive")
        durations = (
            descriptor.max_close_seconds,
            descriptor.max_retry_backoff_seconds,
        )
        if any(value <= 0 for value in durations):
            raise ValueError("bounded descriptor durations must be positive")
        if not descriptor.supports_deadline or not descriptor.supports_cancellation:
            raise ValueError("bounded descriptor lacks deadline/cancellation support")
        if not any(
            (
                descriptor.supports_bootstrap,
                descriptor.supports_delta,
                descriptor.supports_one_time,
            )
        ):
            raise ValueError("bounded descriptor supports no execution mode")
        self._items[source_key] = descriptor

    def discover(self) -> None:
        """Import only adapter-local ``bounded_descriptor`` convention modules."""
        if self._discovered:
            return
        self._discovered = True
        prefix = f"{connectors_package.__name__}."
        for module_info in pkgutil.walk_packages(
            connectors_package.__path__,
            prefix=prefix,
        ):
            if module_info.name.endswith(_DESCRIPTOR_MODULE_SUFFIX):
                module = importlib.import_module(module_info.name)
                for descriptor in _descriptors_from_module(module):
                    self.register(descriptor)

    def require(
        self,
        source_key: str,
        mode: BoundedMode,
    ) -> BoundedConnectorDescriptor:
        self._maybe_discover()
        descriptor = self._items.get(source_key)
        if descriptor is None:
            raise LookupError("source has no bounded descriptor")
        supported = {
            "bootstrap": descriptor.supports_bootstrap,
            "delta": descriptor.supports_delta,
            "one_time": descriptor.supports_one_time,
        }
        if not supported[mode]:
            raise LookupError("bounded descriptor does not support requested mode")
        return descriptor

    def get(self, source_key: str) -> BoundedConnectorDescriptor | None:
        self._maybe_discover()
        return self._items.get(source_key)

    def registered_sources(self) -> tuple[str, ...]:
        self._maybe_discover()
        return tuple(sorted(self._items))

    def _maybe_discover(self) -> None:
        if self._auto_discover:
            self.discover()


def _descriptors_from_module(module: ModuleType) -> tuple[BoundedConnectorDescriptor, ...]:
    single: object = getattr(module, _DESCRIPTOR_EXPORT, None)
    multiple: object = getattr(module, _DESCRIPTORS_EXPORT, None)
    if single is not None and multiple is not None:
        raise ValueError(f"{module.__name__} cannot export both descriptor conventions")
    if single is not None:
        return (cast(BoundedConnectorDescriptor, single),)
    if isinstance(multiple, tuple) and multiple:
        return tuple(cast(BoundedConnectorDescriptor, item) for item in multiple)
    raise ValueError(f"{module.__name__} must export {_DESCRIPTOR_EXPORT} or {_DESCRIPTORS_EXPORT}")


registry = BoundedConnectorRegistry(auto_discover=True)
