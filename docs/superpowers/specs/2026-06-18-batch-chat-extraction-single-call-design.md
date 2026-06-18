# Batch chat extraction — one LLM call per batch

**Date:** 2026-06-18
**Scope:** `services/ingestion/` chat extraction path only.

## Problem

The chat-extraction path (`bitrix_chat` / `whatsapp_chat`) makes **one LLM call per
chat** and `asyncio.gather`s a whole `LLM_BATCH_SIZE = 20` batch concurrently
(`chat_helpers.py:_gather_extractions`). After migrating ingestion's LLM backend to
`ProclaudeService` (proxy-claude-v2, `gemma4-12b`), that fan-out — up to ~40 concurrent
requests across the two chat tasks, amplified by 6× retries — overwhelms proclaude's
pooled-credential rate limit, producing a sustained 429 storm (observed: 534× 429, 22×
503, only 222× 200) and dropping chat records whose retries exhaust.

Address normalization is **not** in scope: it already makes one call per record (a
record's addresses are batched into a single call) and contributes only ~4 concurrent
calls across tasks.

## Decision (from brainstorming)

Combine each batch's per-chat calls into **one LLM call per batch**, using the existing
address-normalization batch pattern (`input_index`). Use a **smaller, configurable batch
size** to bound output token size; rely on the existing retry/backoff. A failed or
truncated batch loses those ≤N chats (no per-chat fallback).

## Design

### 1. Prompt (`llm_prompts.py`)

New combined template + builder `build_batch_extraction_prompt(texts: list[str]) -> str`.
It lists each conversation with an index and instructs the model to return one JSON
object wrapping an array, each entry keeping today's exact per-conversation shape plus a
`conversation_index`:

```json
{ "conversations": [
  { "conversation_index": 0,
    "persons": [...], "possible_persons": [...], "transactions": [...],
    "chat_members": [...], "inquiries": [...], "strong_identifiers": [...],
    "weak_identifiers": [...], "summary": "...", "customer_sentiment": "...",
    "confidence": 0.9 },
  { "conversation_index": 1, ... }
] }
```

`EXTRACTION_SYSTEM` is reused. The old single-conversation `build_extraction_prompt` is
removed (its only callers are `_extract_one`, deleted below, and a test).

### 2. `run_extraction_batch` (`chat_helpers.py`)

- Make **one** `chat_json` call for the whole `texts` list, passing
  `max_tokens=chat_max_tokens` so a multi-conversation response does not truncate
  (today's Anthropic default 4096 would).
- Refactor the existing ~90-line inline per-conversation parsing into a reusable
  `_parse_extraction_object(obj: JsonValue) -> ExtractionResult | None`.
- Parse the response's `conversations` array, index entries by `conversation_index`, and
  return a `list[ExtractionResult | None]` aligned to input order — `None` for any index
  the model omitted.
- On a raised exception or invalid/again-non-dict JSON: return `[None] * len(texts)`.
- Delete `_extract_one` and `_gather_extractions` (and the per-index `asyncio.sleep`
  stagger) — this path no longer runs concurrent calls.

`asyncio` is still used (`asyncio.run` wraps the single async `chat_json`).

### 3. Config (`LlmConfig` in `ingestion_config.py`)

Two new fields (output size is the main risk; both belong with the other LLM tuning):

```python
chat_batch_size: int = 8       # conversations per combined LLM call
chat_max_tokens: int = 8192    # output budget for the combined response
```

Parsed in `_llm_config` (reuse the existing `_int` helper). Added to
`config/ingestion-config.example.json`'s `llm` block.

### 4. Connectors (`whatsapp/connector.py`, `bitrix/connector.py`)

Replace the module constant `LLM_BATCH_SIZE = 20` with a read of
`get_ingestion_config().llm.chat_batch_size` at the point the batch loop slices. The
outer `for i in range(0, len(all_bundles), batch_size)` loop is unchanged in shape; each
slice now becomes exactly one LLM call.

### 5. Tests (`test_chat_extraction_batch.py`)

- Update the fake LLM services to return the wrapped `{"conversations": [...]}` shape.
- Cases: correct split/alignment by `conversation_index`; a response missing one index →
  that position is `None`; invalid JSON → all-`None`; object-summary preservation
  (existing assertion) carried through `_parse_extraction_object`.
- `test_chat_extraction_batch_runs_async_gather_inside_event_loop` (which asserted the
  per-index sleep schedule `[0.5, 1.0]`) is removed/replaced — the stagger is gone.

## Net effect

A 20-chat source goes from ~20 concurrent calls/batch to **1 sequential call per ≤8-chat
batch** — a ~20×+ reduction in request volume. Peak concurrency across the 4 parallel
ingestion tasks drops from ~40 to ~4, which should clear the 429 storm. Trade-off: a
failed batch loses up to `chat_batch_size` chats instead of one.

## Out of scope

- Address normalization (already 1 call/record).
- Global cross-process LLM rate limiting (Redis semaphore) — unnecessary once the chat
  fan-out is removed; revisit only if 429s persist.
- Per-chat fallback on batch failure.
