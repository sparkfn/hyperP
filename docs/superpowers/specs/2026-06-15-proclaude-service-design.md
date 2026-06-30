# ProclaudeService design

## Context

The proxy-claude-v2 API (https://proclaude.sparkfn.io) exposes an Anthropic-compatible
`POST /v1/messages` endpoint (verbatim Anthropic Messages wire format, passthrough to
the active upstream provider). We need a thin async client + module-level singleton for
this endpoint, following the existing `LLMService` pattern at
`services/api/src/llm/service.py`, scoped to `/v1/messages` only (non-streaming).

## Spec facts (from https://proclaude.sparkfn.io/openapi.json)

- `POST /v1/messages` — `operationId: postMessages`. Request body is the Anthropic
  Messages API request, verbatim (schema is `{"type": "object"}` in the OpenAPI doc —
  passthrough, not enumerated). Response is the Anthropic Message object (JSON) or an
  SSE stream when `stream: true` (out of scope here).
- Auth: `security` lists `BearerAuth` (pct_ machine token + fingerprint headers),
  `ServiceCredentialBearer` (`Authorization: Bearer svc_...`), `ServiceCredential`
  (`X-API-Key: svc_...`). We use **ServiceCredentialBearer** — no fingerprint headers,
  no machine-registration flow.
- `X-Proclaude-Fingerprint-Sha256` / `X-Proclaude-Fingerprint-Version` are optional
  headers required only for `pct_` machine-token auth — not sent for `svc_` credentials.
- `X-Model-Alias` is **not** a parameter on `/v1/messages` (only on
  `/anthropic/v1/messages`, `/openai/v1/*`, and provider passthrough) — the model is
  selected via the body `model` field per Anthropic semantics.
- Errors: every non-2xx response is `ErrorEnvelope { error: ErrorDetail }`, where
  `ErrorDetail` has 6 mandatory fields (`type`, `code`, `message`, `retryable`,
  `request_id`, `details`) plus 4 optional recovery fields (`hint`, `example`, `param`,
  `retry_after`).

## Design

### Package layout

New package `services/api/src/proclaude/`, with `service.py` mirroring
`services/api/src/llm/service.py`.

### Config (`src/config.py`)

```python
proclaude_api_base_url: str = Field(default="https://proclaude.sparkfn.io", alias="PROCLAUDE_API_BASE_URL")
proclaude_api_key: str | None = Field(default=None, alias="PROCLAUDE_API_KEY")
proclaude_default_model: str | None = Field(default=None, alias="PROCLAUDE_DEFAULT_MODEL")
```

### Types (Anthropic Messages wire format, hand-modeled — passthrough schema isn't
enumerated in the OpenAPI doc)

- `TextBlock` (`type: Literal["text"]`, `text: str`)
- `ToolUseBlock` (`type: Literal["tool_use"]`, `id: str`, `name: str`,
  `input: dict[str, object]`)
- `ToolResultBlock` (`type: Literal["tool_result"]`, `tool_use_id: str`,
  `content: str | list[TextBlock]`)
- `ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock` (discriminated on `type`)
- `MessageParam` (`role: Literal["user", "assistant"]`,
  `content: str | list[ContentBlock]`)
- `MessagesUsage` (`input_tokens: int`, `output_tokens: int`)
- `MessagesRequest` (`model: str`, `messages: list[MessageParam]`, `max_tokens: int`,
  `system: str | None`, `temperature: float | None`, `top_p: float | None`,
  `top_k: int | None`, `stop_sequences: list[str] | None`,
  `tools: list[dict[str, object]] | None`, `tool_choice: dict[str, object] | None`,
  `metadata: dict[str, str] | None`)
- `MessagesResponse` (`id: str`, `type: Literal["message"]`,
  `role: Literal["assistant"]`, `content: list[ContentBlock]`, `model: str`,
  `stop_reason: str | None`, `stop_sequence: str | None`, `usage: MessagesUsage`)
- `ErrorDetail` (`type: str`, `code: str`, `message: str`, `retryable: bool`,
  `request_id: str`, `details: dict[str, object]`, optional `hint`, `example`,
  `param`, `retry_after`)
- `ErrorEnvelope` (`error: ErrorDetail`)
- `ProclaudeAPIError(Exception)` — carries the parsed `ErrorDetail`
  (`.detail: ErrorDetail`); falls back to a generic error (still raised as
  `ProclaudeAPIError`, wrapping `httpx.HTTPStatusError`) if the response body isn't a
  valid `ErrorEnvelope`.

### `ProclaudeService`

```python
class ProclaudeService:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 60.0) -> None: ...
    async def close(self) -> None: ...
    async def create_message(
        self,
        messages: list[MessageParam],
        model: str | None = None,
        max_tokens: int = 1024,
        system: str | None = None,
        temperature: float = 0.0,
        top_p: float | None = None,
        top_k: int | None = None,
        stop_sequences: list[str] | None = None,
        tools: list[dict[str, object]] | None = None,
        tool_choice: dict[str, object] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> MessagesResponse: ...
    async def create_message_text(self, ...) -> str: ...  # same params, returns concatenated TextBlock text
```

- `model` defaults to `config.proclaude_default_model or "claude-sonnet-4"`.
- Auth header: `Authorization: Bearer {config.proclaude_api_key}`.
- On non-2xx: attempt `ErrorEnvelope.model_validate(response.json())` and raise
  `ProclaudeAPIError(detail)`; if that fails, raise `ProclaudeAPIError` wrapping the
  `httpx.HTTPStatusError` from `response.raise_for_status()`.

### Singleton

`get_proclaude_service()` / `close_proclaude_service()` — same lazy-init /
module-global pattern as `get_llm_service()` / `close_llm_service()`. Wired into
`app.py`'s `_lifespan`, called alongside `close_llm_service()`.

## Out of scope

- Streaming (SSE) responses.
- `/v1/messages/count_tokens` and all other proxy endpoints.
- `pct_` machine-token auth + fingerprint headers + machine registration.
