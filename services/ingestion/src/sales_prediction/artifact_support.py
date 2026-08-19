"""Restricted artifact store construction for sales-prediction artifacts.

Reuses the stage-history restricted artifact primitives verbatim (no copied
filesystem code); only the HMAC domain and settings block differ, so a
sales-prediction manifest can never be authenticated as a Bitrix stage-history
manifest and vice versa.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from src.connectors.bitrix_stage_history.artifact_runtime import (
    ArtifactStoreConfiguration,
    decode_signing_secret,
)
from src.connectors.bitrix_stage_history.artifact_store import LocalRestrictedArtifactStore
from src.sales_prediction.contracts import SALES_ARTIFACT_HMAC_DOMAIN

if TYPE_CHECKING:
    from src.config import Settings


def sales_prediction_store_from_settings(
    settings: Settings,
) -> LocalRestrictedArtifactStore:
    """Open the restricted sales-prediction store without exposing its secret."""
    secret = decode_signing_secret(
        settings.sales_prediction_artifact_signing_key_secret.get_secret_value()
    )
    return ArtifactStoreConfiguration(
        primary_root=Path(settings.sales_prediction_artifact_primary_root),
        backup_root=Path(settings.sales_prediction_artifact_backup_root),
        signing_key_id=settings.sales_prediction_artifact_signing_key_id,
        signing_key_secret=secret,
        hmac_domain=SALES_ARTIFACT_HMAC_DOMAIN,
    ).open()
