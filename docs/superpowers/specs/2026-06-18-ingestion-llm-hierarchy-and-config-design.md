# Ingestion LLM service hierarchy + consolidated config — design

**Date:** 2026-06-18
**Scope:** `services/ingestion/` only. The API service's `llm/service.py` and
`proclaude/service.py` are unchanged.

## Context

The ingestion worker calls an LLM to extract structured identity/transaction data
from chat text (`connectors/chat_helpers.py`) and to normalize addresses
(`normalizers/address.py`). Both go through `src/llm.py`, a single OpenAI-compatible
`LLMService` calling `/v1/chat/completions`, via `svc.chat_json(messages) -> str`.

We are:

1. Renaming the OpenAI-compatible client to **`GPTService`** and switching its env
   vars `LLM_API_*` → `GPT_API_*`.
2. Consolidating ingestion config (exclusions **and** LLM call tuning — timeout,
   request delay, retries) into a single JSON file.
3. Introducing an abstract `LLMService` base with two protocol-specific subclasses —
   `OpenAIService` and `AnthropicService` — with `GPTService(OpenAIService)` and
   `ProclaudeService(AnthropicService)` as concrete clients.
4. Making ingestion's actual LLM calls run through **`ProclaudeService`** (the
   proxy-claude-v2 Anthropic `/v1/messages` endpoint) by default.

The API service already ships a `ProclaudeService` (`services/api/src/proclaude/service.py`)
and an OpenAI-compatible `LLMService` (`services/api/src/llm/service.py`); they are the
reference patterns for the Anthropic and OpenAI wire formats respectively. The
ingestion `.env` already defines `GPT_*` and `PROCLAUDE_*` vars with `LLM_*` commented
out.

## Decisions (resolved during brainstorming)

| Decision | Choice |
|---|---|
| Where the hierarchy lives | **Ingestion only.** API service untouched. |
| OpenAI-compatible class + env | **`GPTService`** + `GPT_API_BASE_URL` / `GPT_API_KEY` / `GPT_DEFAULT_MODEL`. |
| LLM timeout/delay/retry settings | **JSON only** — removed from `Settings`/env. |
| Default ingestion backend | **`ProclaudeService`** (Anthropic `/v1/messages`). |
| Consolidated config file | Rename to **`ingestion-config.json`**; env var `INGESTION_EXCLUSIONS_FILE` → **`INGESTION_CONFIG_FILE`**; shape `{ "exclusions": {…}, "llm": {…} }`. |
| Vestigial `company_*` / `internal_person_names` Settings fields | **Leave as-is** — they keep merging with the file exclusions via `build_exclusion_context`. |

## Architecture

### New `src/llm/` package (replaces `src/llm.py`)

`src/llm.py` becomes a package so existing imports
(`from src.llm import ChatMessage, get_llm_service`) keep working via `__init__.py`
re-exports.

```
src/llm/
  __init__.py      # public re-exports; get_llm_service / close_llm_service
  base.py          # abstract LLMService: ChatMessage, chat_json, retry loop
  openai.py        # OpenAIService(LLMService) + OpenAI wire types
  anthropic.py     # AnthropicService(LLMService) + Anthropic wire types
  gpt.py           # GPTService(OpenAIService)
  proclaude.py     # ProclaudeService(AnthropicService)
```

#### `base.py` — abstract `LLMService`

Owns everything protocol-agnostic:

- The shared `ChatMessage` model (`role: "system" | "user" | "assistant"`, `content: str`).
- An injected `LlmConfig` (timeout, request delay, retry params) — see config section.
- The unified entrypoint `async def chat_json(messages: list[ChatMessage], *, model: str | None = None, temperature: float = 0.0, max_tokens: int | None = None) -> str`.
- The retry/backoff loop (`max_retries`, exponential base/max delay with jitter,
  honoring a `Retry-After`-style hint), ported from today's `_post_chat_completion` /
  `_retry_delay`.
- A lazily-created, reused `httpx.AsyncClient` (`_ensure_client` / `close`), matching the
  API service's pattern (today's ingestion client opens a fresh client per call — the
  base adopts the reuse pattern).

The **protocol-specific hooks** subclasses must implement ("protocol properties"):

```python
class LLMService(ABC):
    _endpoint_path: str                         # class attr: "/v1/chat/completions" | "/v1/messages"

    @abstractmethod
    def _build_payload(self, messages, model, temperature, max_tokens) -> dict[str, object]: ...
    @abstractmethod
    def _extra_headers(self, payload) -> dict[str, str]: ...     # e.g. X-Model-Alias
    @abstractmethod
    def _parse_text(self, body: object) -> str: ...             # → assistant text
    @abstractmethod
    def _is_retryable(self, response: httpx.Response) -> bool: ...
    @abstractmethod
    def _retry_after(self, response: httpx.Response, attempt: int) -> float: ...
    @abstractmethod
    def _default_model(self) -> str: ...
```

`chat_json` builds the payload via the hooks, POSTs to `_endpoint_path`, runs the retry
loop, and returns `_parse_text(response.json())`.

#### `openai.py` — `OpenAIService(LLMService)`

- `_endpoint_path = "/v1/chat/completions"`.
- Wire types ported from today's `llm.py` (`ChatCompletionRequest/Response`, `Usage`).
- `_build_payload`: messages verbatim; sets `response_format={"type": "json_object"}` for
  JSON mode.
- `_extra_headers`: none beyond auth.
- `_parse_text`: `choices[0].message.content or ""`.
- `_is_retryable`: `status == 429 or 500 <= status <= 599`.
- `_retry_after`: `Retry-After` header, else exponential backoff with jitter.

#### `anthropic.py` — `AnthropicService(LLMService)`

- `_endpoint_path = "/v1/messages"`.
- Wire types ported from `services/api/src/proclaude/service.py`: `TextBlock`,
  `ToolUseBlock`, `ToolResultBlock`, `ContentBlock`, `MessageParam`, `MessagesUsage`,
  `MessagesRequest`, `MessagesResponse`, `ErrorDetail`, `ErrorEnvelope`.
- `_build_payload`: split the `ChatMessage` list — the single `system`-role message (if
  any) becomes the `system` param; remaining `user`/`assistant` messages map to
  `MessageParam`s. `max_tokens` defaults to a sane value (e.g. 1024) when `None`.
  No native JSON mode — JSON output is prompt-driven (see below).
- `_extra_headers`: `X-Model-Alias: {model}` (matches the API ProclaudeService).
- `_parse_text`: concatenate the `text` of every `TextBlock` in `content`.
- `_is_retryable`: HTTP 429/5xx **or** a parsed `ErrorEnvelope` with `error.retryable == true`.
- `_retry_after`: `error.retry_after` when present, else `Retry-After` header, else backoff.

**JSON-by-prompting note:** Anthropic `/v1/messages` has no `response_format` JSON mode.
The existing `EXTRACTION_SYSTEM` and `ADDRESS_NORMALIZATION_SYSTEM` prompts already
instruct the model to emit JSON, so `chat_json` returning the raw text is sufficient and
the call sites in `address.py`/`chat_helpers.py` are unchanged. (If those prompts prove
to need reinforcement under Anthropic, that is a prompt tweak, not an interface change —
out of scope here.)

#### `gpt.py` — `GPTService(OpenAIService)`

Thin concrete subclass: `_default_model()` and base URL / API key default from
`GPT_API_BASE_URL` / `GPT_API_KEY` / `GPT_DEFAULT_MODEL`. Constructor still accepts
`base_url` / `api_key` / `default_model` overrides (used by tests).

#### `proclaude.py` — `ProclaudeService(AnthropicService)`

Thin concrete subclass: base URL / API key / model default from
`PROCLAUDE_API_BASE_URL` / `PROCLAUDE_API_KEY` / `PROCLAUDE_DEFAULT_MODEL`
(model fallback `"claude-sonnet-4"`).

#### `__init__.py`

Re-exports `ChatMessage`, `LLMService`, `OpenAIService`, `AnthropicService`,
`GPTService`, `ProclaudeService`, `get_llm_service`, `close_llm_service`.

```python
_llm_service: LLMService | None = None

def get_llm_service() -> LLMService:
    global _llm_service
    if _llm_service is None:
        _llm_service = ProclaudeService(llm_config=load_ingestion_config(...).llm)
    return _llm_service
```

`get_llm_service()` returns a **`ProclaudeService`** singleton (task 4). It is typed as
the abstract `LLMService` so callers stay backend-agnostic.

### Consolidated config — `src/ingestion_config.py` (new)

One JSON file holds exclusions and LLM tuning. The env var
`INGESTION_EXCLUSIONS_FILE` → `INGESTION_CONFIG_FILE`; the sample file
`config/ingestion-exclusions.example.json` → `config/ingestion-config.example.json`
(and the local override `ingestion-exclusions.local.json` → `ingestion-config.local.json`).

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

Types:

```python
@dataclass
class LlmConfig:
    timeout_seconds: float = 60.0
    request_delay_seconds: float = 0.5
    max_retries: int = 6
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 30.0

@dataclass
class IngestionConfig:
    exclusions: ExclusionFile
    llm: LlmConfig
```

`load_ingestion_config(path: str) -> IngestionConfig`:

- Reuses the existing `exclusion_config.py` parsers for the `exclusions` block.
- A new `_llm_config(raw, path)` parser validates the `llm` block (typed, strict —
  same `ValueError("Invalid ingestion config JSON: …")` style as today).
- **Backward compat:** if the top-level object has neither `exclusions` nor `llm` keys
  (i.e. the old bare `{phones, emails, …}` format), treat the whole object as the
  `exclusions` block and use `LlmConfig()` defaults. This keeps existing deployments
  working through the file rename.
- Empty/blank path → `IngestionConfig(ExclusionFile(), LlmConfig())`.

The existing `load_exclusion_file` stays for backward compatibility but the connectors
move to `load_ingestion_config`; `build_exclusion_context` keeps consuming
`config.exclusions` plus the (unchanged) env company lists.

### `Settings` changes (`src/config.py`)

- **Remove:** `llm_default_model`, `llm_request_delay_seconds`, `llm_max_retries`,
  `llm_retry_base_delay_seconds`, `llm_retry_max_delay_seconds` (now JSON-only).
- **Rename:** `ingestion_exclusions_file` → `ingestion_config_file`
  (env `INGESTION_CONFIG_FILE`).
- **Keep:** `company_mobile_numbers`, `company_email_addresses`, `internal_person_names`
  (vestigial but retained per decision).
- GPT/Proclaude secrets (base URL, key, model) are read from the environment directly by
  the service subclasses, matching how `llm.py` reads `os.environ` today — not added to
  `Settings`.

### Wiring touch points

- `connectors/chat_helpers.py`: `_gather_extractions` reads
  `load_ingestion_config(get_settings().ingestion_config_file).llm.request_delay_seconds`
  instead of `get_settings().llm_request_delay_seconds`. `chat_json` call unchanged.
- `normalizers/address.py`: `chat_json` call unchanged (imports still resolve via the
  package `__init__`).
- `docker-compose.yml` **and** `.docker/staging/docker-compose.yml` (sync rule):
  - Drop `LLM_API_BASE_URL` / `LLM_API_KEY` / `LLM_DEFAULT_MODEL`.
  - Add `GPT_API_BASE_URL` / `GPT_API_KEY` / `GPT_DEFAULT_MODEL` and
    `PROCLAUDE_API_BASE_URL` / `PROCLAUDE_API_KEY` / `PROCLAUDE_DEFAULT_MODEL`.
  - `INGESTION_EXCLUSIONS_FILE` → `INGESTION_CONFIG_FILE`
    (default `/app/config/ingestion-config.example.json`).
- `.env.example`: mirror the compose changes (LLM_* removed, GPT_*/PROCLAUDE_* added,
  config file var renamed).
- `config/`: rename the example/local JSON files and add the `llm` block.

## Testing

- `tests/test_llm_retry.py`: retarget at `GPTService` (OpenAI path) and at
  `ProclaudeService` (Anthropic path). Retry params come from an injected `LlmConfig`,
  not `LLM_*` env vars — the test constructs the service with a zero-delay `LlmConfig`.
  Add an Anthropic-path retry case (429 then 200, plus an `ErrorEnvelope.retryable` case).
- `tests/test_address_normalizer.py`: the `get_llm_service` monkeypatches keep working
  (fake returns `chat_json`); no behavioral change since the call site is unchanged.
- New `tests/test_ingestion_config.py`: nested format parse, backward-compat bare-exclusions
  format, invalid `llm` block rejection, missing file, blank path defaults.
- New `tests/test_llm_services.py`: `GPTService` builds an OpenAI payload + parses choices;
  `ProclaudeService` splits system/messages, sends `X-Model-Alias`, concatenates
  `TextBlock`s; `get_llm_service()` returns a `ProclaudeService`.
- Run `ruff check`, `ruff format`, `mypy --strict` on changed files only;
  `uv run pytest services/ingestion/tests`.

## Out of scope

- API service LLM classes (untouched).
- Streaming (SSE) responses; `/v1/messages/count_tokens`; tool-use round-trips
  (the wire types are modeled for fidelity but ingestion only uses text `chat_json`).
- `pct_` machine-token auth / fingerprint headers (ServiceCredential `svc_` bearer only).
- Reworking the extraction/address prompts for Anthropic beyond what JSON-by-prompting
  already provides.
- Folding the vestigial `company_*` env lists into the JSON.
