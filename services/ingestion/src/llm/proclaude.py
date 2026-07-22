"""ProClaude OpenAI-compatible JSON-mode client configured from PROCLAUDE_* env."""

from __future__ import annotations

import os

import httpx

from src.ingestion_config import LlmConfig
from src.llm.openai import OpenAIService


class ProclaudeService(OpenAIService):
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

    def _normalize_base(self, base_url: str | None) -> str:
        stripped = (base_url or "").rstrip("/")
        if stripped.endswith("/openai/v1"):
            return stripped
        if stripped.endswith("/openai"):
            return f"{stripped}/v1"
        return f"{stripped}/openai/v1"

    def _extra_headers(self, payload: dict[str, object]) -> dict[str, str]:
        model = payload.get("model")
        return {"X-Model-Alias": model} if isinstance(model, str) else {}

    async def validate_readiness(self) -> None:
        """Fail clearly when the configured ProClaude model is unavailable."""
        if not self._headers:
            raise RuntimeError("PROCLAUDE_API_KEY is required for ingestion LLM calls")
        async with httpx.AsyncClient(
            base_url=self._base,
            timeout=self._config.timeout_seconds,
        ) as client:
            response = await client.get("/models", headers=self._headers)
        if response.status_code >= 400:
            raise RuntimeError(f"ProClaude model readiness failed with HTTP {response.status_code}")
        body = response.json()
        raw_models = body.get("data") if isinstance(body, dict) else None
        model_ids = (
            {
                item.get("id")
                for item in raw_models
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            if isinstance(raw_models, list)
            else set()
        )
        if self.default_model not in model_ids:
            raise RuntimeError(
                f"ProClaude model {self.default_model!r} is not available to this credential"
            )
