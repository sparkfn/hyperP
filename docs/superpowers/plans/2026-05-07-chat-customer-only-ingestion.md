# Chat Customer-Only Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent chat ingestion from normalizing sales-agent identity facts into customer profiles while preserving full conversation summaries, transactions, and raw agent metadata.

**Architecture:** Tighten the shared LLM extraction prompt so `persons` means customer/client/prospect identity only. Keep connector data flow unchanged because identifiers and `full_name` already derive from `extraction["persons"]`; add regression tests for the prompt contract and downstream connector behavior.

**Tech Stack:** Python 3, pytest, uv workspace, existing chat connectors under `services/ingestion/src/connectors`, shared LLM prompt module `services/ingestion/src/llm_prompts.py`.

---

## File Structure

- Modify `services/ingestion/src/llm_prompts.py`: update `EXTRACTION_TEMPLATE` to define customer-only `persons`, exclude agents/staff/internal users, and keep `transactions`/`summary` conversation-wide.
- Modify `services/ingestion/tests/test_chat_extraction_batch.py`: add a prompt-contract regression test that checks the generated prompt text.
- Modify `services/ingestion/tests/test_bitrix_connector.py`: add a connector regression test that proves agent metadata is preserved raw while normalized identity remains customer-only.
- Modify `services/ingestion/tests/test_whatsapp_connector.py`: add a connector regression test that proves WhatsApp participant/endpoint metadata is preserved raw while normalized identity remains customer-only.

## Task 1: Prompt Contract

**Files:**
- Modify: `services/ingestion/tests/test_chat_extraction_batch.py`
- Modify: `services/ingestion/src/llm_prompts.py`

- [ ] **Step 1: Write the failing prompt-contract test**

Add this import and test to `services/ingestion/tests/test_chat_extraction_batch.py`:

```python
from src.llm_prompts import build_extraction_prompt


def test_chat_extraction_prompt_keeps_persons_customer_only() -> None:
    prompt = build_extraction_prompt(
        "[2026-05-07 10:00:00] Tonni: Customer Ada ordered item A\n"
        "[2026-05-07 10:01:00] Ada: My phone is +6512345678"
    )

    assert "customers, clients, prospects" in prompt
    assert "Do not include sales agents" in prompt
    assert "staff" in prompt
    assert "internal users" in prompt
    assert "tenant or business representatives" in prompt
    assert "message senders acting on behalf of the business" in prompt
    assert "transactions" in prompt
    assert "summary" in prompt
    assert "full conversation" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest services/ingestion/tests/test_chat_extraction_batch.py::test_chat_extraction_prompt_keeps_persons_customer_only -v
```

Expected: FAIL because the current prompt does not contain the customer-only and agent-exclusion wording.

- [ ] **Step 3: Update the extraction prompt**

Replace `EXTRACTION_TEMPLATE` in `services/ingestion/src/llm_prompts.py` with:

```python
EXTRACTION_TEMPLATE = """\
Extract customer identity and transaction information from the following conversation.
Return a JSON object with these top-level keys:
- "persons": array of customers, clients, prospects, or other external people whose
  identity should be attached to the customer profile. Do not include sales agents,
  staff, internal users, tenant or business representatives, or message senders
  acting on behalf of the business. Each person has:
    - "name": full name if stated
    - "phone": phone number if stated (Singapore format like +65 or 8-digit local)
    - "email": email address if stated
    - "address": full address if stated
    - "nric": NRIC/FIN number if stated
    - "notes": any other relevant context about this customer
- "transactions": array of orders/invoices mentioned anywhere in the full conversation.
  Include order details stated by customers or business representatives. Each has:
    - "order_id": order/invoice reference number if stated
    - "product": product name or description if stated
    - "amount": numerical amount if stated
    - "currency": currency code (default SGD)
    - "status": status mentioned (e.g. pending, paid, completed, cancelled)
    - "notes": any other relevant context
- "summary": concise factual summary of the full conversation, including customer intent,
  products/orders discussed, agent-provided order details, and any follow-up state
- "confidence": your overall confidence (0.0-1.0) in this extraction

Conversation (newest messages last):

{messages}

Return only valid JSON.\
"""
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
uv run pytest services/ingestion/tests/test_chat_extraction_batch.py::test_chat_extraction_prompt_keeps_persons_customer_only -v
```

Expected: PASS.

## Task 2: Bitrix Connector Regression

**Files:**
- Modify: `services/ingestion/tests/test_bitrix_connector.py`

- [ ] **Step 1: Write the failing Bitrix connector regression test**

Add this test to `services/ingestion/tests/test_bitrix_connector.py`:

```python
def test_bitrix_chat_envelope_keeps_agent_identity_raw_only() -> None:
    connector = BitrixChatConnector()
    bundle = connector_module._ChatBundle(
        chat_id=1,
        deal_id=101,
        bitrix_chat_id="chat-1",
        entity="fundbox",
        category_name="Fundbox",
        deal={"title": "Ada order", "stage_id": "NEW", "opened": True, "closed": False},
        conv_text="Tonni: Ada ordered product A. Ada: My phone is +6512345678.",
        agents=[{"name": "Tonni", "phone": "+6599990000", "email": "tonni@example.com"}],
        last_message_at=datetime(2026, 5, 7, 10, 1, 0),
        created_at=datetime(2026, 5, 7, 10, 0, 0),
    )
    extraction = {
        "persons": [
            {"name": "Ada Customer", "phone": "+6512345678", "email": "ada@example.com"}
        ],
        "transactions": [
            {
                "order_id": "ORD-1",
                "product": "product A",
                "amount": 25.0,
                "currency": "SGD",
                "status": "pending",
                "notes": "Tonni confirmed the order details",
            }
        ],
        "summary": "Ada ordered product A; Tonni confirmed follow-up state.",
        "confidence": 0.95,
    }

    record = connector._build_envelope(bundle=bundle, extraction=extraction)

    assert record["attributes"]["full_name"] == "Ada Customer"
    assert {item["value"] for item in record["identifiers"]} == {
        "+6512345678",
        "ada@example.com",
    }
    assert "+6599990000" not in {item["value"] for item in record["identifiers"]}
    assert record["raw_payload"]["chat_members"] == [
        {"name": "Tonni", "phone": "+6599990000", "email": "tonni@example.com"}
    ]
    assert record["raw_payload"]["transactions"][0]["notes"] == "Tonni confirmed the order details"
    assert record["raw_payload"]["summary"] == "Ada ordered product A; Tonni confirmed follow-up state."
```

Also add this import near the existing Bitrix import:

```python
from src.connectors.bitrix import connector as connector_module
```

- [ ] **Step 2: Run test to verify current behavior**

Run:

```bash
uv run pytest services/ingestion/tests/test_bitrix_connector.py::test_bitrix_chat_envelope_keeps_agent_identity_raw_only -v
```

Expected: PASS if connector behavior already satisfies the customer-only contract when extraction output is customer-only. If it fails, fix only the connector code needed to keep normalized identity derived from `persons` and raw agent metadata preserved.

## Task 3: WhatsApp Connector Regression

**Files:**
- Modify: `services/ingestion/tests/test_whatsapp_connector.py`

- [ ] **Step 1: Write the WhatsApp connector regression test**

Add this import near the existing WhatsApp import:

```python
from src.connectors.whatsapp import connector as whatsapp_module
```

Add this test to `services/ingestion/tests/test_whatsapp_connector.py`:

```python
def test_whatsapp_chat_envelope_keeps_agent_identity_raw_only() -> None:
    bundle = whatsapp_module._ChatBundle(
        chat_id="6599990000@c.us",
        chat_name="Tonni",
        whatsapp_user_id="session-1",
        session_id=1,
        tenant="fundbox",
        msg_text="[2026-05-07 10:00:00] Tonni: Ada ordered item A\n"
        "[2026-05-07 10:01:00] Ada: My phone is +6512345678",
        observed_at="2026-05-07T10:01:00",
        participants=[
            whatsapp_module._Participant(
                jid="6599990000@c.us",
                phone="+6599990000",
                name="Tonni",
                role="member",
            ),
            whatsapp_module._Participant(
                jid="6512345678@c.us",
                phone="+6512345678",
                name="Ada Customer",
                role="chat",
            ),
        ],
        message_endpoints=[
            {"role": "sender", "jid": "6599990000@c.us", "phone": "+6599990000"},
            {"role": "recipient", "jid": "6512345678@c.us", "phone": "+6512345678"},
        ],
    )
    extraction = {
        "persons": [
            {"name": "Ada Customer", "phone": "+6512345678", "email": "ada@example.com"}
        ],
        "transactions": [
            {
                "order_id": "ORD-1",
                "product": "item A",
                "amount": 25.0,
                "currency": "SGD",
                "status": "pending",
                "notes": "Tonni confirmed the order details",
            }
        ],
        "summary": "Ada ordered item A; Tonni confirmed follow-up state.",
        "confidence": 0.95,
    }

    record = whatsapp_module._build_envelope(bundle=bundle, extraction=extraction)

    assert record["attributes"]["full_name"] == "Ada Customer"
    assert {item["value"] for item in record["identifiers"]} == {
        "+6512345678",
        "ada@example.com",
    }
    assert "+6599990000" not in {item["value"] for item in record["identifiers"]}
    assert record["raw_payload"]["participants"][0] == {
        "jid": "6599990000@c.us",
        "phone": "+6599990000",
        "name": "Tonni",
        "role": "member",
    }
    assert record["raw_payload"]["transactions"][0]["notes"] == "Tonni confirmed the order details"
    assert record["raw_payload"]["summary"] == "Ada ordered item A; Tonni confirmed follow-up state."
```

- [ ] **Step 2: Run test to verify current behavior**

Run:

```bash
uv run pytest services/ingestion/tests/test_whatsapp_connector.py::test_whatsapp_chat_envelope_keeps_agent_identity_raw_only -v
```

Expected: PASS if connector behavior already satisfies the customer-only contract when extraction output is customer-only. If it fails because WhatsApp appends a `chat` participant phone that is not already in extracted customer identifiers, fix `_build_envelope` so it appends the chat participant phone only when it belongs to the extracted customer or remove that fallback for conversation records.

## Task 4: Focused Verification

**Files:**
- Test: `services/ingestion/tests/test_chat_extraction_batch.py`
- Test: `services/ingestion/tests/test_bitrix_connector.py`
- Test: `services/ingestion/tests/test_whatsapp_connector.py`

- [ ] **Step 1: Run focused ingestion tests**

Run:

```bash
uv run pytest services/ingestion/tests/test_chat_extraction_batch.py services/ingestion/tests/test_bitrix_connector.py services/ingestion/tests/test_whatsapp_connector.py -v
```

Expected: PASS for all tests in the three files.

- [ ] **Step 2: Run ingestion lint**

Run:

```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src services/ingestion/tests/test_chat_extraction_batch.py services/ingestion/tests/test_bitrix_connector.py services/ingestion/tests/test_whatsapp_connector.py
```

Expected: PASS with no new lint errors.

- [ ] **Step 3: Run ingestion type check**

Run:

```bash
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src
```

Expected: PASS or only documented pre-existing failures unrelated to this change.

## Self-Review Notes

- Spec coverage: prompt contract is Task 1; customer-only normalized identity is covered by Tasks 2 and 3; preserving raw agent metadata is covered by Tasks 2 and 3; summary/transactions conversation-wide behavior is covered by Tasks 1, 2, and 3.
- Placeholder scan: no placeholders are present.
- Type consistency: tests use existing connector module names, existing `ExtractionResult` shape, existing record keys `attributes`, `identifiers`, and `raw_payload`.
- Commit handling: no commit steps are included because the project memory says never commit without explicit user instruction.
