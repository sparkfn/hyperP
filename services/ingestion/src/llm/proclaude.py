"""ProclaudeService — concrete Anthropic client configured from PROCLAUDE_* env."""

from __future__ import annotations

import os

from src.ingestion_config import LlmConfig
from src.llm.anthropic import AnthropicService


class ProclaudeService(AnthropicService):
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        llm_config: LlmConfig | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url
            or os.environ.get("PROCLAUDE_API_BASE_URL", "https://proclaude.sparkfn.io"),
            api_key=api_key or os.environ.get("PROCLAUDE_API_KEY", ""),
            default_model=default_model
            or os.environ.get("PROCLAUDE_DEFAULT_MODEL")
            or "claude-sonnet-4",
            llm_config=llm_config,
        )
