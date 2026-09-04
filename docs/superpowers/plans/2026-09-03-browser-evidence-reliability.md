# Browser Evidence Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent clipped Franklin evidence and make COSMIC identifiers, fallbacks, and failures deterministic and auditable.

**Architecture:** Keep provider orchestration in `BrowserReviewService`, while extracting small pure functions for COSMIC identifier parsing and failure classification. Capture validation operates on image dimensions and known page bounds so invalid screenshots remain retryable rather than silently accepted.

**Tech Stack:** Python 3.11+, pytest, Playwright-compatible browser fakes, Pillow

**Spec:** `docs/superpowers/specs/2026-09-03-vpm-app-improvements-design.md`

## Global Constraints

- Browser evidence is accepted only after exact GRCh37 variant identity validation.
- Franklin images may not silently omit the left or right edge of relevant text.
- All distinct COSM/COSV identifiers are tried in source order.
- COSMIC fallback may not select the first candidate without identity verification.
- Credentials, patient identifiers, and private report URLs are never exported.

---

### Task 1: Validate Franklin capture geometry

**Files:**
- Modify: `src/archer_processor/services/browser_review.py`
- Test: `tests/test_browser_review.py`

**Interfaces:**
- Produces: `_expanded_capture_box(target_box, document_box, horizontal_margin=32, top_margin=24, bottom_margin=24) -> dict[str, float]`
- Produces: `_capture_dimensions_valid(path: Path, minimum_width: int, minimum_height: int) -> bool`

- [ ] **Step 1: Write geometry unit tests**

Test that a target at the left document edge clamps to `x == 0`, a target near the right edge never exceeds document width, and a normal target receives 32-pixel margins on both sides. Test that zero-byte, blank, narrower-than-required, and shorter-than-required images are rejected.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `pytest tests/test_browser_review.py -q -k "franklin and (margin or geometry or dimensions)"`

Expected: helper methods do not exist.

- [ ] **Step 3: Implement geometry and Pillow validation**

Use numeric bounding boxes only; clamp `x`, `y`, `width`, and `height` to document bounds. Open the saved PNG with Pillow, call `verify()`, reopen it, and reject dimensions smaller than the requested capture floor or an all-white/all-transparent extrema range.

- [ ] **Step 4: Run focused tests**

Run: `pytest tests/test_browser_review.py -q -k "franklin and (margin or geometry or dimensions)"`

Expected: PASS.

- [ ] **Step 5: Commit geometry validation**

```bash
git add src/archer_processor/services/browser_review.py tests/test_browser_review.py
git commit -m "fix: validate Franklin capture bounds"
```

### Task 2: Apply safe bounds to every Franklin overview capture

**Files:**
- Modify: `src/archer_processor/services/browser_review.py`
- Test: `tests/test_browser_review.py`

**Interfaces:**
- Consumes: `_expanded_capture_box(target_box: dict[str, float], document_box: dict[str, float], horizontal_margin: int = 32, top_margin: int = 24, bottom_margin: int = 24) -> dict[str, float]`
- Consumes: `_capture_dimensions_valid(path: Path, minimum_width: int, minimum_height: int) -> bool`
- Preserves: `_capture_with_incident_retry(page: Any, path: Path, capture: Callable[[], None]) -> CaptureValidation` retry behavior

- [ ] **Step 1: Add a clipped-heading regression test**

Use a fake document where the gene header begins left of the current classification section. Assert `_capture_franklin_classification_overview()` requests a clip containing the entire header plus safety margin and retries when the first rendered PNG is invalid.

- [ ] **Step 2: Run regression tests and confirm failure**

Run: `pytest tests/test_browser_review.py -q -k "franklin_overview and (complete or clipped)"`

Expected: current clip begins too far right or accepts the invalid image.

- [ ] **Step 3: Use the expanded union of header and section bounds**

Compute the union of the gene-header box and the classification-content box, apply margins, clamp it, capture, then validate. Raise the existing incomplete-capture exception on failure so resume marks the provider pending.

- [ ] **Step 4: Run all Franklin tests**

Run: `pytest tests/test_browser_review.py -q -k franklin`

Expected: PASS.

- [ ] **Step 5: Commit Franklin fix**

```bash
git add src/archer_processor/services/browser_review.py tests/test_browser_review.py
git commit -m "fix: prevent clipped Franklin evidence"
```

### Task 3: Parse and try every COSMIC identifier

**Files:**
- Modify: `src/archer_processor/services/browser_review.py`
- Test: `tests/test_browser_review.py`

**Interfaces:**
- Produces: `_cosmic_identifiers(value: str | None) -> list[str]`
- Changes: `_resolve_cosmic_mutation_page(page, variant, cosmic_id) -> str`

- [ ] **Step 1: Write ordered-deduplication tests**

```python
def test_cosmic_identifiers_are_ordered_and_deduplicated():
    assert _cosmic_identifiers("COSM123; COSV456, COSM123") == ["COSM123", "COSV456"]
```

Add a resolver test where COSM123 has no result and COSV456 redirects to a verified GRCh37 page; assert both are attempted and COSV456 is recorded as the resolved accession.

- [ ] **Step 2: Run COSMIC ID tests and confirm failure**

Run: `pytest tests/test_browser_review.py -q -k "cosmic and identifier"`

Expected: current helper returns only the first ID.

- [ ] **Step 3: Iterate identifiers without weakening identity checks**

Return uppercase `COSM\d+` and `COSV\d+` tokens in source order with a seen set. Continue to the next ID only for a definitive no-result or unverified identity; propagate login, cancellation, and transient provider errors.

- [ ] **Step 4: Run all COSMIC ID tests**

Run: `pytest tests/test_browser_review.py -q -k cosmic`

Expected: PASS.

- [ ] **Step 5: Commit multi-ID lookup**

```bash
git add src/archer_processor/services/browser_review.py tests/test_browser_review.py
git commit -m "fix: resolve all COSMIC identifiers"
```

### Task 4: Add verified genomic COSMIC fallback and typed failures

**Files:**
- Create: `src/archer_processor/services/provider_failures.py`
- Modify: `src/archer_processor/services/browser_review.py`
- Test: `tests/test_browser_review.py`

**Interfaces:**
- Produces: `ProviderFailureKind` enum values `NOT_FOUND`, `LOGIN_REQUIRED`, `LAYOUT_CHANGED`, `AMBIGUOUS`, `IDENTITY_MISMATCH`, `TRANSIENT`
- Produces: `_cosmic_genomic_query(variant: VariantRecord) -> str`
- Consumes: GRCh37 normalized variant identity

- [ ] **Step 1: Write fallback and classification tests**

Test that exhausted identifiers trigger exactly one genomic search, that one exact GRCh37 candidate is accepted, that two exact candidates yield `AMBIGUOUS`, and that a different REF/ALT yields `IDENTITY_MISMATCH`. Add separate fixtures for login markup, missing expected selectors, and timeout.

- [ ] **Step 2: Run fallback tests and confirm failure**

Run: `pytest tests/test_browser_review.py -q -k "cosmic and (fallback or failure_kind)"`

Expected: fallback and typed failure metadata are absent.

- [ ] **Step 3: Implement typed internal failures**

Define:

```python
class ProviderFailureKind(StrEnum):
    NOT_FOUND = "not_found"
    LOGIN_REQUIRED = "login_required"
    LAYOUT_CHANGED = "layout_changed"
    AMBIGUOUS = "ambiguous"
    IDENTITY_MISMATCH = "identity_mismatch"
    TRANSIENT = "transient"
```

Map these to existing public evidence statuses while storing `raw["failure_kind"]`. The genomic fallback search must validate chromosome, position, REF, ALT, and GRCh37 before opening a candidate.

- [ ] **Step 4: Run all COSMIC and resume tests**

Run: `pytest tests/test_browser_review.py tests/test_gui.py -q -k "cosmic or resume"`

Expected: PASS and transient/unverified results remain retryable.

- [ ] **Step 5: Commit COSMIC fallback**

```bash
git add src/archer_processor/services/provider_failures.py src/archer_processor/services/browser_review.py tests/test_browser_review.py tests/test_gui.py
git commit -m "feat: add verified COSMIC genomic fallback"
```

### Task 5: Verify and document provider behavior

**Files:**
- Modify: `README.md`
- Modify: `docs/priority_database_automation.md`

- [ ] **Step 1: Document Franklin validation and COSMIC resolution order**

Include the distinct operator-visible failure categories and state that COSMIC identity is GRCh37-verified.

- [ ] **Step 2: Run complete tests**

Run: `pytest -q`

Expected: all tests pass.

- [ ] **Step 3: Perform non-patient browser smoke checks**

Use a public synthetic/known variant to confirm current Franklin bounds and COSMIC selectors. Do not save credentials, patient identifiers, or private URLs.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md docs/priority_database_automation.md
git commit -m "docs: describe browser evidence fallbacks"
```
