"""ProClaude-backed LLM service hierarchy for the ingestion worker."""

from __future__ import annotations

import asyncio

from src.ingestion_config import get_ingestion_config
from src.llm.anthropic import AnthropicService
from src.llm.base import ChatMessage, LLMService
from src.llm.openai import OpenAIService
from src.llm.proclaude import ProclaudeService

__all__ = [
    "AnthropicService",
    "ChatMessage",
    "LLMService",
    "OpenAIService",
    "ProclaudeService",
    "get_address_llm_service",
    "get_chat_extraction_service",
    "get_chat_summary_service",
    "get_llm_service",
    "validate_ingestion_llm_readiness",
]

_proclaude_service: ProclaudeService | None = None


def _get_proclaude_service() -> ProclaudeService:
    """Return the shared ProClaude OpenAI/JSON-mode service."""
    global _proclaude_service
    if _proclaude_service is None:
        _proclaude_service = ProclaudeService(llm_config=get_ingestion_config().llm)
    return _proclaude_service


def get_llm_service() -> LLMService:
    """Return the generic ProclaudeService."""
    return _get_proclaude_service()


def get_address_llm_service() -> LLMService:
    """Return the ProClaude JSON-mode address-normalization service."""
    return _get_proclaude_service()


def get_chat_extraction_service() -> LLMService:
    """Return the ProClaude JSON-mode structured chat extraction service."""
    return _get_proclaude_service()


def get_chat_summary_service() -> LLMService:
    """Return the ProClaude chat-summary service."""
    return _get_proclaude_service()


def validate_ingestion_llm_readiness() -> None:
    """Validate ProClaude credentials and model access before long chat discovery."""
    asyncio.run(_get_proclaude_service().validate_readiness())
