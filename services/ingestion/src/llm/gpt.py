"""GPT OpenAI-compatible JSON-mode client configured from GPT_* env."""

from __future__ import annotations

import os

from src.ingestion_config import LlmConfig
from src.llm.openai import OpenAIService


class GPTService(OpenAIService):
    """Dedicated GPT connector used by address normalization."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        llm_config: LlmConfig | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url or os.environ.get("GPT_API_BASE_URL", "https://gpt.ada.asia/api/v1"),
            api_key=api_key or os.environ.get("GPT_API_KEY", ""),
            default_model=default_model or os.environ.get("GPT_DEFAULT_MODEL") or "gpt-4o",
            llm_config=llm_config,
        )

    async def validate_readiness(self) -> None:
        """Fail clearly when GPT credentials or the configured model are unavailable."""
        await self._validate_model_readiness("GPT", "GPT_API_KEY")
