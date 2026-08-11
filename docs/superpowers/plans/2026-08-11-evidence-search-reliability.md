# Evidence Search Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make long evidence-search runs identity-safe, screenshot-complete, resumable, responsive, and able to generate patient workbooks incrementally.

**Architecture:** Keep the existing patient-by-patient coordinator and provider adapters, but add shared variant-identity, audit, capture-validation, popup, and browser-session units. Provider results fail closed, all outcomes become canonical audit records, processed workbooks restore through one indexed background load, and patient workbooks are written from the search worker after each patient with a final reconciliation pass.

**Tech Stack:** Python 3.11, PyQt6, openpyxl, Pillow, requests, Microsoft Edge CDP, pytest

## Global Constraints

- Use somatic mode and hg19/GRCh37 for Franklin.
- ClinVar evidence is accepted only after explicit GRCh37 chromosome, position, reference, and alternate-allele verification.
- MTBP continues to submit one variant per analysis and exports no authenticated report URL.
- Browser work stays serial and reuses one session per provider for the run.
- Successful pages receive no blanket extra wait; extended waits happen only after an incomplete-content incident.
- Variant identity ambiguity fails closed and remains resumable.
- Credentials, patient identifiers, and real evidence artifacts must not enter tests or Git history.
- Known artifacts remain orange and override all priority highlighting.
- Tier I + Tier II greater than 5 is strong green.
- Germ greater than 10 with AF at least 35% is strong green.
- Germ greater than 10 with AF below 35% is weak green.
- Missing or invalid AF receives no germline-priority fill and produces a data warning.
- Every task follows red-green-refactor, runs focused tests, and ends with an atomic commit.

---

## File Structure

### New files

- `src/archer_processor/services/variant_identity.py` — normalize GRCh37 variant identity and compare provider candidates.
- `src/archer_processor/services/evidence_audit.py` — canonical audit naming/writing, status semantics, legacy migration, path rebasing, and one-pass indexing.
- `src/archer_processor/services/capture_validation.py` — validate screenshot dimensions and image content.
- `src/archer_processor/services/browser_popups.py` — dismiss only recognized modal/banner overlays.
- `src/archer_processor/services/browser_session.py` — own one lazily-created Edge session per provider and one safe restart.
- `src/archer_processor/reports/patient_report_coordinator.py` — merge evidence and write/reconcile patient workbooks without failing the run.
- `tests/test_evidence_audit.py` — canonical audit, migration, rebasing, and index tests.
- `tests/test_capture_validation.py` — blank/incomplete screenshot tests.
- `tests/test_browser_session.py` — session reuse, restart, and close tests.
- `tests/test_patient_report_coordinator.py` — incremental, locked-file, and reconciliation tests.

### Existing files changed

- `src/archer_processor/core/highlights.py` — exact priority thresholds and AF-aware categories.
- `src/archer_processor/reports/excel_report.py` — strong/weak green mappings.
- `src/archer_processor/reports/patient_excel.py` — use the same row highlighting on `Oversikt`.
- `src/archer_processor/services/database_search.py` — resolve and verify multiple ClinVar candidates.
- `src/archer_processor/services/browser_review.py` — Franklin fallback/readiness, popup guard, session reuse, MTBP recovery, canonical audits.
- `src/archer_processor/services/processed_workbook.py` — indexed restore and legacy-evidence migration.
- `src/archer_processor/services/__init__.py` — export shared evidence status helpers.
- `src/archer_processor/reports/__init__.py` — export the patient report coordinator.
- `src/archer_processor/gui/app.py` — background workbook loader, resumable statuses, report outcomes, final state.
- `tests/test_highlights.py` — threshold boundary tests.
- `tests/test_browser_review.py` — Franklin, popup, capture, and MTBP behavior.
- `tests/test_database_search.py` — GRCh37 ClinVar candidate verification.
- `tests/test_processed_workbook.py` — one-pass indexing and legacy migration.
- `tests/test_patient_excel.py` — report highlight consistency.
- `tests/test_gui.py` — responsive loading, provider isolation, completion summaries.
- `docs/clinical_workflow.md` and `README.md` — document resume, GRCh37 verification, and automatic reports.

---

### Task 1: Exact Priority Highlighting

**Files:**

- Modify: `src/archer_processor/core/highlights.py:8-15`
- Modify: `src/archer_processor/reports/excel_report.py:85-98,489-497`
- Modify: `src/archer_processor/reports/patient_excel.py:69-83,222-283`
- Modify: `src/archer_processor/gui/app.py:62-78,2090-2105`
- Test: `tests/test_highlights.py`
- Test: `tests/test_patient_excel.py`
- Test: `tests/test_gui.py`

**Interfaces:**

- Consumes: `VariantRecord.history_matches` and `VariantRecord.af`.
- Produces: `variant_highlight(variant) -> Literal["artifact", "tier", "germline", "germline_low_af", ""]`.
- Produces: `priority_warning(variant) -> str` without mutating the record during rendering.
- Produces: identical strong/weak fill decisions in the GUI, review workbook, and patient workbook.

- [ ] **Step 1: Replace the old inclusive threshold tests with explicit boundary tests**

```python
def test_priority_highlight_boundaries_and_precedence():
    assert variant_highlight(variant(history_matches=[{"Tier I": 2, "Tier II": 3}])) == ""
    assert variant_highlight(variant(history_matches=[{"Tier I": 3, "Tier II": 3}])) == "tier"
    assert variant_highlight(variant(history_matches=[{"Germ": 10}], af=0.80)) == ""
    assert variant_highlight(variant(history_matches=[{"Germ": 11}], af=0.3499)) == "germline_low_af"
    assert variant_highlight(variant(history_matches=[{"Germ": 11}], af=0.35)) == "germline"
    assert variant_highlight(variant(history_matches=[{"Germ": 11}], af=None)) == ""
    assert variant_highlight(
        variant(history_matches=[{"Tier I": 6}], matched_rules=["known_artifact"])
    ) == "artifact"
```

Add workbook assertions that strong and weak rows use different fills and add a GUI assertion that both colors differ from artifact orange.

- [ ] **Step 2: Run the boundary tests and confirm the current rules fail**

Run: `python -m pytest tests/test_highlights.py tests/test_patient_excel.py tests/test_gui.py -q`

Expected: FAIL because Tier total 5 and Germ count 10 are currently highlighted, AF is ignored, and patient overview rows are not colored.

- [ ] **Step 3: Implement the threshold categories**

```python
def variant_highlight(variant: VariantRecord) -> str:
    if _is_artifact(variant):
        return "artifact"
    if _history_sum(variant, "Tier I", "Tier II") > 5:
        return "tier"
    if _history_sum(variant, "Germ") > 10:
        if variant.af is None:
            return ""
        return "germline" if variant.af >= 0.35 else "germline_low_af"
    return ""


def priority_warning(variant: VariantRecord) -> str:
    if _history_sum(variant, "Germ") > 10 and variant.af is None:
        return "Germline priority could not be colored because AF is missing."
    return ""
```

Map `tier` and `germline` to strong green (`C6EFCE` in Excel, `#CDEDD8` in Qt), map `germline_low_af` to weak green (`E9F6EF`/`#E9F6EF`), and keep `artifact` orange. In `PatientExcelReportWriter._overview_sheet`, apply the same fill across each variant's `A:I` cells. Surface `priority_warning` in the existing Warnings column and the variant tooltip without changing `variant.warnings` from a paint/render path.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_highlights.py tests/test_patient_excel.py tests/test_gui.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the highlighting slice**

```powershell
git add src/archer_processor/core/highlights.py src/archer_processor/reports/excel_report.py src/archer_processor/reports/patient_excel.py src/archer_processor/gui/app.py tests/test_highlights.py tests/test_patient_excel.py tests/test_gui.py
git commit -m "feat: apply exact clinical priority highlights"
```

---

### Task 2: Variant Identity and Canonical Audit Semantics

**Files:**

- Create: `src/archer_processor/services/variant_identity.py`
- Create: `src/archer_processor/services/evidence_audit.py`
- Modify: `src/archer_processor/services/browser_review.py:2196-2201`
- Modify: `src/archer_processor/services/__init__.py`
- Test: `tests/test_evidence_audit.py`
- Test: `tests/test_browser_review.py`

**Interfaces:**

- Produces: `GenomicIdentity(assembly, chromosome, position, reference, alternate)`.
- Produces: `genomic_identity(variant, assembly="GRCh37") -> GenomicIdentity | None`.
- Produces: `IdentityVerification(accepted, basis, reason, returned)`.
- Produces: `RETRYABLE_EVIDENCE_STATUSES`, `is_completed_evidence(evidence) -> bool`.
- Produces: `audit_digest(database, variant) -> str` and `write_evidence_audit(...) -> Path`.
- Produces: `persist_evidence_result(...) -> DatabaseEvidence`, the single provider-loop exit used for success and failure.

- [ ] **Step 1: Write identity, status, and canonical audit tests**

```python
def test_grch37_identity_normalizes_archer_fields():
    record = VariantRecord(
        source_file=Path("synthetic.tsv"), source_row=2,
        sample="SYNTHETIC_VPM_1", symbol="LUC7L2",
        hgvsc="NM_016019.4:c.784dup", genomic_location="chr7:139097298",
        ref_allele="T", alt_allele="TC",
    )
    assert genomic_identity(record) == GenomicIdentity("GRCh37", "7", 139097298, "T", "TC")


def test_retryable_statuses_are_not_completed():
    for status in (
        "error", "timeout", "identity_mismatch", "partial_capture",
        "verification_required", "quota_exhausted", "session_lost",
        "login_required", "rate_limited", "unauthorized",
    ):
        assert not is_completed_evidence(DatabaseEvidence("Franklin", status))
    assert is_completed_evidence(DatabaseEvidence("Franklin", "found"))
    assert is_completed_evidence(DatabaseEvidence("Franklin", "not_found"))


def test_write_evidence_audit_adds_schema_and_attempt_metadata(tmp_path):
    path = write_evidence_audit(
        tmp_path,
        "Franklin",
        record,
        DatabaseEvidence("Franklin", "identity_mismatch", "wrong candidate"),
        query_attempts=["LUC7L2:c.784dup", "chr7-139097298 T>TC"],
        duration_seconds=2.5,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["retryable"] is True
    assert payload["query_attempts"] == ["LUC7L2:c.784dup", "chr7-139097298 T>TC"]
```

- [ ] **Step 2: Run the new tests and confirm imports fail**

Run: `python -m pytest tests/test_evidence_audit.py tests/test_browser_review.py -q`

Expected: FAIL because the new modules and interfaces do not exist.

- [ ] **Step 3: Implement normalized identity types**

```python
@dataclass(frozen=True, slots=True)
class GenomicIdentity:
    assembly: str
    chromosome: str
    position: int
    reference: str
    alternate: str


@dataclass(frozen=True, slots=True)
class IdentityVerification:
    accepted: bool
    basis: str
    reason: str
    returned: GenomicIdentity | None = None


def genomic_identity(variant: VariantRecord, assembly: str = "GRCh37") -> GenomicIdentity | None:
    match = re.fullmatch(
        r"(?:chr)?(?P<chromosome>[0-9]{1,2}|X|Y|M|MT):(?P<position>\d+)(?:-\d+)?",
        re.sub(r"\s+", "", variant.genomic_location or ""),
        flags=re.IGNORECASE,
    )
    reference = re.sub(r"\s+", "", variant.ref_allele or "").upper()
    alternate = re.sub(r"\s+", "", variant.alt_allele or "").upper()
    if not match or not reference or not alternate:
        return None
    chromosome = match.group("chromosome").upper().removeprefix("CHR")
    chromosome = "M" if chromosome == "MT" else chromosome
    return GenomicIdentity(assembly, chromosome, int(match.group("position")), reference, alternate)
```

- [ ] **Step 4: Implement status and audit helpers**

```python
AUDIT_SCHEMA_VERSION = 2
RETRYABLE_EVIDENCE_STATUSES = frozenset({
    "error", "login_required", "rate_limited", "timeout", "unauthorized",
    "identity_mismatch", "partial_capture", "verification_required",
    "quota_exhausted", "session_lost",
})


def is_completed_evidence(evidence: DatabaseEvidence) -> bool:
    return evidence.status.strip().casefold() not in RETRYABLE_EVIDENCE_STATUSES


def audit_digest(database: str, variant: VariantRecord) -> str:
    identity = f"{database}|{variant.symbol}|{variant.hgvsc}|{variant.hgvsp}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def write_evidence_audit(
    artifact_directory: Path,
    database: str,
    variant: VariantRecord,
    evidence: DatabaseEvidence,
    *,
    query_attempts: Sequence[str] = (),
    duration_seconds: float = 0.0,
) -> Path:
    path = artifact_directory / f"{audit_digest(database, variant)}.audit.json"
    payload = asdict(evidence)
    payload.update({
        "schema_version": AUDIT_SCHEMA_VERSION,
        "retryable": not is_completed_evidence(evidence),
        "variant_key": f"{variant.sample}|{variant.hgvsc}",
        "query_attempts": list(query_attempts),
        "duration_seconds": round(max(0.0, duration_seconds), 3),
        "written_at": datetime.now(timezone.utc).isoformat(),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def persist_evidence_result(
    artifact_directory: Path,
    database: str,
    variant: VariantRecord,
    evidence: DatabaseEvidence,
    *,
    query_attempts: Sequence[str] = (),
    started_at: float,
) -> DatabaseEvidence:
    write_evidence_audit(
        artifact_directory,
        database,
        variant,
        evidence,
        query_attempts=query_attempts,
        duration_seconds=time.monotonic() - started_at,
    )
    return evidence
```

Remove `BrowserReviewService._write_audit`. Subsequent provider tasks must return every terminal per-variant result through `persist_evidence_result`, including authentication, quota, timeout, identity, partial-capture, session, and unexpected-error outcomes.

- [ ] **Step 5: Export the shared status helpers and switch `_completed_evidence_sources` to them**

Remove the duplicate status set from `gui/app.py`. Completion must be decided by `is_completed_evidence(item)` so `identity_mismatch`, `partial_capture`, `verification_required`, and `quota_exhausted` are resumed.

```python
def _completed_evidence_sources(
    evidence: dict[str, list[DatabaseEvidence]],
) -> set[tuple[str, str]]:
    return {
        (key, item.database)
        for key, items in evidence.items()
        for item in items
        if is_completed_evidence(item)
    }
```

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/test_evidence_audit.py tests/test_browser_review.py tests/test_gui.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the evidence foundation**

```powershell
git add src/archer_processor/services/variant_identity.py src/archer_processor/services/evidence_audit.py src/archer_processor/services/browser_review.py src/archer_processor/services/__init__.py src/archer_processor/gui/app.py tests/test_evidence_audit.py tests/test_browser_review.py tests/test_gui.py
git commit -m "feat: add canonical evidence identity and status model"
```

---

### Task 3: One-pass Processed-workbook Recovery

**Files:**

- Modify: `src/archer_processor/services/evidence_audit.py`
- Modify: `src/archer_processor/services/processed_workbook.py:23-177`
- Test: `tests/test_evidence_audit.py`
- Test: `tests/test_processed_workbook.py`

**Interfaces:**

- Consumes: `audit_digest`, `GenomicIdentity`, and status helpers from Task 2.
- Produces: `EvidenceAuditIndex.build(root) -> EvidenceAuditIndex`.
- Produces: `EvidenceAuditIndex.load(patient_index, database, variant) -> tuple[DatabaseEvidence | None, list[str]]`.
- Produces: `ProcessedWorkbookLoader.load(path, progress=None) -> ProcessedWorkbookState`.

- [ ] **Step 1: Add tests for one traversal, legacy selection, path rebasing, and ClinVar migration**

```python
def processed_with_audit(
    tmp_path: Path,
    evidence: DatabaseEvidence,
    *,
    screenshot: Path | None = None,
) -> tuple[Path, VariantRecord, Path]:
    output = tmp_path / "synthetic_review.xlsx"
    result = VariantProcessor().process(FIXTURE, "2026-08-11", output)
    variant = result.variants[3]
    key = f"{variant.sample}|{variant.hgvsc}"
    if screenshot is not None:
        evidence.raw["screenshot"] = str(screenshot)
    ExcelReportWriter().write(result, output, evidence={key: [evidence]})
    patients = list(dict.fromkeys(item.patient_id for item in result.variants))
    patient_index = patients.index(variant.patient_id) + 1
    audit_root = tmp_path / "synthetic_review_browser_evidence"
    audit = (
        audit_root / f"patient-{patient_index:03d}" / evidence.database.casefold()
        / f"{audit_digest(evidence.database, variant)}.audit.json"
    )
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(json.dumps(asdict(evidence)), encoding="utf-8")
    return output, variant, audit_root


def test_processed_workbook_builds_one_audit_index(tmp_path, monkeypatch):
    output, _, _ = processed_with_audit(
        tmp_path, DatabaseEvidence("OncoKB", "found", "synthetic")
    )
    calls = []
    original = Path.rglob

    def counted(path, pattern):
        calls.append((path, pattern))
        return original(path, pattern)

    monkeypatch.setattr(Path, "rglob", counted)
    ProcessedWorkbookLoader().load(output)
    assert len(calls) == 1


def test_legacy_clinvar_found_requires_grch37_reverification(tmp_path):
    output, _, _ = processed_with_audit(
        tmp_path,
        DatabaseEvidence("ClinVar", "found", "legacy", raw={"clinvar_id": "123"}),
    )
    state = ProcessedWorkbookLoader().load(output)
    restored = next(item for items in state.evidence.values() for item in items)
    assert restored.status == "verification_required"


def test_moved_screenshot_is_rebased_under_current_evidence_root(tmp_path):
    old_image = Path("K:/old/run_browser_evidence/patient-004/oncokb/shot.png")
    output, _, audit_root = processed_with_audit(
        tmp_path, DatabaseEvidence("OncoKB", "found", "synthetic"),
        screenshot=old_image,
    )
    expected_image = audit_root / "patient-004" / "oncokb" / "shot.png"
    expected_image.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 200), "navy").save(expected_image)
    state = ProcessedWorkbookLoader().load(output)
    restored = next(item for items in state.evidence.values() for item in items)
    assert Path(restored.raw["screenshot"]) == expected_image
```

Use synthetic patient/sample names only. Add a duplicate Franklin audit fixture where the canonical record wins; without one, the valid record with verified identity and the most valid screenshots wins.

- [ ] **Step 2: Run the recovery tests and measure the current repeated traversal failure**

Run: `python -m pytest tests/test_evidence_audit.py tests/test_processed_workbook.py -q`

Expected: FAIL because `_load_audit` calls `rglob` for each missing expected audit and legacy statuses are not migrated.

- [ ] **Step 3: Implement the audit index**

```python
@dataclass(slots=True)
class EvidenceAuditIndex:
    root: Path
    by_digest: dict[str, list[Path]]

    @classmethod
    def build(cls, root: Path) -> "EvidenceAuditIndex":
        by_digest: dict[str, list[Path]] = defaultdict(list)
        if root.exists():
            for path in root.rglob("*.audit.json"):
                match = re.match(r"([0-9a-f]{16})(?:-[^.]+)?\.audit\.json$", path.name)
                if match:
                    by_digest[match.group(1)].append(path)
        return cls(root=root, by_digest=dict(by_digest))

    def candidates(self, database: str, variant: VariantRecord) -> list[Path]:
        digest = audit_digest(database, variant)
        return [
            path for path in self.by_digest.get(digest, [])
            if path.parent.name.casefold() == database.casefold()
        ]
```

Implement deterministic legacy selection with this order: canonical filename, verified identity, greatest number of valid screenshots, newest modification time. Never delete legacy files while loading.

- [ ] **Step 4: Migrate and rebase loaded evidence**

```python
def migrate_loaded_evidence(evidence: DatabaseEvidence, artifact_root: Path) -> DatabaseEvidence:
    if (
        evidence.database == "ClinVar"
        and evidence.status == "found"
        and evidence.raw.get("assembly_verified") != "GRCh37"
    ):
        evidence.status = "verification_required"
        evidence.summary = "Legacy ClinVar result requires explicit GRCh37 verification."
    rebase_screenshot_paths(evidence.raw, artifact_root)
    return evidence
```

Rebasing preserves the path suffix from the first directory component beneath the old `*_browser_evidence` root and applies it beneath the current root only when the candidate file exists.

- [ ] **Step 5: Update `ProcessedWorkbookLoader` to build and pass one index**

Add an optional progress callback:

```python
def load(
    self,
    workbook_path: Path,
    progress: Callable[[int, int, str], None] | None = None,
) -> ProcessedWorkbookState:
```

Build the index once before `_restore_evidence`, report progress per restored variant, append malformed-audit warnings to `ProcessingResult.warnings`, and remove recursive scanning from `_load_audit`.

- [ ] **Step 6: Run focused recovery tests**

Run: `python -m pytest tests/test_evidence_audit.py tests/test_processed_workbook.py -q`

Expected: PASS, including exactly one recursive evidence-directory traversal.

- [ ] **Step 7: Commit indexed recovery**

```powershell
git add src/archer_processor/services/evidence_audit.py src/archer_processor/services/processed_workbook.py tests/test_evidence_audit.py tests/test_processed_workbook.py
git commit -m "fix: restore evidence through one indexed scan"
```

---

### Task 4: Franklin Query Fallback and Identity Verification

**Files:**

- Modify: `src/archer_processor/services/variant_identity.py`
- Modify: `src/archer_processor/services/browser_review.py:799-1047,2681-2708,2893-2927`
- Test: `tests/test_browser_review.py`

**Interfaces:**

- Consumes: `GenomicIdentity` and `write_evidence_audit` from Task 2.
- Produces: `_franklin_queries(variant) -> list[str]`.
- Produces: `_franklin_identity(body_text, url, variant) -> IdentityVerification`.
- Produces: `_search_franklin_query(page, variant, query) -> DatabaseEvidence` for one bounded query attempt group.
- Changes: `parse_franklin_page(..., query: str | None = None)` records match basis and requested/returned identity.

- [ ] **Step 1: Add Franklin query-order and same-genomic-variant tests**

```python
def luc7l2_variant() -> VariantRecord:
    return VariantRecord(
        source_file=Path("synthetic.tsv"), source_row=1,
        sample="SYNTHETIC_VPM_1", symbol="LUC7L2",
        hgvsc="NM_016019.4:c.784dup",
        hgvsp="NP_057103.2:p.Arg262ProfsTer26",
        genomic_location="chr7:139097298", ref_allele="T", alt_allele="TC",
    )


def test_franklin_queries_use_transcript_then_requested_genomic_syntax():
    variant = luc7l2_variant()
    assert _franklin_queries(variant) == [
        "LUC7L2:c.784dup",
        "chr7-139097298 T>TC",
    ]


def test_franklin_accepts_different_transcript_for_same_grch37_variant():
    body = "FMC1-LUC7L2 c.982dup GRCh37 chr7:139097298 T>TC Suggested classification Likely pathogenic"
    evidence = parse_franklin_page(body, luc7l2_variant(), "https://franklin.genoox.com/variant")
    assert evidence.status == "found"
    assert evidence.raw["identity_verification"]["basis"] == "grch37_genomic"


def test_franklin_rejects_same_gene_at_different_position():
    body = "LUC7L2 GRCh37 chr7:139097299 T>TC Suggested classification Pathogenic"
    evidence = parse_franklin_page(body, luc7l2_variant(), "https://franklin.genoox.com/variant")
    assert evidence.status == "identity_mismatch"
```

Add a fake-page search test proving fallback runs only after the transcript attempt is unresolved or mismatched and is skipped after a verified first result.

- [ ] **Step 2: Run Franklin tests and verify the wrong formatting and strict transcript behavior fail**

Run: `python -m pytest tests/test_browser_review.py -k "franklin and (query or identity or fallback)" -q`

Expected: FAIL because the genomic query currently uses hyphen-separated alleles and the matcher requires the requested cDNA.

- [ ] **Step 3: Implement ordered Franklin queries**

```python
def _franklin_queries(variant: VariantRecord) -> list[str]:
    queries: list[str] = []
    cdna = _cdna_change(variant.hgvsc)
    if variant.symbol and cdna:
        queries.append(f"{variant.symbol}:{cdna}")
    identity = genomic_identity(variant)
    if identity:
        queries.append(
            f"chr{identity.chromosome}-{identity.position} "
            f"{identity.reference}>{identity.alternate}"
        )
    return list(dict.fromkeys(query for query in queries if query))
```

- [ ] **Step 4: Implement identity-aware candidate acceptance**

Parse an exact normalized cDNA from page text first. Separately parse a GRCh37 genomic tuple from page text or the Franklin result URL. Return `IdentityVerification(True, "exact_transcript", ...)` for an exact transcript or `IdentityVerification(True, "grch37_genomic", ...)` for an exact genomic tuple. Reject a conflicting tuple even when the gene matches.

Store this shape in `evidence.raw`:

```python
evidence.raw["identity_verification"] = {
    "accepted": verification.accepted,
    "basis": verification.basis,
    "reason": verification.reason,
    "requested": asdict(genomic_identity(variant)) if genomic_identity(variant) else None,
    "returned": asdict(verification.returned) if verification.returned else None,
}
```

- [ ] **Step 5: Refactor `_search_franklin` to try queries in order and audit every terminal attempt**

For each query, preserve the existing bounded retry loop. Continue to the genomic query on `identity_mismatch`, unresolved result, or timeout. Stop on `found`, `quota_exhausted`, authentication failure, or structurally invalid input. Store `query_attempts` and write the one canonical audit for success and failure.

```python
query_attempts: list[str] = []
evidence = DatabaseEvidence("Franklin", "invalid_query", "No usable Franklin query.")
started = time.monotonic()
for query in _franklin_queries(variant):
    query_attempts.append(query)
    evidence = self._search_franklin_query(page, variant, query)
    if evidence.status == "found":
        break
    if evidence.status not in {"identity_mismatch", "timeout", "not_found", "error"}:
        break
evidence.raw["query_attempts"] = list(query_attempts)
write_evidence_audit(
    artifact_directory,
    "Franklin",
    variant,
    evidence,
    query_attempts=query_attempts,
    duration_seconds=time.monotonic() - started,
)
```

- [ ] **Step 6: Run all Franklin tests**

Run: `python -m pytest tests/test_browser_review.py -k franklin -q`

Expected: PASS, including the synthetic LUC7L2 different-transcript case.

- [ ] **Step 7: Commit Franklin identity resolution**

```powershell
git add src/archer_processor/services/variant_identity.py src/archer_processor/services/browser_review.py tests/test_browser_review.py
git commit -m "fix: add identity-safe Franklin genomic fallback"
```

---

### Task 5: Content-aware Screenshots and Popup Guard

**Files:**

- Create: `src/archer_processor/services/capture_validation.py`
- Create: `src/archer_processor/services/browser_popups.py`
- Modify: `src/archer_processor/services/browser_review.py:1753-2180,2202-2360`
- Modify: `src/archer_processor/services/evidence_audit.py`
- Test: `tests/test_capture_validation.py`
- Test: `tests/test_browser_review.py`

**Interfaces:**

- Produces: `CaptureValidation(valid, reason, width, height, luminance_stddev)`.
- Produces: `validate_capture(path, min_width=200, min_height=80, min_stddev=1.0)`.
- Produces: `choose_overlay_action(labels) -> str` for deterministic consent preference.
- Produces: `dismiss_known_overlays(page) -> list[str]`.
- Produces: `IncompleteCaptureError`, carrying the final `CaptureValidation`.
- Produces: `_capture_with_incident_retry(...) -> CaptureValidation`.

- [ ] **Step 1: Add image and popup safety tests**

```python
def test_blank_capture_is_rejected(tmp_path):
    path = tmp_path / "blank.png"
    Image.new("RGB", (800, 500), "white").save(path)
    result = validate_capture(path)
    assert not result.valid
    assert result.reason == "low_content"


def test_content_capture_is_accepted(tmp_path):
    path = tmp_path / "content.png"
    image = Image.new("RGB", (800, 500), "white")
    ImageDraw.Draw(image).rectangle((20, 20, 780, 300), fill="navy")
    image.save(path)
    assert validate_capture(path).valid


def test_popup_action_prefers_rejection_and_ignores_generic_close():
    assert choose_overlay_action(["Close", "Reject all"]) == "Reject all"
    assert choose_overlay_action(["Accept essential", "Accept all"]) == "Accept essential"
    assert choose_overlay_action(["Close"]) == ""
```

Add Franklin fake-DOM tests where category elements initially return empty strings, later include `De Novo Data`, and never raise `IndexError`. Add a test where the first screenshot is blank, the incident wait runs once, and the recapture succeeds. Add a failure test asserting a second blank capture yields `partial_capture`.

- [ ] **Step 2: Run capture tests and confirm failures**

Run: `python -m pytest tests/test_capture_validation.py tests/test_browser_review.py -k "capture or popup or category or assessment" -q`

Expected: FAIL because screenshot files and semantic content are not validated and category titles are indexed while empty.

- [ ] **Step 3: Implement image validation**

```python
@dataclass(frozen=True, slots=True)
class CaptureValidation:
    valid: bool
    reason: str
    width: int
    height: int
    luminance_stddev: float


class IncompleteCaptureError(RuntimeError):
    def __init__(self, validation: CaptureValidation) -> None:
        super().__init__(f"Screenshot validation failed: {validation.reason}")
        self.validation = validation


def validate_capture(
    path: Path,
    *,
    min_width: int = 200,
    min_height: int = 80,
    min_stddev: float = 1.0,
) -> CaptureValidation:
    try:
        with Image.open(path) as image:
            gray = image.convert("L")
            width, height = gray.size
            deviation = float(ImageStat.Stat(gray).stddev[0])
    except (OSError, ValueError):
        return CaptureValidation(False, "unreadable", 0, 0, 0.0)
    if width < min_width or height < min_height:
        return CaptureValidation(False, "too_small", width, height, deviation)
    if deviation < min_stddev:
        return CaptureValidation(False, "low_content", width, height, deviation)
    return CaptureValidation(True, "ok", width, height, deviation)
```

- [ ] **Step 4: Implement scoped popup dismissal**

Query only recognized containers:

```python
OVERLAY_SELECTOR = (
    "[role='dialog'], [aria-modal='true'], #onetrust-banner-sdk, "
    "#onetrust-consent-sdk, [class*='cookie-consent'], "
    "[class*='modal'], [class*='overlay'], [class*='banner']"
)
BUTTON_LABELS = (
    "Reject all", "Accept essential", "Accept necessary", "Only necessary",
    "No thanks", "Maybe later", "Got it",
)


def choose_overlay_action(labels: Sequence[str]) -> str:
    normalized = {label.strip().casefold(): label for label in labels}
    for preferred in BUTTON_LABELS:
        match = normalized.get(preferred.casefold())
        if match:
            return match
    return ""
```

Click a label only through `container.get_by_role("button", name=label, exact=True)`. A close icon is eligible only when it is a descendant of the recognized container and has a provider-specific selector; a generic `Close` label is never selected by `choose_overlay_action`. Return clicked labels for the audit log. Extend the existing OncoKB fake-page test to prove an ordinary page-level `Close` control is untouched.

- [ ] **Step 5: Replace fixed readiness with semantic waits and incident-only retries**

Create `_wait_for_nonempty_category_titles(categories, required_title=None)` that reads until every used title has a non-empty first line and, for ACMG, `De Novo Data` exists. Do not execute `splitlines()[0]` before this helper succeeds.

```python
def _first_nonempty_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _wait_for_nonempty_category_titles(
    self,
    page: Any,
    categories: Any,
    *,
    required_title: str | None = None,
) -> list[str]:
    for _ in range(max(1, self.navigation_timeout_ms // 500)):
        self._check_cancelled()
        texts = categories.all_inner_texts()
        titles = [_first_nonempty_line(text) for text in texts]
        if titles and all(titles) and (required_title is None or required_title in titles):
            return titles
        page.wait_for_timeout(500)
    raise IncompleteCaptureError(CaptureValidation(False, "semantic_content_missing", 0, 0, 0.0))
```

For Predictions, require one of `REVEL`, `SpliceAI`, `CADD`, or an explicit no-data message. For Population Frequencies, require `gnomAD`, `ExAC`, an allele-frequency value, or an explicit no-data message. First use the normal stability wait. If semantic content or image validation fails, wait 5 seconds once and recapture. A second failure raises `IncompleteCaptureError` and the result becomes `partial_capture` with the reason and screenshot-validation records in `raw`.

```python
def _capture_with_incident_retry(
    self,
    page: Any,
    path: Path,
    capture: Callable[[], None],
) -> CaptureValidation:
    capture()
    validation = validate_capture(path)
    if validation.valid:
        return validation
    self._interruptible_page_wait(page, 5_000)
    capture()
    validation = validate_capture(path)
    if not validation.valid:
        raise IncompleteCaptureError(validation)
    return validation
```

- [ ] **Step 6: Prevent overview/evidence duplication**

Keep the overview clip bottom at the first evidence-card top. Capture each ACMG card through `De Novo Data` exactly once and each oncology tile exactly once. Deduplicate screenshot records by `(label, resolved path)` before audit write.

```python
def _deduplicate_screenshots(records: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, str]] = []
    for record in records:
        key = (record.get("label", ""), str(Path(record.get("path", "")).resolve()))
        if key not in seen:
            seen.add(key)
            unique.append(dict(record))
    return unique
```

- [ ] **Step 7: Migrate blank legacy critical images during restore**

When a legacy `found` audit has a required screenshot that is unreadable, too small, or low-content, convert it to `partial_capture`. Do not invalidate optional screenshots or text-only terminal `not_found` results.

```python
SCREENSHOT_REQUIRED_DATABASES = frozenset({"COSMIC", "OncoKB", "Franklin", "ClinVar", "MTBP"})


def critical_screenshot_paths(evidence: DatabaseEvidence) -> list[Path]:
    records = evidence.raw.get("screenshots", [])
    return [
        Path(record["path"])
        for record in records
        if isinstance(record, dict) and record.get("path")
    ]


if evidence.status == "found" and evidence.database in SCREENSHOT_REQUIRED_DATABASES:
    critical = critical_screenshot_paths(evidence)
    if not critical or any(not validate_capture(path).valid for path in critical):
        evidence.status = "partial_capture"
        evidence.summary = f"{evidence.database} requires screenshot recapture."
```

- [ ] **Step 8: Run focused and regression tests**

Run: `python -m pytest tests/test_capture_validation.py tests/test_browser_review.py tests/test_processed_workbook.py -q`

Expected: PASS with no `list index out of range`, no accepted blank critical capture, and no duplicate Case Control record.

- [ ] **Step 9: Commit content-aware capture**

```powershell
git add src/archer_processor/services/capture_validation.py src/archer_processor/services/browser_popups.py src/archer_processor/services/browser_review.py src/archer_processor/services/evidence_audit.py tests/test_capture_validation.py tests/test_browser_review.py tests/test_processed_workbook.py
git commit -m "fix: validate browser evidence before capture"
```

---

### Task 6: GRCh37-verified ClinVar Resolution

**Files:**

- Modify: `src/archer_processor/services/database_search.py:211-377`
- Modify: `src/archer_processor/services/browser_review.py:553-689`
- Test: `tests/test_database_search.py`
- Test: `tests/test_browser_review.py`

**Interfaces:**

- Consumes: `GenomicIdentity` from Task 2.
- Produces: `_clinvar_candidate_matches(root, expected) -> IdentityVerification`.
- Produces: verified `DatabaseEvidence.raw` keys `assembly_verified`, `matched_location`, `query_attempts`, and `clinvar_id`.
- Browser capture consumes only a `found` result with `assembly_verified == "GRCh37"`.

- [ ] **Step 1: Add multi-candidate and assembly tests**

```python
def clinvar_variant() -> VariantRecord:
    return VariantRecord(
        source_file=Path("synthetic.tsv"), source_row=1,
        sample="SYNTHETIC_VPM_1", symbol="LUC7L2",
        hgvsc="NM_016019.4:c.784dup", genomic_location="chr7:139097298",
        ref_allele="T", alt_allele="TC",
    )


def xml_response(text: str) -> FakeResponse:
    response = FakeResponse({})
    response.content = text.encode("utf-8")
    return response


def sequence_location_xml(
    variation_id: str,
    assembly: str,
    chromosome: str,
    position: int,
    reference: str,
    alternate: str,
) -> str:
    return (
        f"<ReleaseSet><VariationArchive VariationID='{variation_id}' "
        f"VariationName='synthetic'><SequenceLocation Assembly='{assembly}' "
        f"Chr='{chromosome}' positionVCF='{position}' "
        f"referenceAlleleVCF='{reference}' alternateAlleleVCF='{alternate}'/>"
        "</VariationArchive></ReleaseSet>"
    )


def test_clinvar_skips_wrong_first_candidate_and_accepts_matching_grch37(monkeypatch):
    records = {
        "111": sequence_location_xml("111", "GRCh37", "7", 139097299, "T", "TC"),
        "222": sequence_location_xml("222", "GRCh37", "7", 139097298, "T", "TC"),
    }
    service = DatabaseSearchService(AppSettings())

    def fake_eutils(url, params):
        if "esearch.fcgi" in url:
            return xml_response("<eSearchResult><IdList><Id>111</Id><Id>222</Id></IdList></eSearchResult>")
        return xml_response(records[params["id"]])

    monkeypatch.setattr(service, "_eutils_get", fake_eutils)
    evidence = service._search_clinvar(clinvar_variant())
    assert evidence.status == "found"
    assert evidence.raw["clinvar_id"] == "222"
    assert evidence.raw["assembly_verified"] == "GRCh37"


def test_clinvar_rejects_matching_grch38_only_candidate(monkeypatch):
    service = DatabaseSearchService(AppSettings())

    def fake_eutils(url, params):
        if "esearch.fcgi" in url:
            return xml_response("<eSearchResult><IdList><Id>333</Id></IdList></eSearchResult>")
        return xml_response(
            sequence_location_xml("333", "GRCh38", "7", 139407430, "T", "TC")
        )

    monkeypatch.setattr(service, "_eutils_get", fake_eutils)
    evidence = service._search_clinvar(clinvar_variant())
    assert evidence.status == "identity_mismatch"
```

Add regression cases for wrong gene and wrong allele. Add a browser test asserting `_search_clinvar` does not navigate when API evidence lacks `assembly_verified="GRCh37"`.

- [ ] **Step 2: Run ClinVar tests and confirm first-result behavior fails**

Run: `python -m pytest tests/test_database_search.py tests/test_browser_review.py -k clinvar -q`

Expected: FAIL because ESearch uses `retmax=1`, broad searches are accepted, and assembly/alleles are not checked.

- [ ] **Step 3: Build exact ClinVar queries and request multiple IDs**

```python
def _clinvar_queries(self, variant: VariantRecord) -> list[str]:
    queries = [f"{variant.hgvsc}[VARNAME]"] if variant.hgvsc else []
    identity = genomic_identity(variant)
    if identity:
        queries.append(
            f"{identity.chromosome}[chr] AND {identity.position}[chrpos37]"
        )
    return list(dict.fromkeys(query for query in queries if query))
```

Use `retmax=20`. Preserve ordered query attempts and unique candidate IDs. Do not use broad `gene protein`, `gene cDNA`, or unqualified genomic-location fallbacks for automatic acceptance.

- [ ] **Step 4: Verify candidate `SequenceLocation` elements**

```python
def _clinvar_candidate_matches(
    root: ET.Element,
    expected: GenomicIdentity,
) -> IdentityVerification:
    for location in root.findall(".//SequenceLocation"):
        chromosome = (location.attrib.get("Chr") or "").removeprefix("chr")
        position = location.attrib.get("positionVCF")
        reference = (location.attrib.get("referenceAlleleVCF") or "").upper()
        alternate = (location.attrib.get("alternateAlleleVCF") or "").upper()
        if location.attrib.get("Assembly") != "GRCh37" or not position:
            continue
        returned = GenomicIdentity("GRCh37", chromosome, int(position), reference, alternate)
        if returned == expected:
            return IdentityVerification(True, "grch37_vcf", "Exact GRCh37 VCF match.", returned)
    return IdentityVerification(False, "none", "No exact GRCh37 VCF location matched.")
```

If no genomic identity can be constructed from the Archer row, return `invalid_query` rather than accepting an unverified ClinVar result.

- [ ] **Step 5: Store verification and gate browser capture**

Populate `raw["assembly_verified"] = "GRCh37"`, `raw["matched_location"]`, `raw["query_attempts"]`, and `raw["candidate_ids"]`. In browser review, treat any `found` result without explicit GRCh37 proof as `verification_required` and do not open it.

- [ ] **Step 6: Run all ClinVar tests**

Run: `python -m pytest tests/test_database_search.py tests/test_browser_review.py tests/test_processed_workbook.py -k clinvar -q`

Expected: PASS; wrong first hits and GRCh38-only hits are never captured.

- [ ] **Step 7: Commit ClinVar verification**

```powershell
git add src/archer_processor/services/database_search.py src/archer_processor/services/browser_review.py tests/test_database_search.py tests/test_browser_review.py tests/test_processed_workbook.py
git commit -m "fix: verify ClinVar candidates against GRCh37"
```

---

### Task 7: Provider Session Reuse and MTBP Recovery

**Files:**

- Create: `src/archer_processor/services/browser_session.py`
- Modify: `src/archer_processor/services/browser_review.py:65-118,209-388,389-689,799-1752,2335-2401`
- Modify: `src/archer_processor/gui/app.py:179-513`
- Test: `tests/test_browser_session.py`
- Test: `tests/test_browser_review.py`
- Test: `tests/test_gui.py`

**Interfaces:**

- Produces: `BrowserHandle(database, page, close)`.
- Produces: `BrowserSessionPool.get(database)`, `restart(database)`, and `close()`.
- Produces: `BrowserReviewService.close()` and context-manager methods.
- Changes: `BrowserReviewService.__init__(..., session_pool: BrowserSessionPool | None = None)` permits deterministic tests.
- Changes: `_search_mtbp_batch(..., page)` uses the run-owned MTBP page and never launches Edge itself.
- Produces: `_open_exact_mtbp_report(page, analysis_id) -> bool` without submitting.
- Produces: `_finish_mtbp_analysis(...)` and `_parse_ready_mtbp_report(...)` by extracting existing polling and parsing blocks.

- [ ] **Step 1: Add session reuse, restart, and cleanup tests**

```python
def fake_handle(database: str, opened: list[str]) -> BrowserHandle:
    opened.append(database)
    return BrowserHandle(database, object(), lambda: None)


def synthetic_variant(row: int, gene: str, hgvsc: str) -> VariantRecord:
    return VariantRecord(
        source_file=Path("synthetic.tsv"), source_row=row,
        sample="SYNTHETIC_VPM_1", symbol=gene, hgvsc=hgvsc,
    )


def test_session_pool_reuses_one_handle_per_provider():
    opened = []
    pool = BrowserSessionPool(lambda database: fake_handle(database, opened))
    first = pool.get("Franklin")
    second = pool.get("Franklin")
    assert first is second
    assert opened == ["Franklin"]


def test_session_pool_restarts_provider_once_and_closes_old_handle():
    opened = []
    pool = BrowserSessionPool(lambda database: fake_handle(database, opened))
    first = pool.get("MTBP")
    second = pool.restart("MTBP")
    assert first.closed
    assert second is not first
    with pytest.raises(SessionRestartExhausted):
        pool.restart("MTBP")


def test_mtbp_multiple_variants_launch_one_provider_session(tmp_path, monkeypatch):
    launches = []
    pool = BrowserSessionPool(lambda database: fake_handle(database, launches))
    service = BrowserReviewService(
        profile_root=tmp_path,
        request_delay_ms=0,
        request_delay_max_ms=0,
        session_pool=pool,
    )
    variants = [
        synthetic_variant(1, "TP53", "NM_000546.6:c.524G>A"),
        synthetic_variant(2, "KRAS", "NM_004985.5:c.183A>C"),
    ]

    def fake_batch(current, artifact_directory, *, progress, page):
        return {
            service.variant_key(item): DatabaseEvidence("MTBP", "found", "synthetic")
            for item in current
        }

    monkeypatch.setattr(service, "_search_mtbp_batch", fake_batch)
    service._search_mtbp(variants, tmp_path, progress=None)
    service.close()
    assert launches == ["MTBP"]
```

Add a test where MTBP loses CDP after submission: the restarted page checks Reports List for the exact `ARCHER-*` analysis ID and does not click `#run-analysis` a second time. Add a hidden/detached screenshot-row test where target discovery is repeated once and succeeds.

- [ ] **Step 2: Run session and MTBP tests and confirm repeated launches fail**

Run: `python -m pytest tests/test_browser_session.py tests/test_browser_review.py -k "session or mtbp" -q`

Expected: FAIL because MTBP currently opens a new runtime and Edge context for every variant.

- [ ] **Step 3: Implement the session pool**

```python
@dataclass(slots=True)
class BrowserHandle:
    database: str
    page: Any
    close_callback: Callable[[], None]
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.close_callback()


class SessionRestartExhausted(RuntimeError):
    pass


class BrowserSessionPool:
    def __init__(self, factory: Callable[[str], BrowserHandle]) -> None:
        self._factory = factory
        self._handles: dict[str, BrowserHandle] = {}
        self._restarted: set[str] = set()

    def get(self, database: str) -> BrowserHandle:
        if database not in self._handles:
            self._handles[database] = self._factory(database)
        return self._handles[database]

    def restart(self, database: str) -> BrowserHandle:
        if database in self._restarted:
            raise SessionRestartExhausted(database)
        self._restarted.add(database)
        previous = self._handles.pop(database, None)
        if previous:
            previous.close()
        return self.get(database)

    def close(self) -> None:
        for handle in reversed(list(self._handles.values())):
            handle.close()
        self._handles.clear()
```

- [ ] **Step 4: Make `BrowserReviewService` own and close run sessions**

Implement `_open_provider_handle(database)` using the existing `sync_edge_cdp()` context manager. Its close callback closes the Edge context and exits the runtime context manager. Replace provider-local `with sync_browser()` blocks with `self._sessions.get(database).page`. Add `__enter__`, `__exit__`, and idempotent `close`.

```python
def _open_provider_handle(self, database: str) -> BrowserHandle:
    sync_browser, _, _ = self._browser_api()
    runtime_manager = sync_browser()
    runtime = runtime_manager.__enter__()
    try:
        context = runtime.chromium.launch_persistent_context(
            str(self.profile_directory(database)),
            channel=self.channel,
            headless=False,
            accept_downloads=True,
            viewport={"width": 1440, "height": 1000},
            background=self.browser_background,
        )
        page = context.pages[0] if context.pages else context.new_page()
    except BaseException:
        runtime_manager.__exit__(*sys.exc_info())
        raise
    return BrowserHandle(
        database=database,
        page=page,
        close_callback=lambda: runtime_manager.__exit__(None, None, None),
    )


def close(self) -> None:
    self._sessions.close()
```

Wrap both `DatabaseWorker.run` and `BrowserReviewWorker.run` with `finally: browser_service.close()` so stop, failure, and success all release Edge.

- [ ] **Step 5: Make ordinary provider lookups restart once**

Catch only the CDP browser error class around an idempotent single-variant navigation/capture. Restart that provider and retry the same variant once. A second connection failure yields `DatabaseEvidence(database, "session_lost", ...)`, writes its canonical audit, and allows later providers/patients to continue.

```python
def _run_provider_operation(
    self,
    database: str,
    variant: VariantRecord,
    operation: Callable[[Any], DatabaseEvidence],
) -> DatabaseEvidence:
    _, browser_error, _ = self._browser_api()
    for attempt in range(2):
        try:
            return operation(self._sessions.get(database).page)
        except browser_error as exc:
            if attempt == 0:
                self._sessions.restart(database)
                continue
            return DatabaseEvidence(
                database,
                "session_lost",
                f"{database} Edge session was lost after one safe restart: {exc}",
                accession=_review_query(variant),
            )
    raise AssertionError("provider retry loop exhausted without returning")
```

- [ ] **Step 6: Reuse MTBP while preventing duplicate submission**

Pass the run-owned page into `_search_mtbp_batch`. Track `analysis_id` and `submitted` before and after clicking `#run-analysis`. If CDP is lost:

- before submission: restart once and repeat form preparation;
- after submission: restart once, open `MTBP_REPORTS_URL`, and recover only the exact `analysis_id`;
- if the exact report is unavailable: return `session_lost`; never resubmit that analysis automatically.

Keep one variant in `variants` for every call from `_search_mtbp`.

```python
try:
    page.locator("#run-analysis").click()
    submitted = True
    return self._finish_mtbp_analysis(page, analysis_id, query_pairs, artifact_directory)
except browser_error as exc:
    if not submitted:
        page = self._sessions.restart("MTBP").page
        return self._search_mtbp_batch(
            variants, artifact_directory, progress=progress, page=page
        )
    page = self._sessions.restart("MTBP").page
    if self._open_exact_mtbp_report(page, analysis_id):
        return self._parse_ready_mtbp_report(
            page, analysis_id, query_pairs, artifact_directory
        )
    return {
        self.variant_key(variant): DatabaseEvidence(
            "MTBP", "session_lost",
            f"MTBP session was lost after submitting {analysis_id}; the analysis was not resubmitted.",
            accession=query,
            url="",
        )
        for variant, query in query_pairs
    }
```

Define `_finish_mtbp_analysis`, `_open_exact_mtbp_report`, and `_parse_ready_mtbp_report` by extracting the existing queue polling, exact report-link recovery, and parse/capture blocks without changing their clinical matching rules.

- [ ] **Step 7: Rediscover the MTBP screenshot target on incident**

Extract `_locate_mtbp_screenshot_target(page, variant)`. On hidden, detached, or missing target, wait 2 seconds, re-read the table, expand the exact accordion again, and retry once. If it remains unavailable, return `partial_capture` with the already-parsed evidence in `raw`.

```python
last_error: Exception | None = None
for attempt in range(2):
    try:
        target = self._locate_mtbp_screenshot_target(page, variant)
        target.scroll_into_view_if_needed()
        target.screenshot(path=str(screenshot_path))
        return screenshot_path
    except Exception as exc:
        last_error = exc
        if attempt == 0:
            self._interruptible_page_wait(page, 2_000)
            continue
raise IncompleteCaptureError(
    CaptureValidation(False, f"mtbp_target:{last_error}", 0, 0, 0.0)
)
```

- [ ] **Step 8: Run focused and worker tests**

Run: `python -m pytest tests/test_browser_session.py tests/test_browser_review.py tests/test_gui.py -k "session or mtbp or worker or stop or pause" -q`

Expected: PASS; one MTBP session serves multiple variants, stop closes sessions, and connection loss does not duplicate an analysis.

- [ ] **Step 9: Commit session lifetime and MTBP recovery**

```powershell
git add src/archer_processor/services/browser_session.py src/archer_processor/services/browser_review.py src/archer_processor/gui/app.py tests/test_browser_session.py tests/test_browser_review.py tests/test_gui.py
git commit -m "fix: reuse provider sessions across long searches"
```

---

### Task 8: Responsive Loading and Incremental Patient Reports

**Files:**

- Create: `src/archer_processor/reports/patient_report_coordinator.py`
- Modify: `src/archer_processor/reports/__init__.py`
- Modify: `src/archer_processor/gui/app.py:81-108,179-513,568-605,1399-1473,1457-1467,1652-1733,1912-2021,2159-2194`
- Test: `tests/test_patient_report_coordinator.py`
- Test: `tests/test_gui.py`

**Interfaces:**

- Produces: `PatientReportOutcome(patient_id, path, status, message)`.
- Produces: `PatientReportCoordinator.merge`, `write_patient`, and `reconcile`.
- Produces: `SearchRunOutcome(evidence, retry_count, report_outcomes)` for worker completion.
- Produces: `ProcessedWorkbookWorker.progress`, `finished`, and `failed` signals.
- Changes: search workers emit `patient_completed(str, object)` and `report_outcome(object)`.

- [ ] **Step 1: Add coordinator tests for incremental output, locked reports, and reconciliation**

```python
def synthetic_result(
    tmp_path: Path,
    *,
    patient_id: str,
) -> tuple[ProcessingResult, list[VariantRecord]]:
    variant = VariantRecord(
        source_file=tmp_path / "synthetic.tsv", source_row=2,
        sample=f"{patient_id}_VPM_SYNTHETIC", symbol="TP53",
        hgvsc="NM_000546.6:c.524G>A", hgvsp="NP_000537.3:p.Arg175His",
    )
    result = ProcessingResult(
        input_path=variant.source_file,
        output_path=tmp_path / "synthetic_review.xlsx",
        run_date="2026-08-11",
        variants=[variant],
        rules_applied=[],
    )
    return result, [variant]


def test_patient_report_is_written_beside_main_workbook(tmp_path):
    result, variants = synthetic_result(tmp_path, patient_id="26OUM00001")
    coordinator = PatientReportCoordinator(result, variants, {})
    outcome = coordinator.write_patient("26OUM00001")
    assert outcome.status == "written"
    assert outcome.path == tmp_path / "26OUM00001_VPM_Tolkning.xlsx"
    assert outcome.path.exists()


def test_locked_patient_report_is_pending_not_fatal(tmp_path, monkeypatch):
    result, variants = synthetic_result(tmp_path, patient_id="26OUM00001")
    coordinator = PatientReportCoordinator(result, variants, {})
    monkeypatch.setattr(
        coordinator.writer,
        "write_patient",
        lambda *args: (_ for _ in ()).throw(PermissionError(13, "locked")),
    )
    outcome = coordinator.write_patient("26OUM00001")
    assert outcome.status == "pending"
    assert coordinator.pending == {"26OUM00001"}
```

Add a reconciliation test where the first write is pending and the final pass succeeds. Add a worker test proving `patient_completed` is emitted once after all sources, not after every source checkpoint.

- [ ] **Step 2: Add asynchronous workbook-loading GUI tests**

Use `QSignalSpy` or `qtbot.waitUntil` to assert that `_load_processed_workbook` returns after starting a `QThread`, shows loading progress, and applies the state only when the worker emits `finished`. Add a failure test showing a concise dialog while the app remains usable.

- [ ] **Step 3: Run new tests and confirm synchronous behavior fails**

Run: `python -m pytest tests/test_patient_report_coordinator.py tests/test_gui.py -k "report or processed_workbook or load" -q`

Expected: FAIL because loading is synchronous and reports are only created manually.

- [ ] **Step 4: Implement the pure-Python report coordinator**

```python
@dataclass(frozen=True, slots=True)
class PatientReportOutcome:
    patient_id: str
    path: Path
    status: str
    message: str


@dataclass(slots=True)
class SearchRunOutcome:
    evidence: dict[str, list[DatabaseEvidence]]
    retry_count: int
    report_outcomes: list[PatientReportOutcome]


class PatientReportCoordinator:
    def __init__(
        self,
        result: ProcessingResult,
        variants: Sequence[VariantRecord],
        evidence: dict[str, list[DatabaseEvidence]],
        writer: PatientExcelReportWriter | None = None,
    ) -> None:
        self.result = result
        self.variants = list(variants)
        self.evidence = {key: list(items) for key, items in evidence.items()}
        self.writer = writer or PatientExcelReportWriter()
        self.pending: set[str] = set()

    def write_patient(self, patient_id: str) -> PatientReportOutcome:
        patient_variants = [item for item in self.variants if item.patient_id == patient_id]
        if self.result.output_path is None:
            raise ValueError("Patient reports require a processed workbook output path.")
        safe_patient = re.sub(r"[^A-Za-z0-9_-]+", "_", patient_id).strip("_")
        output = self.result.output_path.parent / f"{safe_patient}_VPM_Tolkning.xlsx"
        try:
            self.writer.write_patient(
                self.result, patient_id, patient_variants, output, self.evidence
            )
        except PermissionError as exc:
            self.pending.add(patient_id)
            return PatientReportOutcome(patient_id, output, "pending", str(exc))
        self.pending.discard(patient_id)
        return PatientReportOutcome(patient_id, output, "written", "Patient report written.")
```

`merge` uses database replacement semantics. `reconcile` calls `write_patient` for every patient missing, stale, or pending and returns all outcomes.

- [ ] **Step 5: Write patient reports from the search worker**

Pass `ProcessingResult` and the existing evidence snapshot into `DatabaseWorker` and `BrowserReviewWorker`. After the final provider for a patient:

1. merge patient evidence into the coordinator;
2. emit `patient_completed(patient_id, patient_evidence)`;
3. call `write_patient(patient_id)` in the worker thread;
4. emit `report_outcome(outcome)`;
5. continue regardless of `pending` status.

At successful run completion, call `reconcile()` and emit `SearchRunOutcome`. Update `_database_finished` and `_browser_review_finished` to merge `outcome.evidence`, log every report outcome, and pass `outcome.retry_count` plus the number of `pending` reports into the final status renderer.

```python
coordinator = PatientReportCoordinator(
    self.result,
    self.variants,
    self.existing_evidence,
)
report_outcomes: list[PatientReportOutcome] = []
for patient_id, patient_variants in patients:
    patient_evidence = self._search_patient(patient_id, patient_variants)
    coordinator.merge(patient_evidence)
    self.patient_completed.emit(patient_id, patient_evidence)
    report = coordinator.write_patient(patient_id)
    report_outcomes.append(report)
    self.report_outcome.emit(report)
report_outcomes.extend(coordinator.reconcile())
self.finished.emit(
    SearchRunOutcome(
        evidence=coordinator.evidence,
        retry_count=sum(
            not is_completed_evidence(item)
            for items in coordinator.evidence.values()
            for item in items
        ),
        report_outcomes=report_outcomes,
    )
)
```

Extract `_search_patient` from the current patient loop without changing provider order. `reconcile` must return only final reconciliation outcomes, not duplicate already-written records.

- [ ] **Step 6: Implement `ProcessedWorkbookWorker` and asynchronous apply**

```python
class ProcessedWorkbookWorker(QObject):
    finished = pyqtSignal(object, object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)

    def __init__(self, workbook_path: Path, settings: AppSettings) -> None:
        super().__init__()
        self.workbook_path = workbook_path
        self.settings = settings

    def run(self) -> None:
        try:
            history_path = Path(self.settings.history_workbook)
            history = VariantHistoryRepository(history_path) if history_path.exists() else None
            loader = ProcessedWorkbookLoader(
                filter_engine=FilterEngine(production_rules(self.settings.artifact_rules)),
                history=history,
            )
            state = loader.load(self.workbook_path, progress=self.progress.emit)
            self.finished.emit(self.workbook_path, state)
        except Exception as exc:
            self.failed.emit(str(exc))
```

`MainWindow._load_processed_workbook` starts the thread and returns. Move the existing state-to-widget code into `_processed_workbook_loaded(workbook_path, state)`. Remove `QApplication.processEvents()` from this path. Disable only actions that require a loaded analysis; keep the window responsive and show progress.

- [ ] **Step 7: Report clear completion states**

Handle `PatientReportOutcome` by logging written paths or one nonfatal lock warning. The final progress card must distinguish:

- `Search complete`
- `Search complete · retry available`
- `Search complete · report save pending`
- `Search stopped · progress saved`

Calculate retry counts with the shared retry-status helper.

```python
def _complete_search_outcome(self, outcome: SearchRunOutcome) -> None:
    pending_reports = sum(item.status == "pending" for item in outcome.report_outcomes)
    if pending_reports:
        title = "Search complete · report save pending"
    elif outcome.retry_count:
        title = "Search complete · retry available"
    else:
        title = "Search complete"
    self._complete_run_progress(title)
    self.search_btn.setText(
        "Resume Incomplete Search" if outcome.retry_count else "Run Evidence Search"
    )
```

- [ ] **Step 8: Run coordinator and GUI tests**

Run: `python -m pytest tests/test_patient_report_coordinator.py tests/test_gui.py tests/test_patient_excel.py tests/test_processed_workbook.py -q`

Expected: PASS; loading is asynchronous, a locked file is nonfatal, and patient reports appear beside the main workbook during the run.

- [ ] **Step 9: Commit responsive recovery and automatic reports**

```powershell
git add src/archer_processor/reports/patient_report_coordinator.py src/archer_processor/reports/__init__.py src/archer_processor/gui/app.py tests/test_patient_report_coordinator.py tests/test_gui.py
git commit -m "feat: write patient reports during resumable searches"
```

---

### Task 9: End-to-end Recovery Regression and Documentation

**Files:**

- Modify: `tests/test_browser_review.py`
- Modify: `tests/test_database_search.py`
- Modify: `tests/test_processed_workbook.py`
- Modify: `tests/test_gui.py`
- Modify: `README.md`
- Modify: `docs/clinical_workflow.md`

**Interfaces:**

- Consumes: all preceding task interfaces.
- Produces: one synthetic regression scenario covering false matches, partial captures, moved evidence, session loss, resume, and patient reports.

- [ ] **Step 1: Add the synthetic end-to-end recovery test**

Create synthetic variants in test code and fake provider/browser responses. The scenario must prove:

```python
assert recovered_clinvar.status == "verification_required"
assert recovered_franklin.status == "partial_capture"
assert completed_oncokb.status == "found"
assert pending_sources == {("SYNTHETIC_VPM_1|NM_016019.4:c.784dup", "ClinVar"),
                           ("SYNTHETIC_VPM_1|NM_016019.4:c.784dup", "Franklin")}
assert oncokb_call_count == 0
assert (tmp_path / "SYNTHETIC_VPM_1_VPM_Tolkning.xlsx").exists()
assert all(path.exists() for path in expected_retry_audit_paths)
```

The fixture must not contain any real patient identifier, credential, screenshot, or copied provider response containing patient information.

- [ ] **Step 2: Run the end-to-end test before final integration fixes**

Run: `python -m pytest tests/test_processed_workbook.py tests/test_gui.py -k recovery -q`

Expected: PASS if all earlier slices integrate correctly; otherwise fix only the failing integration boundary and rerun.

- [ ] **Step 3: Update operator documentation**

Document in `README.md` and `docs/clinical_workflow.md`:

- Franklin transcript-first and `chr-position REF>ALT` fallback;
- ClinVar GRCh37 verification and `verification_required` legacy behavior;
- provider session reuse and background/minimized Edge behavior;
- meaning of retryable evidence and `Resume Incomplete Search`;
- automatic `{DIT}_VPM_Tolkning.xlsx` generation beside the processed workbook;
- locked-report recovery and final status labels;
- exact priority highlighting thresholds and artifact precedence.

- [ ] **Step 4: Run formatting and secret checks**

Run:

```powershell
git diff --check
rg -n -i "api[_-]?key\s*=\s*['\"][^'\"]+|password\s*=\s*['\"][^'\"]+|bearer\s+[A-Za-z0-9._-]+" src tests docs README.md
```

Expected: no whitespace errors and no hard-coded credential matches.

- [ ] **Step 5: Run the complete automated suite**

Run: `python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 6: Perform the local large-workbook acceptance check**

With the application closed, copy the processed workbook from `D:\CFB_app_browser_evidence2` to a temporary verification directory while leaving the original untouched. Open the copy through `Open Processed Workbook` and verify:

- the window stays responsive and displays loading progress;
- evidence discovery performs one indexed pass;
- old verified results remain complete;
- legacy unverified ClinVar, failed Franklin, blank captures, and CDP failures appear as pending resume work;
- no completed OncoKB/COSMIC result is queued again;
- patient workbooks are produced beside the copied main workbook;
- no provider is contacted until `Resume Incomplete Search` is explicitly started.

Record elapsed load time and pending counts in the commit message notes or final handoff, but do not commit the workbook or evidence directory.

- [ ] **Step 7: Run a controlled browser smoke test**

Use one synthetic/non-patient variant for each configured provider. Verify:

- Franklin explicitly selects hg19 and Somatic;
- Franklin uses fallback only when necessary and all required screenshots pass validation;
- ClinVar opens only the verified GRCh37 variation page;
- popup banners do not appear in screenshots;
- MTBP submits one variant per report through one Edge session;
- the final UI says `Search complete` or clearly identifies retry/pending work.

- [ ] **Step 8: Commit documentation and final regression coverage**

```powershell
git add tests/test_browser_review.py tests/test_database_search.py tests/test_processed_workbook.py tests/test_gui.py README.md docs/clinical_workflow.md
git commit -m "docs: describe reliable evidence recovery workflow"
```

- [ ] **Step 9: Review the complete branch before delivery**

Run:

```powershell
git status --short
git log --oneline 30354d9..HEAD
git diff 30354d9..HEAD --stat
python -m pytest -q
```

Expected: clean worktree, nine focused implementation commits or fewer only where adjacent tasks were safely combined, a scoped diff, and a passing suite.

Do not push until the user-requested final verification has passed.
