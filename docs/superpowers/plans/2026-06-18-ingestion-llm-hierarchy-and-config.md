# Ingestion LLM Hierarchy + Consolidated Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the ingestion service's LLM client into an abstract `LLMService` hierarchy (`OpenAIService`/`AnthropicService` → `GPTService`/`ProclaudeService`), route ingestion LLM calls through `ProclaudeService` by default, and consolidate exclusions + LLM tuning into one JSON config file.

**Architecture:** `src/llm.py` becomes a `src/llm/` package: an abstract base owns the unified `chat_json` entrypoint, retry loop, and `httpx.AsyncClient` lifecycle; protocol subclasses implement wire-format hooks. A new `src/ingestion_config.py` loads `{ "exclusions": {…}, "llm": {…} }` from one file; LLM tuning leaves `Settings`/env entirely. Connectors switch from `load_exclusion_file(settings.ingestion_exclusions_file)` to `get_ingestion_config().exclusions`.

**Tech Stack:** Python 3.12, pydantic v2, httpx (async), pytest + pytest-asyncio, uv workspace, mypy --strict, ruff.

## Global Constraints

- Service scope: `services/ingestion/` only. Do **not** touch `services/api/src/llm/` or `services/api/src/proclaude/`.
- Strict typing: every function has a return type; no `Any`, no untyped `dict`/`list`; passes `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src`.
- Lint/format changed files only: `uv run --package profile-unifier-ingestion ruff check services/ingestion/src` and `ruff format`.
- Cypher/SQL/query rules: N/A here.
- Existing public imports MUST keep working: `from src.llm import ChatMessage, get_llm_service` and `from src.llm import LLMService` (re-exported from the new package `__init__`).
- The default ingestion backend returned by `get_llm_service()` is `ProclaudeService`.
- Env var names: OpenAI-compatible client uses `GPT_API_BASE_URL` / `GPT_API_KEY` / `GPT_DEFAULT_MODEL`; Anthropic client uses `PROCLAUDE_API_BASE_URL` / `PROCLAUDE_API_KEY` / `PROCLAUDE_DEFAULT_MODEL`.
- Consolidated config env var: `INGESTION_CONFIG_FILE` (replaces `INGESTION_EXCLUSIONS_FILE`); default `/app/config/ingestion-config.example.json`.
- `docker-compose.yml` changes MUST be mirrored in `.docker/staging/docker-compose.yml` in the same commit (differ only in build context/`name:`/volume paths).
- Run ingestion tests with `uv run pytest services/ingestion/tests`.
- **Never commit without explicit user confirmation** — the `git commit` step in each task is a checkpoint reminder; pause for the user.

---

### Task 1: Consolidated config module (`ingestion_config.py`)

Adds the `{ "exclusions": {…}, "llm": {…} }` loader and an `IngestionConfig`/`LlmConfig` model, plus a `get_ingestion_config()` accessor and the new `Settings.ingestion_config_file` field. Purely additive — the old `ingestion_exclusions_file` field and `load_exclusion_file` stay until Task 5.

**Files:**
- Create: `services/ingestion/src/ingestion_config.py`
- Modify: `services/ingestion/src/config.py` (add `ingestion_config_file` field — line 131 area)
- Test: `services/ingestion/tests/test_ingestion_config.py`

**Interfaces:**
- Consumes: `ExclusionFile`, `load_exclusion_file`'s parsers (`_str_list`, `_machine_unit_identifier_list`) from `src.exclusion_config`; `get_settings` from `src.config`; `JsonValue` from `src.models`.
- Produces:
  - `@dataclass class LlmConfig` with fields `timeout_seconds: float = 60.0`, `request_delay_seconds: float = 0.5`, `max_retries: int = 6`, `retry_base_delay_seconds: float = 1.0`, `retry_max_delay_seconds: float = 30.0`.
  - `@dataclass class IngestionConfig` with `exclusions: ExclusionFile`, `llm: LlmConfig`.
  - `def load_ingestion_config(path_value: str) -> IngestionConfig`
  - `def get_ingestion_config() -> IngestionConfig` (reads `get_settings().ingestion_config_file`)

- [ ] **Step 1: Write the failing tests**

Create `services/ingestion/tests/test_ingestion_config.py`:

```python
"""Tests for the consolidated ingestion config loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.exclusion_config import ExclusionFile
from src.ingestion_config import (
    IngestionConfig,
    LlmConfig,
    load_ingestion_config,
)


def test_nested_format_parses_exclusions_and_llm(tmp_path: Path) -> None:
    path = tmp_path / "ingestion-config.json"
    path.write_text(
        json.dumps(
            {
                "exclusions": {
                    "phones": ["+6511111111"],
                    "emails": ["a@b.com"],
                    "email_domains": ["b.com"],
                    "names": ["Acme"],
                    "source_ids": ["s1"],
                    "machine_unit_identifiers": [{"machine_product": "Forklift X"}],
                },
                "llm": {
                    "timeout_seconds": 12.0,
                    "request_delay_seconds": 0.25,
                    "max_retries": 3,
                    "retry_base_delay_seconds": 0.5,
                    "retry_max_delay_seconds": 10.0,
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_ingestion_config(str(path))
    assert config.exclusions.phones == ["+6511111111"]
    assert config.exclusions.machine_unit_identifiers == [{"machine_product": "Forklift X"}]
    assert config.llm == LlmConfig(
        timeout_seconds=12.0,
        request_delay_seconds=0.25,
        max_retries=3,
        retry_base_delay_seconds=0.5,
        retry_max_delay_seconds=10.0,
    )


def test_bare_exclusions_format_is_backward_compatible(tmp_path: Path) -> None:
    # Old format: top-level exclusion keys, no "exclusions"/"llm" wrapper.
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"phones": ["+6522222222"], "emails": []}), encoding="utf-8")
    config = load_ingestion_config(str(path))
    assert config.exclusions.phones == ["+6522222222"]
    assert config.llm == LlmConfig()  # defaults


def test_blank_path_returns_defaults() -> None:
    config = load_ingestion_config("")
    assert config == IngestionConfig(exclusions=ExclusionFile(), llm=LlmConfig())


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_ingestion_config(str(tmp_path / "nope.json"))


def test_invalid_llm_block_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"exclusions": {}, "llm": {"max_retries": "lots"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid ingestion config JSON"):
        load_ingestion_config(str(path))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest services/ingestion/tests/test_ingestion_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.ingestion_config'`.

- [ ] **Step 3: Write the config module**

Create `services/ingestion/src/ingestion_config.py`:

```python
"""Consolidated JSON config for ingestion: hard exclusions + LLM call tuning."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from src.config import get_settings
from src.exclusion_config import (
    ExclusionFile,
    _machine_unit_identifier_list,
    _str_list,
)
from src.models import JsonValue


@dataclass
class LlmConfig:
    """LLM call tuning loaded from the consolidated config file."""

    timeout_seconds: float = 60.0
    request_delay_seconds: float = 0.5
    max_retries: int = 6
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 30.0


@dataclass
class IngestionConfig:
    """The whole ingestion config file: exclusions + LLM tuning."""

    exclusions: ExclusionFile = field(default_factory=ExclusionFile)
    llm: LlmConfig = field(default_factory=LlmConfig)


def _exclusion_file(raw: JsonValue, *, path: Path) -> ExclusionFile:
    if raw is None:
        return ExclusionFile()
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    payload = cast(dict[str, JsonValue], raw)
    return ExclusionFile(
        phones=_str_list(payload.get("phones"), path=path),
        emails=_str_list(payload.get("emails"), path=path),
        email_domains=_str_list(payload.get("email_domains"), path=path),
        names=_str_list(payload.get("names"), path=path),
        source_ids=_str_list(payload.get("source_ids"), path=path),
        machine_unit_identifiers=_machine_unit_identifier_list(
            payload.get("machine_unit_identifiers"), path=path
        ),
    )


def _float(raw: JsonValue, default: float, *, path: Path) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    return float(raw)


def _int(raw: JsonValue, default: int, *, path: Path) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    return raw


def _llm_config(raw: JsonValue, *, path: Path) -> LlmConfig:
    if raw is None:
        return LlmConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    payload = cast(dict[str, JsonValue], raw)
    defaults = LlmConfig()
    return LlmConfig(
        timeout_seconds=_float(payload.get("timeout_seconds"), defaults.timeout_seconds, path=path),
        request_delay_seconds=_float(
            payload.get("request_delay_seconds"), defaults.request_delay_seconds, path=path
        ),
        max_retries=_int(payload.get("max_retries"), defaults.max_retries, path=path),
        retry_base_delay_seconds=_float(
            payload.get("retry_base_delay_seconds"), defaults.retry_base_delay_seconds, path=path
        ),
        retry_max_delay_seconds=_float(
            payload.get("retry_max_delay_seconds"), defaults.retry_max_delay_seconds, path=path
        ),
    )


def load_ingestion_config(path_value: str) -> IngestionConfig:
    """Load the consolidated ingestion config, with backward-compat for the
    old bare-exclusions format (top-level exclusion keys, no wrapper)."""
    if not path_value.strip():
        return IngestionConfig()
    path = Path(path_value)
    if not path.exists():
        raise FileNotFoundError(f"Ingestion config file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid ingestion config JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    payload = cast(dict[str, JsonValue], raw)
    if "exclusions" not in payload and "llm" not in payload:
        # Old format: whole object is the exclusions block.
        return IngestionConfig(exclusions=_exclusion_file(payload, path=path), llm=LlmConfig())
    return IngestionConfig(
        exclusions=_exclusion_file(payload.get("exclusions"), path=path),
        llm=_llm_config(payload.get("llm"), path=path),
    )


def get_ingestion_config() -> IngestionConfig:
    """Load the ingestion config from the configured file path."""
    return load_ingestion_config(get_settings().ingestion_config_file)
```

- [ ] **Step 4: Add the new Settings field**

In `services/ingestion/src/config.py`, in the "Hard ingestion exclusions" block (around line 127-131), add `ingestion_config_file` alongside the existing `ingestion_exclusions_file` (keep both for now):

```python
    # Hard ingestion exclusions -------------------------------------------------
    company_mobile_numbers: list[str] = []
    company_email_addresses: list[str] = []
    internal_person_names: list[str] = []
    ingestion_exclusions_file: str = ""
    ingestion_config_file: str = ""
```

- [ ] **Step 5: Run tests + type-check to verify pass**

Run: `uv run pytest services/ingestion/tests/test_ingestion_config.py -v`
Expected: PASS (5 tests).
Run: `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/ingestion_config.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add services/ingestion/src/ingestion_config.py services/ingestion/src/config.py services/ingestion/tests/test_ingestion_config.py
git commit -m "feat(ingestion): add consolidated ingestion config loader (exclusions + llm)"
```

---

### Task 2: LLM service hierarchy (`src/llm/` package)

Replaces `src/llm.py` with a package: abstract `LLMService` base + `OpenAIService`/`AnthropicService` + concrete `GPTService`/`ProclaudeService`. `get_llm_service()` returns a `ProclaudeService`. Retry/timeout/delay come from an injected `LlmConfig` (no `Settings`/env reads for tuning).

**Files:**
- Delete: `services/ingestion/src/llm.py`
- Create: `services/ingestion/src/llm/__init__.py`, `base.py`, `openai.py`, `anthropic.py`, `gpt.py`, `proclaude.py`
- Modify: `services/ingestion/tests/test_llm_retry.py`
- Test: `services/ingestion/tests/test_llm_services.py`

**Interfaces:**
- Consumes: `LlmConfig`, `get_ingestion_config` from `src.ingestion_config` (Task 1).
- Produces (re-exported from `src.llm`):
  - `class ChatMessage(BaseModel)` — `role: Literal["system","user","assistant"]`, `content: str`.
  - `class LLMService(ABC)` — `async def chat_json(self, messages: list[ChatMessage], *, model: str | None = None, temperature: float = 0.0, max_tokens: int | None = None) -> str`; `@property default_model -> str`; `async def close() -> None`.
  - `class OpenAIService(LLMService)`, `class AnthropicService(LLMService)`.
  - `class GPTService(OpenAIService)`, `class ProclaudeService(AnthropicService)` — each `__init__(self, base_url: str | None = None, api_key: str | None = None, default_model: str | None = None, llm_config: LlmConfig | None = None)`.
  - `def get_llm_service() -> LLMService` (returns `ProclaudeService`), `async def close_llm_service() -> None`.

- [ ] **Step 1: Write the failing tests**

Create `services/ingestion/tests/test_llm_services.py`:

```python
"""Tests for the ingestion LLM service hierarchy."""

from __future__ import annotations

import httpx
import pytest
from src.ingestion_config import LlmConfig
from src.llm import (
    AnthropicService,
    ChatMessage,
    GPTService,
    LLMService,
    OpenAIService,
    ProclaudeService,
    get_llm_service,
)


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler: httpx.MockTransport) -> None:
    original = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        base = kwargs.get("base_url")
        return original(transport=handler, base_url=base if isinstance(base, str) else "")

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_class_hierarchy() -> None:
    assert issubclass(GPTService, OpenAIService)
    assert issubclass(ProclaudeService, AnthropicService)
    assert issubclass(OpenAIService, LLMService)
    assert issubclass(AnthropicService, LLMService)


def test_get_llm_service_returns_proclaude() -> None:
    assert isinstance(get_llm_service(), ProclaudeService)


@pytest.mark.asyncio
async def test_gpt_service_builds_openai_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured["url"] = str(request.url)
        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "created": 1,
                "model": "m",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "{\"ok\":1}"},
                     "finish_reason": "stop"}
                ],
            },
        )

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    svc = GPTService(base_url="https://gpt.test/api/v1", api_key="k", default_model="m")
    out = await svc.chat_json(
        [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hi")]
    )
    assert out == '{"ok":1}'
    assert str(captured["url"]).endswith("/v1/chat/completions")
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "m"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


@pytest.mark.asyncio
async def test_proclaude_service_splits_system_and_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured["url"] = str(request.url)
        captured["body"] = _json.loads(request.content)
        captured["alias"] = request.headers.get("X-Model-Alias")
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "{\"a\":"},
                    {"type": "text", "text": "1}"},
                ],
                "model": "claude-x",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        )

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    svc = ProclaudeService(base_url="https://proclaude.test", api_key="svc_k", default_model="claude-x")
    out = await svc.chat_json(
        [ChatMessage(role="system", content="be json"), ChatMessage(role="user", content="extract")]
    )
    assert out == '{"a":1}'
    assert str(captured["url"]).endswith("/v1/messages")
    assert captured["alias"] == "claude-x"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["system"] == "be json"
    assert body["messages"] == [{"role": "user", "content": "extract"}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest services/ingestion/tests/test_llm_services.py -v`
Expected: FAIL — `ImportError: cannot import name 'AnthropicService' from 'src.llm'`.

- [ ] **Step 3: Write `base.py`**

Create `services/ingestion/src/llm/base.py`:

```python
"""Abstract LLM service base: unified chat_json entrypoint + retry loop."""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict

from src.ingestion_config import LlmConfig


class ChatMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role: Literal["system", "user", "assistant"]
    content: str


class LLMService(ABC):
    """Backend-agnostic LLM client.

    Owns the unified ``chat_json`` entrypoint, the retry/backoff loop, and a
    reused ``httpx.AsyncClient``. Subclasses implement protocol-specific hooks.
    """

    _endpoint_path: str

    def __init__(
        self,
        base_url: str | None,
        api_key: str | None,
        default_model: str,
        llm_config: LlmConfig | None = None,
    ) -> None:
        self._base = self._normalize_base(base_url)
        key = api_key or ""
        self._headers: dict[str, str] = {"Authorization": f"Bearer {key}"} if key else {}
        self._default_model_value = default_model
        self._config = llm_config or LlmConfig()
        self._client: httpx.AsyncClient | None = None

    @property
    def default_model(self) -> str:
        return self._default_model_value

    def _normalize_base(self, base_url: str | None) -> str:
        return (base_url or "").rstrip("/")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base, timeout=self._config.timeout_seconds
            )
        return self._client

    async def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        """Return the assistant text for ``messages`` (JSON-by-prompting)."""
        client = await self._ensure_client()
        payload = self._build_payload(
            messages, model or self._default_model_value, temperature, max_tokens
        )
        headers = {**self._headers, **self._extra_headers(payload)}
        max_retries = max(self._config.max_retries, 0)
        for attempt in range(max_retries + 1):
            response = await client.post(self._endpoint_path, json=payload, headers=headers)
            if response.status_code < 400:
                return self._parse_text(response.json())
            if not self._is_retryable(response) or attempt == max_retries:
                _raise_http_status(response)
            await asyncio.sleep(self._retry_after(response, attempt))
        raise RuntimeError("unreachable LLM retry state")

    def _backoff_delay(self, attempt: int) -> float:
        capped = min(
            self._config.retry_base_delay_seconds * (2**attempt),
            self._config.retry_max_delay_seconds,
        )
        return random.uniform(capped * 0.5, capped)

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                pass
        return self._backoff_delay(attempt)

    # Protocol-specific hooks ------------------------------------------------

    @abstractmethod
    def _build_payload(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float,
        max_tokens: int | None,
    ) -> dict[str, object]: ...

    def _extra_headers(self, payload: dict[str, object]) -> dict[str, str]:
        return {}

    @abstractmethod
    def _parse_text(self, body: object) -> str: ...

    @abstractmethod
    def _is_retryable(self, response: httpx.Response) -> bool: ...


def _raise_http_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as err:
        body = response.text[:500]
        raise httpx.HTTPStatusError(
            f"{response.status_code} {response.reason_phrase}: {body}",
            request=response.request,
            response=response,
        ) from err
```

- [ ] **Step 4: Write `openai.py`**

Create `services/ingestion/src/llm/openai.py`:

```python
"""OpenAI-compatible (/v1/chat/completions) LLM service."""

from __future__ import annotations

import httpx

from src.llm.base import ChatMessage, LLMService


class OpenAIService(LLMService):
    """LLM client for servers speaking the OpenAI chat-completions protocol."""

    _endpoint_path = "/chat/completions"

    def _normalize_base(self, base_url: str | None) -> str:
        # Gateways expose the API under /api/v1; ensure exactly a /v1 suffix so
        # the endpoint path resolves to .../v1/chat/completions.
        stripped = (base_url or "").rstrip("/")
        return stripped if stripped.endswith("/v1") else stripped + "/v1"

    def _build_payload(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float,
        max_tokens: int | None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        return payload

    def _parse_text(self, body: object) -> str:
        if not isinstance(body, dict):
            return ""
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        return content if isinstance(content, str) else ""

    def _is_retryable(self, response: httpx.Response) -> bool:
        return response.status_code == 429 or 500 <= response.status_code <= 599
```

- [ ] **Step 5: Write `anthropic.py`**

Create `services/ingestion/src/llm/anthropic.py`:

```python
"""Anthropic Messages (/v1/messages) LLM service."""

from __future__ import annotations

import httpx

from src.llm.base import ChatMessage, LLMService

_DEFAULT_MAX_TOKENS = 4096


class AnthropicService(LLMService):
    """LLM client for the Anthropic Messages wire format (non-streaming)."""

    _endpoint_path = "/v1/messages"

    def _build_payload(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float,
        max_tokens: int | None,
    ) -> dict[str, object]:
        system_parts = [m.content for m in messages if m.role == "system"]
        turns = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]
        payload: dict[str, object] = {
            "model": model,
            "messages": turns,
            "max_tokens": max_tokens if max_tokens is not None else _DEFAULT_MAX_TOKENS,
            "temperature": temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        return payload

    def _extra_headers(self, payload: dict[str, object]) -> dict[str, str]:
        model = payload.get("model")
        return {"X-Model-Alias": model} if isinstance(model, str) else {}

    def _parse_text(self, body: object) -> str:
        if not isinstance(body, dict):
            return ""
        content = body.get("content")
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    def _is_retryable(self, response: httpx.Response) -> bool:
        if response.status_code == 429 or 500 <= response.status_code <= 599:
            return True
        return self._envelope_retryable(response)

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        retry_after = self._envelope_retry_after(response)
        if retry_after is not None:
            return max(retry_after, 0.0)
        return super()._retry_after(response, attempt)

    def _envelope_retryable(self, response: httpx.Response) -> bool:
        error = self._error_obj(response)
        return bool(error.get("retryable", False)) if error is not None else False

    def _envelope_retry_after(self, response: httpx.Response) -> float | None:
        error = self._error_obj(response)
        if error is None:
            return None
        value = error.get("retry_after")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    @staticmethod
    def _error_obj(response: httpx.Response) -> dict[str, object] | None:
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        error = body.get("error")
        return error if isinstance(error, dict) else None
```

- [ ] **Step 6: Write `gpt.py` and `proclaude.py`**

Create `services/ingestion/src/llm/gpt.py`:

```python
"""GPTService — concrete OpenAI-compatible client configured from GPT_* env."""

from __future__ import annotations

import os

from src.ingestion_config import LlmConfig
from src.llm.openai import OpenAIService


class GPTService(OpenAIService):
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
```

Create `services/ingestion/src/llm/proclaude.py`:

```python
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
```

- [ ] **Step 7: Write `__init__.py` and delete `llm.py`**

Create `services/ingestion/src/llm/__init__.py`:

```python
"""LLM service hierarchy for the ingestion worker.

Public surface is re-exported here so callers keep using ``from src.llm import …``.
``get_llm_service()`` returns a ProclaudeService (Anthropic /v1/messages) by default.
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
    "close_llm_service",
    "get_llm_service",
]

_llm_service: LLMService | None = None


def get_llm_service() -> LLMService:
    """Return the module-level ingestion LLM service (ProclaudeService)."""
    global _llm_service
    if _llm_service is None:
        _llm_service = ProclaudeService(llm_config=get_ingestion_config().llm)
    return _llm_service


async def close_llm_service() -> None:
    global _llm_service
    if _llm_service is not None:
        await _llm_service.close()
        _llm_service = None
```

Then delete the old module:

```bash
git rm services/ingestion/src/llm.py
```

- [ ] **Step 8: Update `test_llm_retry.py`**

Replace `services/ingestion/tests/test_llm_retry.py` with retry coverage for both protocols, driven by an injected `LlmConfig` (no `LLM_*` env):

```python
"""Regression tests for ingestion LLM retry behavior."""

from __future__ import annotations

import httpx
import pytest
from src.ingestion_config import LlmConfig
from src.llm import ChatMessage, GPTService, ProclaudeService

_NO_DELAY = LlmConfig(max_retries=2, retry_base_delay_seconds=0.0, retry_max_delay_seconds=0.0)


def _patch_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    original = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        base = kwargs.get("base_url")
        return original(transport=transport, base_url=base if isinstance(base, str) else "")

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_gpt_retries_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}},
                                  headers={"Retry-After": "0"})
        return httpx.Response(
            200,
            json={
                "id": "c", "created": 1, "model": "m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "{}"},
                             "finish_reason": "stop"}],
            },
        )

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    svc = GPTService(base_url="https://llm.test/v1", api_key="k", default_model="m",
                     llm_config=_NO_DELAY)
    result = await svc.chat_json([ChatMessage(role="user", content="extract")])
    assert result == "{}"
    assert calls == 2


@pytest.mark.asyncio
async def test_proclaude_retries_on_envelope_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                503,
                json={"error": {"type": "overloaded", "code": "overloaded", "message": "busy",
                                "retryable": True, "request_id": "r", "details": {},
                                "retry_after": 0}},
            )
        return httpx.Response(
            200,
            json={
                "id": "msg", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": "{}"}],
                "model": "claude-x", "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    svc = ProclaudeService(base_url="https://proclaude.test", api_key="svc_k",
                           default_model="claude-x", llm_config=_NO_DELAY)
    result = await svc.chat_json([ChatMessage(role="user", content="extract")])
    assert result == "{}"
    assert calls == 2
```

- [ ] **Step 9: Run tests + type-check**

Run: `uv run pytest services/ingestion/tests/test_llm_services.py services/ingestion/tests/test_llm_retry.py -v`
Expected: PASS (all).
Run: `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/llm`
Expected: no errors.

- [ ] **Step 10: Commit**

```bash
git add services/ingestion/src/llm services/ingestion/tests/test_llm_services.py services/ingestion/tests/test_llm_retry.py
git rm services/ingestion/src/llm.py
git commit -m "feat(ingestion): abstract LLMService hierarchy with GPT + Proclaude backends"
```

---

### Task 3: Route chat extraction through the new config + Proclaude model label

`chat_helpers.py` reads the request delay from `get_ingestion_config().llm` and the method label from the active service's `default_model` (the `llm_default_model` Settings field is going away).

**Files:**
- Modify: `services/ingestion/src/connectors/chat_helpers.py` (lines 11-14 imports, 129, 281-286)
- Modify: `services/ingestion/tests/test_chat_extraction_batch.py` (the `_FakeSettings` block, ~78-88)

**Interfaces:**
- Consumes: `get_ingestion_config` (Task 1), `get_llm_service().default_model` (Task 2).

- [ ] **Step 1: Update the delay source in `chat_helpers.py`**

Add the import near line 13 (after `from src.config import get_settings`):

```python
from src.ingestion_config import get_ingestion_config
```

Replace line 129 inside `_gather_extractions`:

```python
    delay_seconds = get_ingestion_config().llm.request_delay_seconds
```

- [ ] **Step 2: Update `extraction_method_label()`**

Replace the body (lines 281-286) so the label reflects the active backend model:

```python
def extraction_method_label() -> str:
    try:
        model = get_llm_service().default_model
    except Exception:
        model = "proclaude"
    return f"llm:{model}"
```

(`get_llm_service` is already imported at line 13.)

- [ ] **Step 3: Update the batch test's fake settings**

In `services/ingestion/tests/test_chat_extraction_batch.py`, the `_FakeSettings` class (with `llm_request_delay_seconds = 0.5`) and its monkeypatch of `get_settings` no longer drive the delay. Replace with a patch of `get_ingestion_config`:

```python
from src.ingestion_config import IngestionConfig, LlmConfig
from src.exclusion_config import ExclusionFile


def _fake_ingestion_config() -> IngestionConfig:
    return IngestionConfig(exclusions=ExclusionFile(), llm=LlmConfig(request_delay_seconds=0.0))
```

Then in each test that currently does
`monkeypatch.setattr(chat_helpers, "get_settings", lambda: _FakeSettings())`, replace it with:

```python
    monkeypatch.setattr(chat_helpers, "get_ingestion_config", _fake_ingestion_config)
```

Delete the now-unused `_FakeSettings` class. (Search the file for other `get_settings`/`_FakeSettings` references and update them the same way.)

- [ ] **Step 4: Run tests + type-check**

Run: `uv run pytest services/ingestion/tests/test_chat_extraction_batch.py -v`
Expected: PASS.
Run: `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/connectors/chat_helpers.py`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add services/ingestion/src/connectors/chat_helpers.py services/ingestion/tests/test_chat_extraction_batch.py
git commit -m "refactor(ingestion): chat extraction reads delay from ingestion config, model label from active LLM"
```

---

### Task 4: Migrate exclusions-file consumers to the consolidated config

`main.py`, `whatsapp/connector.py`, and `bitrix/connector.py` switch from `load_exclusion_file(settings.ingestion_exclusions_file)` to `get_ingestion_config().exclusions`.

**Files:**
- Modify: `services/ingestion/src/main.py` (line 27 import, line 229)
- Modify: `services/ingestion/src/connectors/whatsapp/connector.py` (line 43 import, 154-164)
- Modify: `services/ingestion/src/connectors/bitrix/connector.py` (line 55 import, 179-185)
- Modify: `services/ingestion/tests/test_main_exclusions.py`, `services/ingestion/tests/test_chat_exclusions.py`

**Interfaces:**
- Consumes: `get_ingestion_config` (Task 1); `ExclusionFile` stays imported where the type annotation is used.

- [ ] **Step 1: Update `main.py`**

Change the import at line 27 from:

```python
from src.exclusion_config import load_exclusion_file
```
to:
```python
from src.ingestion_config import get_ingestion_config
```

Replace line 229 (inside the `build_exclusion_context(...)` call):

```python
        file_exclusions=get_ingestion_config().exclusions,
```

- [ ] **Step 2: Update `whatsapp/connector.py`**

At line 43, change:
```python
from src.exclusion_config import ExclusionFile, load_exclusion_file
```
to:
```python
from src.exclusion_config import ExclusionFile
from src.ingestion_config import get_ingestion_config
```

Replace the try/except block at lines 154-164 so it reads exclusions directly:

```python
        try:
            settings = get_settings()
            company_mobile_numbers = list(settings.company_mobile_numbers)
            company_email_addresses = list(settings.company_email_addresses)
            internal_person_names = list(settings.internal_person_names)
            file_exclusions = get_ingestion_config().exclusions
        except Exception:
            company_mobile_numbers = []
            company_email_addresses = []
            internal_person_names = []
            file_exclusions = ExclusionFile()
```

(Removes the `exclusions_file = settings.ingestion_exclusions_file` line and the
separate `load_exclusion_file(exclusions_file)` call at line 164.)

- [ ] **Step 3: Update `bitrix/connector.py`**

Apply the identical change at line 55 (import) and lines 179-185 (read
`file_exclusions = get_ingestion_config().exclusions`, drop the `exclusions_file` var
and the `load_exclusion_file(...)` call), mirroring Step 2. Verify the surrounding
variable names match the whatsapp connector (`company_mobile_numbers`, etc.).

- [ ] **Step 4: Update `test_main_exclusions.py`**

This test monkeypatches `src.main.load_exclusion_file` and sets
`ingestion_exclusions_file` on its `_TestSettings`. Update both:

- Replace `monkeypatch.setattr("src.main.load_exclusion_file", ...)` with a patch of
  `src.main.get_ingestion_config` returning an `IngestionConfig`. For the failure test
  (line ~121-124), have the fake raise:

```python
    from src.ingestion_config import IngestionConfig

    def _fail_get_ingestion_config() -> IngestionConfig:
        raise RuntimeError("boom")

    monkeypatch.setattr("src.main.get_ingestion_config", _fail_get_ingestion_config)
```

  For the success test (line ~165), return a real config:

```python
    from src.exclusion_config import ExclusionFile
    from src.ingestion_config import IngestionConfig, LlmConfig

    def _get_ingestion_config() -> IngestionConfig:
        return IngestionConfig(exclusions=ExclusionFile(names=["Env Person"]), llm=LlmConfig())

    monkeypatch.setattr("src.main.get_ingestion_config", _get_ingestion_config)
```

  Adjust the asserted exclusion values to match whatever the test already expects from
  the file (keep the existing expectations; only the source of the data changes).
  The `ingestion_exclusions_file` attribute on `_TestSettings` can stay (harmless) or be
  removed — it's no longer read.

- [ ] **Step 5: Update `test_chat_exclusions.py`**

Replace the two `monkeypatch.setattr("src.connectors.<X>.connector.load_exclusion_file", fake_load_exclusion_file)` (lines ~133 and ~219) with patches of
`get_ingestion_config` in each connector module:

```python
    from src.exclusion_config import ExclusionFile
    from src.ingestion_config import IngestionConfig, LlmConfig

    def fake_get_ingestion_config() -> IngestionConfig:
        return IngestionConfig(exclusions=<the ExclusionFile the test was returning>, llm=LlmConfig())

    monkeypatch.setattr(
        "src.connectors.bitrix.connector.get_ingestion_config", fake_get_ingestion_config
    )
    # ...and for the whatsapp case:
    monkeypatch.setattr(
        "src.connectors.whatsapp.connector.get_ingestion_config", fake_get_ingestion_config
    )
```

Reuse the exact `ExclusionFile(...)` the existing `fake_load_exclusion_file` returned so
the assertions are unchanged. The `_TestSettings(ingestion_exclusions_file="ignored.json")`
instances can keep that attribute (no longer read).

- [ ] **Step 6: Run tests + type-check**

Run: `uv run pytest services/ingestion/tests/test_main_exclusions.py services/ingestion/tests/test_chat_exclusions.py -v`
Expected: PASS.
Run: `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/main.py services/ingestion/src/connectors/whatsapp/connector.py services/ingestion/src/connectors/bitrix/connector.py`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add services/ingestion/src/main.py services/ingestion/src/connectors/whatsapp/connector.py services/ingestion/src/connectors/bitrix/connector.py services/ingestion/tests/test_main_exclusions.py services/ingestion/tests/test_chat_exclusions.py
git commit -m "refactor(ingestion): connectors read exclusions from consolidated ingestion config"
```

---

### Task 5: Remove dead Settings fields, rename config files, update infra

Drop the now-unused LLM tuning + old exclusions-file `Settings` fields, rename the sample config files (adding the `llm` block + `exclusions` wrapper), and update `docker-compose.yml` / staging / `.env.example`.

**Files:**
- Modify: `services/ingestion/src/config.py` (remove lines 131, 134-138)
- Rename + edit: `config/ingestion-exclusions.example.json` → `config/ingestion-config.example.json`
- Rename + edit: `config/ingestion-exclusions.local.json` → `config/ingestion-config.local.json`
- Modify: `docker-compose.yml`, `.docker/staging/docker-compose.yml`, `.env.example`

**Interfaces:**
- Consumes: nothing new. Verifies no remaining references to the removed fields exist.

- [ ] **Step 1: Verify no remaining references to removed fields**

Run:
```bash
grep -rn "ingestion_exclusions_file\|llm_default_model\|llm_request_delay_seconds\|llm_max_retries\|llm_retry_base_delay_seconds\|llm_retry_max_delay_seconds" services/ingestion/src
```
Expected: **no matches** (all migrated in Tasks 2-4). If any remain, migrate them before continuing.

- [ ] **Step 2: Remove dead `Settings` fields**

In `services/ingestion/src/config.py`, delete `ingestion_exclusions_file: str = ""` (line 131) and the entire LLM block (lines 133-138):

```python
    # LLM service -------------------------------------------------------------
    llm_default_model: str = "Qwen/Qwen2.5-72B-Instruct"
    llm_request_delay_seconds: float = 0.5
    llm_max_retries: int = 6
    llm_retry_base_delay_seconds: float = 1.0
    llm_retry_max_delay_seconds: float = 30.0
```

Keep `ingestion_config_file: str = ""` (added in Task 1) and the `company_*`/`internal_person_names` fields.

- [ ] **Step 3: Rename + rewrite the example config file**

```bash
git mv config/ingestion-exclusions.example.json config/ingestion-config.example.json
```
Set its contents to:

```json
{
  "exclusions": {
    "phones": [],
    "emails": [],
    "email_domains": [],
    "names": [],
    "source_ids": [],
    "machine_unit_identifiers": []
  },
  "llm": {
    "timeout_seconds": 60.0,
    "request_delay_seconds": 0.5,
    "max_retries": 6,
    "retry_base_delay_seconds": 1.0,
    "retry_max_delay_seconds": 30.0
  }
}
```

- [ ] **Step 4: Rename + rewrite the local config file**

```bash
git mv config/ingestion-exclusions.local.json config/ingestion-config.local.json
```
Wrap its existing exclusion arrays under an `"exclusions"` key and append the same `"llm"` block as Step 3 (preserve whatever phone/email/name values the local file already had). Because `load_ingestion_config` is backward-compatible, an un-migrated local file still loads — but migrate it for clarity.

- [ ] **Step 5: Update `docker-compose.yml`**

In the ingestion/worker/beat env section (around lines 46-48 and 70):

- Remove:
```yaml
  LLM_API_BASE_URL: ${LLM_API_BASE_URL:-https://gpt.ada.asia/api/v1}
  LLM_API_KEY: ${LLM_API_KEY:-}
  LLM_DEFAULT_MODEL: ${LLM_DEFAULT_MODEL:-Qwen/Qwen2.5-72B-Instruct}
```
- Add:
```yaml
  GPT_API_BASE_URL: ${GPT_API_BASE_URL:-https://gpt.ada.asia/api/v1}
  GPT_API_KEY: ${GPT_API_KEY:-}
  GPT_DEFAULT_MODEL: ${GPT_DEFAULT_MODEL:-gpt-4o}
  PROCLAUDE_API_BASE_URL: ${PROCLAUDE_API_BASE_URL:-https://proclaude.sparkfn.io}
  PROCLAUDE_API_KEY: ${PROCLAUDE_API_KEY:-}
  PROCLAUDE_DEFAULT_MODEL: ${PROCLAUDE_DEFAULT_MODEL:-claude-sonnet-4}
```
- Rename line 70:
```yaml
  INGESTION_CONFIG_FILE: ${INGESTION_CONFIG_FILE:-/app/config/ingestion-config.example.json}
```

- [ ] **Step 6: Mirror into `.docker/staging/docker-compose.yml`**

Apply the exact same three edits (line ~75 for the config file var) to
`.docker/staging/docker-compose.yml`. Per the repo sync rule, only build-context/`name:`/volume paths may differ — the env vars above must match.

- [ ] **Step 7: Update `.env.example`**

Remove the `LLM_API_BASE_URL` / `LLM_API_KEY` / `LLM_DEFAULT_MODEL` lines (around 58-62);
add `GPT_*` and `PROCLAUDE_*` examples and rename any `INGESTION_EXCLUSIONS_*` host-file
hint to `INGESTION_CONFIG_*`. Example block:

```bash
GPT_API_BASE_URL=https://gpt.ada.asia/api/v1
GPT_API_KEY=
GPT_DEFAULT_MODEL=llama-3.3-70b-versatile

PROCLAUDE_API_BASE_URL=https://proclaude.sparkfn.io
PROCLAUDE_API_KEY=
PROCLAUDE_DEFAULT_MODEL=claude-sonnet-4

INGESTION_CONFIG_FILE=/app/config/ingestion-config.example.json
```

- [ ] **Step 8: Full verification**

Run: `uv run pytest services/ingestion/tests`
Expected: PASS (whole ingestion suite).
Run: `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src`
Expected: no errors.
Run: `uv run --package profile-unifier-ingestion ruff check services/ingestion/src && uv run --package profile-unifier-ingestion ruff format --check services/ingestion/src`
Expected: clean (run `ruff format` without `--check` to apply, then re-check).
Run: `docker compose config -q` and `docker compose -f .docker/staging/docker-compose.yml config -q`
Expected: both validate with no error.

- [ ] **Step 9: Commit**

```bash
git add services/ingestion/src/config.py config/ docker-compose.yml .docker/staging/docker-compose.yml .env.example
git commit -m "chore(ingestion): drop LLM_*/exclusions-file settings, consolidate config + env to GPT_*/PROCLAUDE_*"
```

---

## Self-Review

**Spec coverage:**
- GPTService + GPT_* env → Tasks 2, 5. ✓
- Consolidated JSON (exclusions + llm), file/var rename → Tasks 1, 4, 5. ✓
- Abstract LLMService → OpenAI/Anthropic → GPT/Proclaude → Task 2. ✓
- Proclaude as ingestion default → Task 2 (`get_llm_service`), Task 3 (label). ✓
- LLM tuning JSON-only (removed from Settings/env) → Tasks 1 (source), 2 (consumer), 5 (removal). ✓
- Backward-compat bare-exclusions format → Task 1. ✓
- Vestigial company_* left as-is → preserved in Tasks 4, 5. ✓
- docker-compose staging sync → Task 5 Steps 5-6. ✓
- Tests for retry (both protocols), services, config, migrated consumers → Tasks 1-4. ✓

**Placeholder scan:** Task 4 Steps 4-5 say "reuse the exact `ExclusionFile(...)` the existing fake returned" — this is intentional (the existing test value is the source of truth; copying it verbatim here would risk drift). All code steps include concrete code. No TBD/TODO.

**Type consistency:** `chat_json(messages, *, model, temperature, max_tokens)` signature consistent across base + tests. `LlmConfig` field names (`timeout_seconds`, `request_delay_seconds`, `max_retries`, `retry_base_delay_seconds`, `retry_max_delay_seconds`) consistent across Tasks 1, 2, 5. `get_ingestion_config()`/`IngestionConfig.exclusions`/`.llm` consistent across Tasks 1, 3, 4. `default_model` property consistent across Task 2, 3.
