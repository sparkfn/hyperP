"""LLM service selection for ingestion workloads."""

from __future__ import annotations

import asyncio
import logging

from src.ingestion_config import get_ingestion_config
from src.llm.anthropic import AnthropicService
from src.llm.base import ChatMessage, LLMService
from src.llm.gpt import GPTService
from src.llm.openai import OpenAIService
from src.llm.proclaude import ProclaudeService

logger = logging.getLogger(__name__)

__all__ = [
    "AnthropicService",
    "ChatMessage",
    "GPTService",
    "LLMService",
    "OpenAIService",
    "ProclaudeService",
    "get_chat_extraction_service",
    "get_chat_summary_service",
    "get_llm_service",
    "validate_ingestion_llm_readiness",
]

_proclaude_service: ProclaudeService | None = None
_gpt_service: GPTService | None = None


def _get_proclaude_service() -> ProclaudeService:
    """Return the shared ProClaude OpenAI/JSON-mode service."""
    global _proclaude_service
    if _proclaude_service is None:
        _proclaude_service = ProclaudeService(llm_config=get_ingestion_config().llm)
    return _proclaude_service


def _get_gpt_service() -> GPTService:
    """Return the shared generic OpenAI-compatible ingestion service."""
    global _gpt_service
    if _gpt_service is None:
        _gpt_service = GPTService(llm_config=get_ingestion_config().llm)
        logger.info(
            "GPT connector backend=GPT model=%s",
            _gpt_service.default_model,
        )
    return _gpt_service


def get_llm_service() -> GPTService:
    """Return the GPT service for non-chat ingestion workloads."""
    return _get_gpt_service()


def get_chat_extraction_service() -> ProclaudeService:
    """Return the ProClaude JSON-mode structured chat extraction service."""
    return _get_proclaude_service()


def get_chat_summary_service() -> ProclaudeService:
    """Return the ProClaude chat-summary service."""
    return _get_proclaude_service()


def validate_ingestion_llm_readiness() -> None:
    """Validate the ProClaude ingestion LLM backend before long chat discovery."""
    asyncio.run(_get_proclaude_service().validate_readiness())
