# Chat Ingestion Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve chat LLM extraction with sectioned summaries and split strong/weak identifiers while preserving the existing payload shape.

**Architecture:** Extend the prompt, typed extraction parser, and connector raw payloads compatibly. Strong phone/email identifiers are normalized and de-duplicated through the existing identifier flow; weak identifiers remain raw/review evidence and are not promoted to identity keys.

**Tech Stack:** Python 3.12, TypedDict, Pydantic JsonValue, uv, pytest, ruff, mypy strict.

---

## File structure

- Modify `services/ingestion/src/llm_prompts.py` — prompt includes sectioned summary and strong/weak identifier instructions.
- Modify `services/ingestion/src/connectors/chat_helpers.py` — typed `ExtractedStrongIdentifier`, `ExtractedWeakIdentifier`, parser support, payload helpers, and de-duplicated identifier promotion.
- Modify `services/ingestion/src/connectors/bitrix/connector.py` — persist new identifier arrays in raw payload.
- Modify `services/ingestion/src/connectors/whatsapp/connector.py` — persist new identifier arrays in raw payload.
- Modify `services/ingestion/tests/test_chat_extraction_batch.py` — parser and prompt tests.
- Modify `services/ingestion/tests/test_bitrix_connector.py` and `services/ingestion/tests/test_whatsapp_connector.py` — raw payload persistence tests.

### Task 1: Prompt requirements for sectioned summaries and identifier classes

**Files:**
- Modify: `services/ingestion/src/llm_prompts.py`
- Test: `services/ingestion/tests/test_chat_extraction_batch.py`

- [ ] **Step 1: Add failing prompt assertions**

Add to `test_chat_extraction_prompt_keeps_persons_customer_only`:

```python
    assert "Customer / Participants" in prompt
    assert "Identity Evidence" in prompt
    assert "Products / Machine Units" in prompt
    assert "Orders / Commercial Terms" in prompt
    assert "Timeline / Follow-ups" in prompt
    assert "Uncertainties" in prompt
    assert "strong_identifiers" in prompt
    assert "weak_identifiers" in prompt
    assert "weak identifiers are evidence" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_chat_extraction_batch.py::test_chat_extraction_prompt_keeps_persons_customer_only -v`

Expected: FAIL because the prompt lacks new instructions.

- [ ] **Step 3: Update prompt**

In `services/ingestion/src/llm_prompts.py`, replace the summary and identity portions of `EXTRACTION_TEMPLATE` with text that includes:

```text
- "strong_identifiers": array of explicitly stated customer identity identifiers. Each has:
    - "type": "phone" | "email" | "government_id" | "source_customer_ref"
    - "value": exact extracted value
    - "label": optional source label such as nric, fin, customer_id
    - "person_name": associated customer name if stated
    - "confidence": confidence for this identifier from 0.0 to 1.0
    - "notes": short evidence context
- "weak_identifiers": array of contextual identifiers. Weak identifiers are evidence, not identity keys. Each has:
    - "type": "name" | "address" | "dob" | "machine_lta_tag" | "machine_serial_number" | "machine_unit" | "product" | "order_ref" | "relationship" | "other"
    - "value": exact extracted value
    - "label": optional source label
    - "person_name": associated customer name if stated
    - "confidence": confidence for this value from 0.0 to 1.0
    - "notes": short evidence context
- "summary": thorough sectioned factual summary of the full conversation. Use these headings when evidence exists: Customer / Participants, Identity Evidence, Products / Machine Units, Orders / Commercial Terms, Timeline / Follow-ups, Uncertainties.
```

Keep the existing instructions to exclude agents/staff from customer identifiers and to return only valid JSON.

- [ ] **Step 4: Run prompt test**

Run: `uv run pytest services/ingestion/tests/test_chat_extraction_batch.py::test_chat_extraction_prompt_keeps_persons_customer_only -v`

Expected: PASS.

### Task 2: Typed parser support for strong and weak identifiers

**Files:**
- Modify: `services/ingestion/src/connectors/chat_helpers.py`
- Test: `services/ingestion/tests/test_chat_extraction_batch.py`

- [ ] **Step 1: Add failing parser assertions**

Modify `_FakeLlmService.chat_json` to include:

```python
                "summary": "Customer / Participants:\nAda asked about Bike A.",
                "strong_identifiers": [
                    {"type": "phone", "value": "+6512345678", "confidence": 0.95, "notes": "Customer stated phone"}
                ],
                "weak_identifiers": [
                    {"type": "machine_lta_tag", "value": "LTA123", "confidence": 0.7, "notes": "Asked about unit"}
                ],
```

Add assertions:

```python
    assert results[0]["summary"].startswith("Customer / Participants")
    assert results[0]["strong_identifiers"][0]["type"] == "phone"
    assert results[0]["weak_identifiers"][0]["type"] == "machine_lta_tag"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_chat_extraction_batch.py::test_chat_extraction_batch_runs_async_gather_inside_event_loop -v`

Expected: FAIL because extraction result lacks new fields.

- [ ] **Step 3: Add TypedDicts and parser helpers**

In `chat_helpers.py`, add:

```python
class ExtractedStrongIdentifier(TypedDict, total=False):
    type: str
    value: str | None
    label: str | None
    person_name: str | None
    confidence: float | None
    notes: str | None


class ExtractedWeakIdentifier(TypedDict, total=False):
    type: str
    value: str | None
    label: str | None
    person_name: str | None
    confidence: float | None
    notes: str | None
```

Extend `ExtractionResult`:

```python
    strong_identifiers: list[ExtractedStrongIdentifier]
    weak_identifiers: list[ExtractedWeakIdentifier]
```

Add parser helper:

```python
def _parse_identifier(raw: object) -> ExtractedStrongIdentifier | ExtractedWeakIdentifier | None:
    if not isinstance(raw, dict):
        return None
    type_value = _optional_str(raw.get("type"))
    value = _optional_str(raw.get("value"))
    if type_value is None or value is None:
        return None
    confidence_raw = raw.get("confidence")
    confidence = float(confidence_raw) if isinstance(confidence_raw, int | float) else None
    return {
        "type": type_value,
        "value": value,
        "label": _optional_str(raw.get("label")),
        "person_name": _optional_str(raw.get("person_name")),
        "confidence": confidence,
        "notes": _optional_str(raw.get("notes")),
    }
```

In `run_extraction_batch`, parse arrays:

```python
            strong_raw = parsed.get("strong_identifiers")
            weak_raw = parsed.get("weak_identifiers")
            strong_identifiers = [item for item in (_parse_identifier(raw) for raw in strong_raw) if item is not None] if isinstance(strong_raw, list) else []
            weak_identifiers = [item for item in (_parse_identifier(raw) for raw in weak_raw) if item is not None] if isinstance(weak_raw, list) else []
```

Add them to `ExtractionResult(...)`.

- [ ] **Step 4: Run parser test**

Run: `uv run pytest services/ingestion/tests/test_chat_extraction_batch.py::test_chat_extraction_batch_runs_async_gather_inside_event_loop -v`

Expected: PASS.

### Task 3: Strong identifier promotion and de-duplication

**Files:**
- Modify: `services/ingestion/src/connectors/chat_helpers.py`
- Test: `services/ingestion/tests/test_chat_extraction_batch.py`

- [ ] **Step 1: Add failing identifier promotion test**

```python
def test_identifiers_from_extraction_deduplicates_legacy_and_strong_identifiers() -> None:
    from src.connectors.chat_helpers import ExtractionResult, identifiers_from_extraction

    extraction = ExtractionResult(
        persons=[{"name": "Ada", "phone": "+65 1234 5678", "email": "ada@example.com"}],
        transactions=[],
        chat_members=[],
        inquiries=[],
        strong_identifiers=[
            {"type": "phone", "value": "+6512345678", "confidence": 0.95},
            {"type": "email", "value": "ADA@example.com", "confidence": 0.95},
            {"type": "government_id", "value": "S1234567A", "confidence": 0.95},
        ],
        weak_identifiers=[{"type": "name", "value": "Ada", "confidence": 0.8}],
        summary="Customer / Participants:\nAda.",
        customer_sentiment="neutral",
        confidence=0.9,
    )

    identifiers = identifiers_from_extraction(extraction)

    assert identifiers.count({"identifier_type": "phone", "normalized_value": "+6512345678", "is_verified": False}) == 1
    assert any(item["identifier_type"] == "email" for item in identifiers)
    assert not any(item["identifier_type"] == "government_id" for item in identifiers)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_chat_extraction_batch.py::test_identifiers_from_extraction_deduplicates_legacy_and_strong_identifiers -v`

Expected: FAIL until strong identifier parsing/promotion is implemented.

- [ ] **Step 3: Extend `identifiers_from_extraction`**

Update `identifiers_from_extraction` to iterate over `extraction.get("strong_identifiers", [])` and promote only valid `phone` and `email` values using existing normalizers and `IdentifierBag` de-duplication:

```python
    for identifier in extraction.get("strong_identifiers", []):
        identifier_type = identifier.get("type")
        value = identifier.get("value")
        if not value:
            continue
        if identifier_type == "phone":
            normalized_phone, phone_quality = normalize_phone(value)
            if normalized_phone is not None and phone_quality == QualityFlag.VALID:
                identifiers.add("phone", normalized_phone, verified=False)
        elif identifier_type == "email":
            normalized_email, email_quality = normalize_email(value)
            if normalized_email is not None and email_quality == QualityFlag.VALID:
                identifiers.add("email", normalized_email, verified=False)
```

Do not promote government IDs in this task unless a chat-safe hash normalizer already exists.

- [ ] **Step 4: Run promotion test**

Run: `uv run pytest services/ingestion/tests/test_chat_extraction_batch.py::test_identifiers_from_extraction_deduplicates_legacy_and_strong_identifiers -v`

Expected: PASS.

### Task 4: Payload helpers for new identifier arrays

**Files:**
- Modify: `services/ingestion/src/connectors/chat_helpers.py`
- Modify: `services/ingestion/src/connectors/bitrix/connector.py`
- Modify: `services/ingestion/src/connectors/whatsapp/connector.py`
- Test: `services/ingestion/tests/test_bitrix_connector.py`
- Test: `services/ingestion/tests/test_whatsapp_connector.py`

- [ ] **Step 1: Add helper functions**

Add to `chat_helpers.py`:

```python
def strong_identifiers_payload(extraction: ExtractionResult) -> list[JsonValue]:
    return [dict(item) for item in extraction.get("strong_identifiers", [])]


def weak_identifiers_payload(extraction: ExtractionResult) -> list[JsonValue]:
    return [dict(item) for item in extraction.get("weak_identifiers", [])]
```

- [ ] **Step 2: Persist payloads in connectors**

In both Bitrix and WhatsApp connector imports, add:

```python
    strong_identifiers_payload,
    weak_identifiers_payload,
```

In each raw payload, add:

```python
            "strong_identifiers": strong_identifiers_payload(extraction),
            "weak_identifiers": weak_identifiers_payload(extraction),
```

- [ ] **Step 3: Add connector assertions**

In existing Bitrix and WhatsApp connector tests that assert raw payload summary/inquiries, add expected fake extraction values and assertions:

```python
    assert record["raw_payload"]["strong_identifiers"] == [
        {"type": "phone", "value": "+6512345678", "confidence": 0.95, "label": None, "person_name": None, "notes": "Customer stated phone"}
    ]
    assert record["raw_payload"]["weak_identifiers"][0]["type"] == "machine_lta_tag"
```

- [ ] **Step 4: Run connector tests**

Run: `uv run pytest services/ingestion/tests/test_bitrix_connector.py services/ingestion/tests/test_whatsapp_connector.py -v`

Expected: PASS.

### Task 5: Malformed identifier entries are ignored

**Files:**
- Modify: `services/ingestion/tests/test_chat_extraction_batch.py`

- [ ] **Step 1: Add parser robustness test**

```python
def test_chat_extraction_ignores_malformed_identifier_entries(monkeypatch: MonkeyPatch) -> None:
    from src.connectors import chat_helpers

    class BadIdentifierLlm:
        async def chat_json(self, messages: Sequence[ChatMessage]) -> str:
            _ = messages
            return json.dumps(
                {
                    "persons": [],
                    "transactions": [],
                    "chat_members": [],
                    "inquiries": [],
                    "strong_identifiers": [{"type": "phone"}, "bad"],
                    "weak_identifiers": [{"value": "Ada"}, 123],
                    "summary": "Uncertainties:\nNo usable identifiers.",
                    "confidence": 0.5,
                }
            )

    monkeypatch.setattr(chat_helpers, "get_llm_service", lambda: BadIdentifierLlm())
    monkeypatch.setattr(chat_helpers, "get_settings", lambda: _FakeSettings())

    results = chat_helpers.run_extraction_batch(["hello"])

    assert results[0] is not None
    assert results[0]["strong_identifiers"] == []
    assert results[0]["weak_identifiers"] == []
```

- [ ] **Step 2: Run robustness test**

Run: `uv run pytest services/ingestion/tests/test_chat_extraction_batch.py::test_chat_extraction_ignores_malformed_identifier_entries -v`

Expected: PASS.

### Task 6: Chat extraction verification

**Files:**
- None

- [ ] **Step 1: Run focused chat tests**

Run: `uv run pytest services/ingestion/tests/test_chat_extraction_batch.py services/ingestion/tests/test_chat_summary_payload.py services/ingestion/tests/test_bitrix_connector.py services/ingestion/tests/test_whatsapp_connector.py -v`

Expected: PASS.

- [ ] **Step 2: Run ingestion lint**

Run: `uv run --package profile-unifier-ingestion ruff check services/ingestion/src services/ingestion/tests`

Expected: PASS.

- [ ] **Step 3: Run ingestion type check**

Run: `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src`

Expected: PASS or only documented pre-existing failures outside changed files.

## Self-review

- Spec coverage: sectioned summary, strong/weak identifiers, compatible parser, de-duplication, raw payload persistence, and tests are covered.
- Placeholder scan: no placeholder tasks remain.
- Type consistency: `strong_identifiers`, `weak_identifiers`, and payload helper names are consistent across tasks.
