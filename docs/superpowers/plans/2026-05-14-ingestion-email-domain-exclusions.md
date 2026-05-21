# Ingestion Email Domain Exclusions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend ingestion hard exclusions so configured email domains and their subdomains are excluded, and add the requested phone/domain values to the local exclusion config.

**Architecture:** The existing JSON-backed exclusion config remains the single editable source for hard-exclusion values. `ExclusionFile` will gain an `email_domains` field, `ExclusionContext` will store normalized domains, and `is_excluded_email()` will check both exact configured emails and domain/subdomain matches after email normalization.

**Tech Stack:** Python 3.12, dataclasses, Pydantic settings, pytest, uv, JSON config.

---

## File Structure

- Modify `services/ingestion/src/exclusion_config.py`
  - Add `email_domains` to the JSON-backed dataclass and loader.
  - Reuse the existing `_str_list()` validation helper so malformed arrays fail consistently.
- Modify `services/ingestion/src/exclusions.py`
  - Add normalized domain storage to `ExclusionContext`.
  - Add small domain normalization and matching helpers.
  - Extend `build_exclusion_context()` and `is_excluded_email()`.
- Modify `services/ingestion/tests/test_exclusion_config.py`
  - Prove the loader accepts `email_domains` and still defaults to an empty list.
- Modify `services/ingestion/tests/test_exclusions.py`
  - Prove exact domain and subdomain email exclusion works.
  - Prove partial suffixes like `notada.asia` do not match `ada.asia`.
- Modify `config/ingestion-exclusions.example.json`
  - Add an empty `email_domains` array so the schema is visible.
- Modify `config/ingestion-exclusions.local.json`
  - Add requested phone exclusions.
  - Add requested email domain exclusions.

---

### Task 1: Add Failing Config Loader Tests

**Files:**
- Test: `services/ingestion/tests/test_exclusion_config.py`

- [ ] **Step 1: Update the loader test to expect email domains**

Replace the JSON payload and assertions in `test_load_exclusion_file_returns_arrays()` with:

```python
def test_load_exclusion_file_returns_arrays(tmp_path: Path) -> None:
    path = tmp_path / "exclusions.json"
    path.write_text(
        "{"
        '"phones":["+6512345678"],'
        '"emails":["ops@example.com"],'
        '"email_domains":["ada.asia"],'
        '"names":["Ada Ops"],'
        '"source_ids":["staff-1"]'
        "}",
        encoding="utf-8",
    )

    loaded = load_exclusion_file(str(path))

    assert loaded.phones == ["+6512345678"]
    assert loaded.emails == ["ops@example.com"]
    assert loaded.email_domains == ["ada.asia"]
    assert loaded.names == ["Ada Ops"]
    assert loaded.source_ids == ["staff-1"]
```

- [ ] **Step 2: Update the blank-path default test**

Replace `test_load_exclusion_file_blank_path_returns_empty()` with:

```python
def test_load_exclusion_file_blank_path_returns_empty() -> None:
    loaded = load_exclusion_file("")

    assert loaded.phones == []
    assert loaded.emails == []
    assert loaded.email_domains == []
    assert loaded.names == []
    assert loaded.source_ids == []
```

- [ ] **Step 3: Run the targeted config test and verify it fails**

Run:

```bash
uv run pytest services/ingestion/tests/test_exclusion_config.py -q
```

Expected: FAIL with an error like:

```text
AttributeError: 'ExclusionFile' object has no attribute 'email_domains'
```

---

### Task 2: Implement Config Loader Support

**Files:**
- Modify: `services/ingestion/src/exclusion_config.py:13-54`
- Test: `services/ingestion/tests/test_exclusion_config.py`

- [ ] **Step 1: Add the dataclass field**

Change `ExclusionFile` to:

```python
@dataclass
class ExclusionFile:
    """Editable hard-exclusion values loaded from JSON."""

    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    email_domains: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
```

- [ ] **Step 2: Load `email_domains` from JSON**

Change the `return ExclusionFile(...)` block in `load_exclusion_file()` to:

```python
    return ExclusionFile(
        phones=_str_list(payload.get("phones"), path=path),
        emails=_str_list(payload.get("emails"), path=path),
        email_domains=_str_list(payload.get("email_domains"), path=path),
        names=_str_list(payload.get("names"), path=path),
        source_ids=_str_list(payload.get("source_ids"), path=path),
    )
```

- [ ] **Step 3: Run the targeted config test and verify it passes**

Run:

```bash
uv run pytest services/ingestion/tests/test_exclusion_config.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit only if explicit commit authorization has been granted**

If the user has explicitly asked for commits in this execution session, run:

```bash
git add services/ingestion/src/exclusion_config.py services/ingestion/tests/test_exclusion_config.py
git commit -m "Add ingestion email domain exclusion config"
```

If no explicit commit authorization has been granted, do not commit.

---

### Task 3: Add Failing Domain Matching Tests

**Files:**
- Test: `services/ingestion/tests/test_exclusions.py`

- [ ] **Step 1: Update the context merge test**

In `test_build_exclusion_context_merges_env_and_file_values()`, replace the `ExclusionFile(...)` argument with:

```python
        file_exclusions=ExclusionFile(
            phones=["+6582222222"],
            emails=["file@example.com"],
            email_domains=["Ada.Asia"],
            names=["File Person"],
            source_ids=["staff-1"],
        ),
```

Then add this assertion after the existing email assertions:

```python
    assert "ada.asia" in context.email_domains
```

- [ ] **Step 2: Add exact-domain and subdomain tests**

Append this test to `services/ingestion/tests/test_exclusions.py`:

```python
def test_email_domain_exclusion_matches_domain_and_subdomains() -> None:
    context = ExclusionContext(email_domains=frozenset({"ada.asia"}))

    assert is_excluded_email("staff@ada.asia", context)
    assert is_excluded_email("staff@mail.ada.asia", context)
    assert not is_excluded_email("staff@notada.asia", context)
```

- [ ] **Step 3: Add normalization boundary test**

Append this test to `services/ingestion/tests/test_exclusions.py`:

```python
def test_email_domain_exclusion_normalizes_configured_domains() -> None:
    context = build_exclusion_context(
        company_mobile_numbers=[],
        company_email_addresses=[],
        internal_person_names=[],
        file_exclusions=ExclusionFile(email_domains=["@SpeedZone.Asia", " mail.ADA.Asia "]),
    )

    assert is_excluded_email("person@speedzone.asia", context)
    assert is_excluded_email("person@x.mail.ada.asia", context)
```

- [ ] **Step 4: Run the targeted exclusions test and verify it fails**

Run:

```bash
uv run pytest services/ingestion/tests/test_exclusions.py -q
```

Expected: FAIL with an error like:

```text
TypeError: ExclusionContext.__init__() got an unexpected keyword argument 'email_domains'
```

or:

```text
AttributeError: 'ExclusionContext' object has no attribute 'email_domains'
```

---

### Task 4: Implement Domain Matching

**Files:**
- Modify: `services/ingestion/src/exclusions.py:14-82`
- Test: `services/ingestion/tests/test_exclusions.py`

- [ ] **Step 1: Add domain storage to `ExclusionContext`**

Change `ExclusionContext` to:

```python
@dataclass(frozen=True)
class ExclusionContext:
    """Normalized identifiers that must not enter profile matching."""

    phones: frozenset[str] = field(default_factory=frozenset)
    emails: frozenset[str] = field(default_factory=frozenset)
    email_domains: frozenset[str] = field(default_factory=frozenset)
    names: frozenset[str] = field(default_factory=frozenset)
    source_ids: frozenset[str] = field(default_factory=frozenset)
```

- [ ] **Step 2: Add domain normalization helpers**

Insert these helpers after `normalize_excluded_email()`:

```python
def normalize_excluded_email_domain(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower().removeprefix("@")
    if not normalized or "@" in normalized:
        return None
    return normalized.rstrip(".")


def email_matches_domain(email: str, domain: str) -> bool:
    email_domain = email.rsplit("@", maxsplit=1)[1]
    return email_domain == domain or email_domain.endswith(f".{domain}")
```

- [ ] **Step 3: Add a normalized domain set helper**

Insert this helper after `normalized_email_set()`:

```python
def normalized_email_domain_set(values: list[str]) -> frozenset[str]:
    return frozenset(
        v for value in values if (v := normalize_excluded_email_domain(value)) is not None
    )
```

- [ ] **Step 4: Include configured domains in `build_exclusion_context()`**

Change the `ExclusionContext(...)` construction in `build_exclusion_context()` to:

```python
    return ExclusionContext(
        phones=normalized_phone_set(company_mobile_numbers + file_exclusions.phones),
        emails=normalized_email_set(company_email_addresses + file_exclusions.emails),
        email_domains=normalized_email_domain_set(file_exclusions.email_domains),
        names=normalized_name_set(internal_person_names + file_exclusions.names),
        source_ids=frozenset(
            value.strip().lower() for value in file_exclusions.source_ids if value.strip()
        ),
    )
```

- [ ] **Step 5: Extend `is_excluded_email()`**

Replace `is_excluded_email()` with:

```python
def is_excluded_email(value: str | None, context: ExclusionContext) -> bool:
    normalized = normalize_excluded_email(value)
    if normalized is None:
        return False
    if normalized in context.emails:
        return True
    return any(email_matches_domain(normalized, domain) for domain in context.email_domains)
```

- [ ] **Step 6: Run the targeted exclusions test and verify it passes**

Run:

```bash
uv run pytest services/ingestion/tests/test_exclusions.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit only if explicit commit authorization has been granted**

If the user has explicitly asked for commits in this execution session, run:

```bash
git add services/ingestion/src/exclusions.py services/ingestion/tests/test_exclusions.py
git commit -m "Exclude ingestion emails by configured domain"
```

If no explicit commit authorization has been granted, do not commit.

---

### Task 5: Update Exclusion JSON Config Files

**Files:**
- Modify: `config/ingestion-exclusions.example.json`
- Modify: `config/ingestion-exclusions.local.json`

- [ ] **Step 1: Update the example schema**

Replace `config/ingestion-exclusions.example.json` with:

```json
{
  "phones": [],
  "emails": [],
  "email_domains": [],
  "names": [],
  "source_ids": []
}
```

- [ ] **Step 2: Update the local exclusions**

Replace `config/ingestion-exclusions.local.json` with:

```json
{
  "phones": [
    "+6588888888",
    "+6587878787",
    "+6591213474",
    "+6581234567"
  ],
  "emails": [],
  "email_domains": [
    "ekolife.asia",
    "ada.asia",
    "speedzone.asia",
    "autocollect.asia"
  ],
  "names": [],
  "source_ids": []
}
```

- [ ] **Step 3: Verify JSON shape via config tests**

Run:

```bash
uv run pytest services/ingestion/tests/test_exclusion_config.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit only if explicit commit authorization has been granted**

If the user has explicitly asked for commits in this execution session, run:

```bash
git add config/ingestion-exclusions.example.json config/ingestion-exclusions.local.json
git commit -m "Update ingestion exclusion lists"
```

If no explicit commit authorization has been granted, do not commit.

---

### Task 6: Run Final Verification

**Files:**
- Verify: `services/ingestion/src/exclusion_config.py`
- Verify: `services/ingestion/src/exclusions.py`
- Verify: `services/ingestion/tests/test_exclusion_config.py`
- Verify: `services/ingestion/tests/test_exclusions.py`
- Verify: `config/ingestion-exclusions.example.json`
- Verify: `config/ingestion-exclusions.local.json`

- [ ] **Step 1: Run focused ingestion tests**

Run:

```bash
uv run pytest services/ingestion/tests/test_exclusion_config.py services/ingestion/tests/test_exclusions.py -q
```

Expected: PASS.

- [ ] **Step 2: Run all existing exclusion tests**

Run:

```bash
uv run pytest services/ingestion/tests/test_exclusion_config.py services/ingestion/tests/test_exclusions.py services/ingestion/tests/test_chat_exclusions.py services/ingestion/tests/test_fundbox_exclusions.py services/ingestion/tests/test_phppos_exclusions.py services/ingestion/tests/test_phppos_sales_exclusions.py -q
```

Expected: PASS.

- [ ] **Step 3: Run ingestion lint**

Run:

```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src services/ingestion/tests
```

Expected: PASS.

- [ ] **Step 4: Run ingestion type check**

Run:

```bash
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src
```

Expected: PASS, ignoring only documented pre-existing failures if they appear in unrelated files listed in `CLAUDE.md`.

- [ ] **Step 5: Inspect git diff**

Run:

```bash
git diff -- services/ingestion/src/exclusion_config.py services/ingestion/src/exclusions.py services/ingestion/tests/test_exclusion_config.py services/ingestion/tests/test_exclusions.py config/ingestion-exclusions.example.json config/ingestion-exclusions.local.json
```

Expected: Diff only contains the config schema addition, requested exclusion values, domain matching implementation, and tests.

- [ ] **Step 6: Final commit only if explicit commit authorization has been granted**

If the user has explicitly asked for commits in this execution session and prior task commits were skipped, run:

```bash
git add services/ingestion/src/exclusion_config.py services/ingestion/src/exclusions.py services/ingestion/tests/test_exclusion_config.py services/ingestion/tests/test_exclusions.py config/ingestion-exclusions.example.json config/ingestion-exclusions.local.json
git commit -m "Add ingestion email domain exclusions"
```

If no explicit commit authorization has been granted, do not commit.

---

## Self-Review

- Spec coverage: The plan adds `email_domains` to the exclusion config, supports exact domain and subdomain matching, updates local phones and domains, updates the example schema, and adds focused tests.
- Placeholder scan: No placeholders remain; every code step includes exact snippets and commands.
- Type consistency: `ExclusionFile.email_domains` is introduced before `build_exclusion_context()` uses it; `ExclusionContext.email_domains` is introduced before tests instantiate it.
