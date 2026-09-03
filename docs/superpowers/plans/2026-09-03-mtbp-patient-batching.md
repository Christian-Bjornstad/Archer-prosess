# MTBP Patient Batching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-variant MTBP reports with one recoverable combined report per patient, targeted genomic fallback, and locally derived variant screenshots.

**Architecture:** Put pure GRCh37 allele normalization in a new provider-independent module and keep MTBP portal syntax in `browser_review.py`. Reuse the existing batch submission/parser, but make report identity and evidence mapping explicitly carry the accepted query per variant. Capture the full report once, then derive exact variant crops from the same page.

**Tech Stack:** Python 3.11+, pytest, Playwright-compatible browser fakes, Pillow for image verification

**Spec:** `docs/superpowers/specs/2026-09-03-vpm-app-improvements-design.md`

## Global Constraints

- One initial MTBP submission per patient, not one per variant.
- Only explicitly rejected HGVSc queries change to genomic fallback.
- The full patient list is resubmitted together after replacement.
- GRCh37 is mandatory for genomic fallback.
- Identity mismatches fail closed; no guessed crop or result mapping.
- Resume recovers retained reports before resubmission.

---

### Task 1: Normalize VCF alleles into genomic changes

**Files:**
- Create: `src/archer_processor/services/genomic_notation.py`
- Create: `tests/test_genomic_notation.py`
- Modify: `src/archer_processor/services/browser_review.py`

**Interfaces:**
- Produces: `GenomicChange(kind: Literal["substitution", "deletion", "insertion", "delins"], start: int, end: int, ref: str, alt: str)`
- Produces: `normalize_vcf_change(location: str, ref: str, alt: str) -> GenomicChange | None`
- Produces: `format_mtbp_grch37(location: str, ref: str, alt: str) -> str`

- [ ] **Step 1: Write table-driven normalization tests**

```python
@pytest.mark.parametrize(
    ("location", "ref", "alt", "expected"),
    [
        ("chr19:33792996", "G", "A", "chr19:g.33792996G>A"),
        ("chr13:28609813", "GA", "G", "chr13:g.28609814del"),
        ("chr13:28609813", "G", "GA", "chr13:g.28609813_28609814insA"),
        ("chr13:28609813", "GA", "A", "chr13:g.28609813del"),
    ],
)
def test_format_mtbp_grch37(location, ref, alt, expected):
    assert format_mtbp_grch37(location, ref, alt) == expected
```

Add tests returning an empty string for missing location, symbolic alleles, equal REF/ALT, and non-DNA characters.

- [ ] **Step 2: Run tests and confirm failure**

Run: `pytest tests/test_genomic_notation.py -q`

Expected: import failure because the module does not exist.

- [ ] **Step 3: Implement trimming and coordinate calculation**

Trim matching suffix bases while both alleles have length greater than one, then trim matching prefix bases while advancing the coordinate. Classify one-to-one as substitution, empty ALT as deletion, empty REF as insertion, and remaining replacements as delins. Return no query for unparseable input. Replace `_mtbp_genomic_query()` internals with `format_mtbp_grch37(variant.genomic_location, variant.ref_allele, variant.alt_allele)`.

- [ ] **Step 4: Run notation and existing MTBP query tests**

Run: `pytest tests/test_genomic_notation.py tests/test_browser_review.py -q -k "genomic_queries or mtbp_queries"`

Expected: PASS.

- [ ] **Step 5: Commit notation helper**

```bash
git add src/archer_processor/services/genomic_notation.py src/archer_processor/services/browser_review.py tests/test_genomic_notation.py tests/test_browser_review.py
git commit -m "feat: normalize GRCh37 MTBP queries"
```

### Task 2: Submit and map one combined patient report

**Files:**
- Modify: `src/archer_processor/services/browser_review.py`
- Test: `tests/test_browser_review.py`

**Interfaces:**
- Produces: `_search_mtbp(variants: list[VariantRecord], artifact_directory: Path, *, progress: Callable[[str], None] | None, prior_evidence: dict[str, list[DatabaseEvidence]] | None = None) -> dict[str, DatabaseEvidence]`
- Consumes: `_search_mtbp_batch(batch: list[VariantRecord], artifact_directory: Path, *, progress: Callable[[str], None] | None) -> dict[str, DatabaseEvidence]`
- Evidence raw contract: `raw["submitted_query"]`, `raw["mtbp_analysis_id"]`, and `raw["mtbp_batch_size"]`

- [ ] **Step 1: Replace the per-variant expectation with a batch test**

```python
def test_mtbp_submits_one_combined_report_for_patient(tmp_path, monkeypatch):
    submitted = []
    variants = [make_variant("BRAF"), make_variant("TP53")]

    def run_batch(batch, artifact_directory, *, progress):
        submitted.append([item.hgvsc for item in batch])
        return [found_evidence(item) for item in batch]

    service = make_service(tmp_path)
    monkeypatch.setattr(service, "_search_mtbp_batch", run_batch)
    evidence = service._search_mtbp(object(), variants, tmp_path, progress=lambda _: None)
    assert submitted == [[variants[0].hgvsc, variants[1].hgvsc]]
    assert len(evidence) == 2
```

Adapt the existing local test helpers rather than adding duplicate factories.

- [ ] **Step 2: Run the MTBP orchestration tests and confirm failure**

Run: `pytest tests/test_browser_review.py -q -k "mtbp and (independent_report or combined_report)"`

Expected: current code records two single-item batches.

- [ ] **Step 3: Make `_search_mtbp` call one batch**

Remove the loop that invokes `_search_mtbp_batch([variant])`. Pass the complete patient sequence and preserve existing timeout recovery/finalization behavior at report scope.

- [ ] **Step 4: Run all MTBP tests**

Run: `pytest tests/test_browser_review.py -q -k mtbp`

Expected: PASS after adapting report-level fixtures.

- [ ] **Step 5: Commit batching**

```bash
git add src/archer_processor/services/browser_review.py tests/test_browser_review.py
git commit -m "feat: batch MTBP searches by patient"
```

### Task 3: Replace only rejected entries and resubmit the batch

**Files:**
- Modify: `src/archer_processor/services/browser_review.py`
- Test: `tests/test_browser_review.py`

**Interfaces:**
- Produces: `_mtbp_queries_for_batch(variants, rejected) -> dict[str, str]`
- Consumes: `_mtbp_unmapped_queries(body_text) -> list[str]`
- Consumes: `format_mtbp_grch37(location: str, ref: str, alt: str) -> str`

- [ ] **Step 1: Write a mixed acceptance test**

Build three variants. Make the first submission reject only TP53 HGVSc. Assert the second form payload contains unchanged BRAF and JAK2 HGVSc plus TP53 genomic notation, and only two report submissions occurred.

- [ ] **Step 2: Run the mixed test and confirm failure**

Run: `pytest tests/test_browser_review.py -q -k "mtbp_replaces_only_rejected"`

Expected: current behavior submits variants independently or applies fallback outside a shared payload.

- [ ] **Step 3: Track queries by stable variant key**

Initialize `{variant_key(v): _mtbp_variant_query(v)}`. After validation, match rejected strings exactly using the existing normalized rejection helper. Replace only matched entries with `_mtbp_genomic_query(v)`. If fallback is unavailable, emit `review_needed` for that variant and continue the resubmission only when the remaining report still maps one-to-one.

- [ ] **Step 4: Test rejection, recovery, and cancellation paths**

Run: `pytest tests/test_browser_review.py -q -k mtbp`

Expected: PASS, including no duplicate submission during retained-report recovery.

- [ ] **Step 5: Commit targeted fallback**

```bash
git add src/archer_processor/services/browser_review.py tests/test_browser_review.py
git commit -m "feat: retry rejected MTBP variants genomically"
```

### Task 4: Capture one full report and exact local variant crops

**Files:**
- Modify: `src/archer_processor/services/browser_review.py`
- Modify: `src/archer_processor/reports/patient_excel.py`
- Test: `tests/test_browser_review.py`
- Test: `tests/test_patient_excel.py`

**Interfaces:**
- Evidence raw contract: `raw["patient_report_screenshot"]` stores the common full image; `raw["screenshot"]` stores the variant crop
- Produces: `_capture_mtbp_full_report(page, path: Path) -> Path`
- Consumes: `_locate_mtbp_screenshot_target(page, variant) -> locator`

- [ ] **Step 1: Write capture-count and ambiguity tests**

Assert one `page.screenshot(full_page=True)` call for a three-variant report, three exact locator screenshots, and zero extra navigation/submission calls. Add cases where zero and two locators match; both must omit the variant crop and return a review-needed capture status.

- [ ] **Step 2: Run capture tests and confirm failure**

Run: `pytest tests/test_browser_review.py -q -k "mtbp and screenshot"`

Expected: no common full-report artifact exists.

- [ ] **Step 3: Implement full capture and per-variant derivatives**

Capture the full report after parsing and before cleanup. Reuse the exact row matcher for each variant and capture only a unique target with a small bounding-box margin clamped to document coordinates. Put the same full-image path into each batch evidence record and distinct crop paths into `raw["screenshot"]`.

- [ ] **Step 4: Place images in the workbook**

Deduplicate `patient_report_screenshot` paths and add the full MTBP report once to `VEDLEGG`. Keep `raw["screenshot"]` in the matching variant sheet's MTBP image section.

- [ ] **Step 5: Run browser and workbook tests**

Run: `pytest tests/test_browser_review.py tests/test_patient_excel.py -q -k "mtbp or attachment or image_order"`

Expected: PASS.

- [ ] **Step 6: Commit image slicing**

```bash
git add src/archer_processor/services/browser_review.py src/archer_processor/reports/patient_excel.py tests/test_browser_review.py tests/test_patient_excel.py
git commit -m "feat: slice combined MTBP reports by variant"
```

### Task 5: Verify and document the MTBP workflow

**Files:**
- Modify: `README.md`
- Modify: `docs/clinical_workflow.md`

- [ ] **Step 1: Document batching, fallback, recovery, and image placement**

State that a patient may require a second combined submission only when MTBP rejects one or more HGVSc queries.

- [ ] **Step 2: Run full automated tests**

Run: `pytest -q`

Expected: all tests pass.

- [ ] **Step 3: Perform a synthetic live smoke test**

Use non-patient synthetic variants matching the approved examples. Confirm one report, exact row mapping, one full capture, variant crops, and successful cleanup. Record only pass/fail and portal-layout observations; do not commit credentials or session data.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md docs/clinical_workflow.md
git commit -m "docs: describe patient-batched MTBP workflow"
```
