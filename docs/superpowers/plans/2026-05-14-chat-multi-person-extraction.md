# Chat Multi-Person Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split chat-derived identity evidence into one conversation SourceRecord per extracted possible person, process every person through the existing matching pipeline, and create pending KNOWS edges when relationship evidence is present.

**Architecture:** Extend chat extraction to support grouped `possible_persons` while accepting legacy `persons` output. Add focused helper functions in `chat_helpers.py` to normalize person groups into per-person envelope payloads, update WhatsApp and Bitrix connectors to emit multiple records per chat, and add a post-ingest relationship materializer for chat-derived pending KNOWS edges. Keep the existing invariant that one SourceRecord resolves to one Person.

**Tech Stack:** Python 3.12, Pydantic, Neo4j/Cypher, pytest, ruff, mypy strict, existing ingestion connector and graph query modules.

---

### Task 1: Prompt and extraction model

**Files:**
- Modify: `services/ingestion/src/llm_prompts.py`
- Modify: `services/ingestion/src/connectors/chat_helpers.py`
- Test: `services/ingestion/tests/test_chat_extraction_batch.py`

- [ ] Add `ExtractedPossiblePerson` with identity fields, `role`, `relationship_to_primary`, `relationship_label`, `identifiers`, `weak_identifiers`, `evidence`, and `confidence`.
- [ ] Update `ExtractionResult` to include `possible_persons: list[ExtractedPossiblePerson]`.
- [ ] Update parsing so `possible_persons` is read when present and legacy `persons` is converted to one possible person each.
- [ ] Update `EXTRACTION_TEMPLATE` to ask for grouped possible persons and clarify that secondary external people should be extracted, while agents/staff stay in `chat_members`.
- [ ] Add/update tests asserting the prompt mentions `possible_persons`, grouped identifiers, secondary external people, and pending relationship evidence.

Run:
```bash
uv run pytest services/ingestion/tests/test_chat_extraction_batch.py -v
```
Expected: prompt/helper tests pass.

### Task 2: Per-person envelope helpers

**Files:**
- Modify: `services/ingestion/src/connectors/chat_helpers.py`
- Test: `services/ingestion/tests/test_chat_extraction_batch.py`

- [ ] Add `possible_persons_from_extraction(extraction)` returning normalized grouped persons.
- [ ] Add `identifiers_from_possible_person(person)` that deduplicates a single possible person's legacy fields and strong identifiers, excluding government IDs from raw identifier writes as current code does.
- [ ] Add `weak_identifiers_for_possible_person(person)` and `possible_person_payload(person)` for raw payload evidence.
- [ ] Keep `identifiers_from_extraction(extraction)` as a legacy compatibility wrapper that flattens all possible persons.
- [ ] Add tests proving Alice's phone and Bob's email stay in separate per-person identifier lists.

Run:
```bash
uv run pytest services/ingestion/tests/test_chat_extraction_batch.py -v
```
Expected: per-person helper tests pass.

### Task 3: WhatsApp connector emits one record per possible person

**Files:**
- Modify: `services/ingestion/src/connectors/whatsapp/connector.py`
- Test: `services/ingestion/tests/test_whatsapp_connector.py`

- [ ] Replace `_build_envelope(...) -> dict | None` with `_build_envelopes(...) -> list[dict[str, JsonValue]]`.
- [ ] Emit one SourceRecord per possible person with IDs like `whatsapp-chat-{chat_id}-person-{index}`.
- [ ] Preserve shared chat payload: participants, message_endpoints, transactions, inquiries, summary, sentiment, and original extraction.
- [ ] Put only that possible person's identifiers and `full_name` on the envelope.
- [ ] Store per-person relationship fields in raw payload for later KNOWS materialization.
- [ ] Update callers to yield every returned envelope.
- [ ] Keep a thin `_build_envelope` compatibility wrapper only if tests or existing internal callers need it.
- [ ] Add tests asserting two possible persons produce two records and identifiers do not cross-contaminate.

Run:
```bash
uv run pytest services/ingestion/tests/test_whatsapp_connector.py -v
```
Expected: WhatsApp connector tests pass.

### Task 4: Bitrix connector emits one record per possible person

**Files:**
- Modify: `services/ingestion/src/connectors/bitrix/connector.py`
- Test: `services/ingestion/tests/test_bitrix_connector.py`

- [ ] Mirror the WhatsApp connector changes with IDs like `bitrix-chat-{chat_id}-person-{index}`.
- [ ] Preserve deal metadata and shared chat evidence in each raw payload.
- [ ] Update `_fetch_chats` to yield every per-person envelope.
- [ ] Add tests asserting multiple possible persons from one chat create multiple SourceRecords with isolated identifiers.

Run:
```bash
uv run pytest services/ingestion/tests/test_bitrix_connector.py -v
```
Expected: Bitrix connector tests pass.

### Task 5: Pending KNOWS materialization for chat relationships

**Files:**
- Modify: `services/ingestion/src/pipeline_knows.py`
- Modify: `services/ingestion/src/main.py`
- Modify: `services/ingestion/src/graph/queries/knows.py`
- Test: add or modify `services/ingestion/tests/test_pipeline_knows.py`

- [ ] Add a scanner for conversation SourceRecords whose raw payload contains `primary_source_record_id` and a relationship label/reference.
- [ ] Resolve both SourceRecords to Persons using existing source-record resolution queries.
- [ ] Create pending `KNOWS` edges with `status='pending'`, `approved_at=None`, source system from the chat record, and the secondary person's SourceRecord as provenance.
- [ ] Skip same-person links and incomplete relationship evidence.
- [ ] Call this materializer after the main ingestion run, alongside existing Fundbox contact materialization.
- [ ] Test that a secondary chat record with `relationship_to_primary='brother'` creates a pending family/social KNOWS edge after both SourceRecords resolve.

Run:
```bash
uv run pytest services/ingestion/tests/test_pipeline_knows.py -v
```
Expected: KNOWS materialization tests pass.

### Task 6: Full verification

**Files:**
- No source edits unless verification exposes failures.

Run:
```bash
uv run pytest services/ingestion/tests/test_chat_extraction_batch.py services/ingestion/tests/test_whatsapp_connector.py services/ingestion/tests/test_bitrix_connector.py services/ingestion/tests/test_pipeline_knows.py -v
uv run --package profile-unifier-ingestion ruff check services/ingestion/src services/ingestion/tests
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src
```
Expected: targeted tests, ruff, and mypy pass or only known pre-existing mypy failures are reported.

### Self-review

- Spec coverage: prompt, grouped extraction, per-person SourceRecords, same matching pipeline, pending KNOWS, and verification are covered.
- Placeholder scan: no TBD/TODO placeholders are present.
- Type consistency: helper names and payload field names are consistent across tasks.
