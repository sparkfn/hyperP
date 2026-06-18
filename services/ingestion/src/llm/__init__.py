"""LLM service hierarchy for the ingestion worker.

The public surface is re-exported here so callers keep using
``from src.llm import …``. Two backends are wired by purpose:

- **GPTService** (OpenAI-compatible, real JSON mode → reliable, no truncation):
  ``get_address_llm_service`` (address normalization) and
  ``get_chat_extraction_service`` (structured chat data extraction).
- **ProclaudeService** (Anthropic /v1/messages, strong prose):
  ``get_chat_summary_service`` (narrative chat summaries) and the generic
  ``get_llm_service``.
"""

from __future__ import annotations

from src.ingestion_config import get_ingestion_config
from src.llm.anthropic import AnthropicService
from src.llm.base import ChatMessage, LLMService
from src.llm.gpt import GPTService
from src.llm.openai import OpenAIService
from src.llm.proclaude import ProclaudeService

__all__ = [
    "AnthropicService",
    "ChatMessage",
    "GPTService",
    "LLMService",
    "OpenAIService",
    "ProclaudeService",
    "get_address_llm_service",
    "get_chat_extraction_service",
    "get_chat_summary_service",
    "get_llm_service",
]

_gpt_service: LLMService | None = None
_proclaude_service: LLMService | None = None


def _get_gpt_service() -> LLMService:
    """Shared GPTService singleton (OpenAI JSON mode, low latency)."""
    global _gpt_service
    if _gpt_service is None:
        _gpt_service = GPTService(llm_config=get_ingestion_config().llm)
    return _gpt_service


def _get_proclaude_service() -> LLMService:
    """Shared ProclaudeService singleton (Anthropic /v1/messages)."""
    global _proclaude_service
    if _proclaude_service is None:
        _proclaude_service = ProclaudeService(llm_config=get_ingestion_config().llm)
    return _proclaude_service


def get_llm_service() -> LLMService:
    """Return the generic ProclaudeService."""
    return _get_proclaude_service()


def get_address_llm_service() -> LLMService:
    """Return the address-normalization service (GPTService).

    Address normalization runs once per record on the identity/sales sources.
    GPT's real JSON mode gives reliable, untruncated output at low latency.
    """
    return _get_gpt_service()


def get_chat_extraction_service() -> LLMService:
    """Return the structured chat-data extraction service (GPTService).

    Identity/transaction extraction needs reliable JSON; GPT's JSON mode avoids
    the intermittent invalid/truncated JSON proclaude emits for large outputs.
    """
    return _get_gpt_service()


def get_chat_summary_service() -> LLMService:
    """Return the chat-summary service (ProclaudeService).

    The narrative conversation summary is prose, where proclaude is strong; a
    failed summary call only drops summaries, never the GPT-extracted identity
    data.
    """
    return _get_proclaude_service()
