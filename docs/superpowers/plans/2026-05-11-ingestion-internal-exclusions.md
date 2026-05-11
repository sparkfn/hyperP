# Ingestion Internal Exclusions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Exclude internal staff, agents, merchants, admin users, and company-owned identifiers from source ingestion and profile matching.

**Architecture:** Add a shared ingestion exclusion helper for normalized phone/email/name checks, then apply source-native exclusion at connector boundaries before envelopes are emitted. Source connectors keep discovering their own staff/role context, while chat connectors filter post-LLM extraction evidence before matching can see it.

**Tech Stack:** Python 3.13, SQLAlchemy Core, Pydantic v2, uv, pytest, ruff, mypy strict.

---

## File Structure

- Create `services/ingestion/src/exclusions.py`: shared exclusion configuration, normalization, extraction filtering, and skip helpers.
- Modify `services/ingestion/src/config.py`: add env-configured company phone/email/name denylists.
- Modify `services/ingestion/src/connectors/fundbox/schema.py`: declare `roles`, `model_has_roles`, and `merchant_staff`.
- Modify `services/ingestion/src/connectors/fundbox/base.py`: add reusable excluded-user lookup for Fundbox connectors.
- Modify `services/ingestion/src/connectors/fundbox/users.py`, `legacy.py`, `merged.py`, `sales.py`: skip excluded Fundbox users.
- Modify `services/ingestion/src/connectors/eko/schema.py` and `speedzone/schema.py`: declare `phppos_employees`.
- Modify `services/ingestion/src/connectors/eko/connector.py`, `speedzone/connector.py`, and sales connectors: skip employee-backed identities and sales.
- Modify `services/ingestion/src/connectors/bitrix/connector.py`: filter extracted agent evidence.
- Modify `services/ingestion/src/connectors/whatsapp/connector.py`: filter session/company phone evidence.
- Add tests under `services/ingestion/tests/` for shared helper, Fundbox, PHP POS, Bitrix, and WhatsApp exclusion behavior.

## Task 1: Shared exclusion helper

**Files:**
- Create: `services/ingestion/src/exclusions.py`
- Test: `services/ingestion/tests/test_exclusions.py`

- [ ] **Step 1: Write failing tests**

```python
from src.exclusions import ExclusionContext, filter_extraction, is_excluded_email, is_excluded_name, is_excluded_phone


def test_phone_exclusion_normalizes_singapore_numbers() -> None:
    context = ExclusionContext(phones=frozenset({"+6568505434"}))

    assert is_excluded_phone("6850 5434", context)


def test_filter_extraction_removes_excluded_person_and_keeps_customer() -> None:
    extraction = {
        "confidence": 0.9,
        "summary": "customer spoke with agent",
        "persons": [
            {"name": "Agent One", "phone": "+6568505434", "email": None},
            {"name": "Customer One", "phone": "+6588889999", "email": "customer@example.com"},
        ],
        "transactions": [],
    }
    context = ExclusionContext(phones=frozenset({"+6568505434"}), names=frozenset({"agent one"}))

    filtered = filter_extraction(extraction, context)

    assert filtered is not None
    assert filtered["persons"] == [
        {"name": "Customer One", "phone": "+6588889999", "email": "customer@example.com"}
    ]


def test_filter_extraction_returns_none_when_all_people_excluded() -> None:
    extraction = {
        "confidence": 0.9,
        "summary": "agent only",
        "persons": [{"name": "Agent One", "phone": "+6568505434", "email": None}],
        "transactions": [],
    }
    context = ExclusionContext(phones=frozenset({"+6568505434"}), names=frozenset({"agent one"}))

    assert filter_extraction(extraction, context) is None


def test_email_and_name_checks_are_exact_after_normalization() -> None:
    context = ExclusionContext(emails=frozenset({"staff@example.com"}), names=frozenset({"staff member"}))

    assert is_excluded_email(" Staff@Example.com ", context)
    assert is_excluded_name("  Staff   Member ", context)
    assert not is_excluded_name("Staff Member Jr", context)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest services/ingestion/tests/test_exclusions.py -v`
Expected: FAIL because `src.exclusions` does not exist.

- [ ] **Step 3: Implement helper**

Create `ExclusionContext`, normalized phone/email/name functions, env parser, and `filter_extraction()` that returns `None` when no person evidence remains.

- [ ] **Step 4: Run helper tests**

Run: `uv run pytest services/ingestion/tests/test_exclusions.py -v`
Expected: PASS.

## Task 2: Fundbox user exclusion

**Files:**
- Modify: `services/ingestion/src/connectors/fundbox/schema.py`
- Modify: `services/ingestion/src/connectors/fundbox/base.py`
- Modify: `services/ingestion/src/connectors/fundbox/users.py`
- Modify: `services/ingestion/src/connectors/fundbox/legacy.py`
- Modify: `services/ingestion/src/connectors/fundbox/merged.py`
- Modify: `services/ingestion/src/connectors/fundbox/sales.py`
- Test: `services/ingestion/tests/test_fundbox_exclusions.py`

- [ ] **Step 1: Write failing tests**

Add tests that create in-memory SQLite tables for Fundbox users, roles, model_has_roles, merchant_staff, legacy profiles, merged users, and orders. Assert merchant/admin/staff user IDs emit no identity envelopes and that sales for excluded user IDs are skipped.

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest services/ingestion/tests/test_fundbox_exclusions.py -v`
Expected: FAIL because the schema/helper methods are absent and connectors do not skip users.

- [ ] **Step 3: Implement Fundbox exclusion lookup**

Declare the three tables and add a base helper returning `set[int]` for users with role `merchant`/`admin` or merchant staff rows. Filter rows before envelope creation in each Fundbox connector.

- [ ] **Step 4: Run Fundbox tests**

Run: `uv run pytest services/ingestion/tests/test_fundbox_exclusions.py -v`
Expected: PASS.

## Task 3: PHP POS employee exclusion

**Files:**
- Modify: `services/ingestion/src/connectors/eko/schema.py`
- Modify: `services/ingestion/src/connectors/speedzone/schema.py`
- Modify: `services/ingestion/src/connectors/eko/connector.py`
- Modify: `services/ingestion/src/connectors/speedzone/connector.py`
- Modify: `services/ingestion/src/connectors/eko/sales.py`
- Modify: `services/ingestion/src/connectors/speedzone/sales.py`
- Test: `services/ingestion/tests/test_phppos_exclusions.py`

- [ ] **Step 1: Write failing tests**

Add tests that assert people/customers with `person_id` in `phppos_employees.person_id` are skipped in normal and people-only paths, and sales for employee-backed customer IDs are skipped.

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest services/ingestion/tests/test_phppos_exclusions.py -v`
Expected: FAIL because employee tables are not declared and connectors do not filter employees.

- [ ] **Step 3: Implement PHP POS exclusion**

Declare `employees` tables and add connector-level employee person ID lookup. Filter identity and sales rows before envelope creation.

- [ ] **Step 4: Run PHP POS tests**

Run: `uv run pytest services/ingestion/tests/test_phppos_exclusions.py -v`
Expected: PASS.

## Task 4: Chat extraction exclusion

**Files:**
- Modify: `services/ingestion/src/connectors/bitrix/connector.py`
- Modify: `services/ingestion/src/connectors/whatsapp/connector.py`
- Test: `services/ingestion/tests/test_chat_exclusions.py`

- [ ] **Step 1: Write failing tests**

Add tests that call connector envelope builders with extraction payloads containing one agent/session person and one customer person. Assert excluded people are removed and all-excluded extractions emit no envelope.

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest services/ingestion/tests/test_chat_exclusions.py -v`
Expected: FAIL because chat builders currently pass extracted identifiers through unchanged.

- [ ] **Step 3: Implement chat filters**

Use `filter_extraction()` with Bitrix agent names/IDs and WhatsApp session/company phones before building identifiers or attributes. Return `None`/skip when no person evidence remains.

- [ ] **Step 4: Run chat tests**

Run: `uv run pytest services/ingestion/tests/test_chat_exclusions.py -v`
Expected: PASS.

## Task 5: Verification

**Files:**
- Verify changed Python files and ingestion tests.

- [ ] **Step 1: Run focused tests**

Run: `uv run pytest services/ingestion/tests/test_exclusions.py services/ingestion/tests/test_fundbox_exclusions.py services/ingestion/tests/test_phppos_exclusions.py services/ingestion/tests/test_chat_exclusions.py -v`
Expected: PASS.

- [ ] **Step 2: Run ingestion test suite**

Run: `uv run pytest services/ingestion/tests -v`
Expected: PASS.

- [ ] **Step 3: Run lint**

Run: `uv run --package profile-unifier-ingestion ruff check services/ingestion/src services/ingestion/tests`
Expected: PASS.

- [ ] **Step 4: Run type check**

Run: `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src`
Expected: PASS or only documented pre-existing failures outside changed files.

## Self-Review

- Spec coverage: Fundbox roles/staff, PHP POS employees, Bitrix agents, WhatsApp company phones, shared helper, observability, and verification are covered.
- Placeholder scan: no TBD/TODO/future placeholders.
- Type consistency: helper types and connector responsibilities are consistent across tasks.
