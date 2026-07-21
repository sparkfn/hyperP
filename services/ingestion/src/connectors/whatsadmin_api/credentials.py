"""Tenant-scoped WhatsAdmin credential resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import SecretStr

from src.errors import SourceNotConfiguredError

WhatsAdminEntity = Literal["eko", "speedzone"]
WHATSADMIN_ENTITIES: tuple[WhatsAdminEntity, ...] = ("eko", "speedzone")


def is_valid_whatsadmin_handle_key(api_key: SecretStr) -> bool:
    """Return whether a secret has the non-empty WhatsAdmin handle-key shape."""
    value = api_key.get_secret_value()
    return value.startswith("hk_") and len(value) > len("hk_")


def whatsadmin_entity_keys_are_distinct(
    eko_api_key: SecretStr,
    speedzone_api_key: SecretStr,
) -> bool:
    """Return whether two configured handle keys cannot authenticate as each other."""
    if not (
        is_valid_whatsadmin_handle_key(eko_api_key)
        and is_valid_whatsadmin_handle_key(speedzone_api_key)
    ):
        return True
    return eko_api_key.get_secret_value() != speedzone_api_key.get_secret_value()


@dataclass(frozen=True)
class WhatsAdminCredential:
    """One entity's validated WhatsAdmin connection settings."""

    entity_key: WhatsAdminEntity
    base_url: str
    api_key: SecretStr


class WhatsAdminCredentialResolver:
    """Resolve credentials without cross-entity fallback."""

    def __init__(
        self,
        *,
        base_url: str,
        eko_api_key: SecretStr,
        speedzone_api_key: SecretStr,
    ) -> None:
        self._base_url = base_url.strip()
        self._eko_api_key = eko_api_key
        self._speedzone_api_key = speedzone_api_key

    def resolve(self, entity_key: str) -> WhatsAdminCredential:
        """Return only the requested entity's credential."""
        if entity_key not in WHATSADMIN_ENTITIES:
            raise ValueError(f"Unknown WhatsAdmin entity {entity_key!r}")
        entity: WhatsAdminEntity = entity_key
        if not self._base_url:
            raise SourceNotConfiguredError(
                f"WhatsAdmin base URL is not configured for entity {entity}"
            )
        api_key = self._api_key_for(entity)
        if not is_valid_whatsadmin_handle_key(api_key):
            raise SourceNotConfiguredError(
                f"WhatsAdmin API key is not configured for entity {entity}"
            )
        if not whatsadmin_entity_keys_are_distinct(
            self._eko_api_key,
            self._speedzone_api_key,
        ):
            raise SourceNotConfiguredError("WhatsAdmin Eko and Speedzone API keys must be distinct")
        return WhatsAdminCredential(entity, self._base_url, api_key)

    def resolve_job(self, entity_key: str | None) -> tuple[WhatsAdminCredential, ...]:
        """Resolve one entity, or both entities for a default extraction job."""
        requested_entities: tuple[str, ...] = (
            WHATSADMIN_ENTITIES if entity_key is None else (entity_key,)
        )
        return tuple(self.resolve(requested) for requested in requested_entities)

    def _api_key_for(self, entity_key: WhatsAdminEntity) -> SecretStr:
        if entity_key == "eko":
            return self._eko_api_key
        return self._speedzone_api_key
