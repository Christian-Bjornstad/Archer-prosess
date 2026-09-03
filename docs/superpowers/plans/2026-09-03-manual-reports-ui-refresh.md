# Manual Reports and UI Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decouple patient workbooks from evidence searches, generate them safely under `VEDLEGG_APP` only on operator request, and clarify the evidence-page controls.

**Architecture:** `DatabaseWorker` and `BrowserReviewWorker` remain evidence/checkpoint workers and no longer own report coordination. A dedicated report worker invokes `PatientReportCoordinator` for all completed patients or an explicit selection. Report writing uses a destination helper and atomic replacement; UI styling is centralized in `gui/theme.py`.

**Tech Stack:** Python 3.11+, pytest, PyQt6, openpyxl

**Spec:** `docs/superpowers/specs/2026-09-03-vpm-app-improvements-design.md`

## Global Constraints

- No patient workbook is generated as a side effect of a database search.
- Output directory is exactly `VEDLEGG_APP` beside the review workbook.
- Output filename is exactly `<DIT>_VPM_Tolkning_APP.xlsx`.
- Default scope is all completed patients; selected patient rows can limit scope.
- Existing destination files are replaced only after a complete workbook has been written successfully.
- Locked files do not abort other patient reports.
- Primary text actions have a minimum 44-pixel target height.

---

### Task 1: Make patient-report paths and writes atomic

**Files:**
- Modify: `src/archer_processor/reports/patient_report_coordinator.py`
- Modify: `src/archer_processor/reports/patient_excel.py`
- Test: `tests/test_patient_report_coordinator.py`
- Test: `tests/test_patient_excel.py`

**Interfaces:**
- Produces: `patient_report_path(review_workbook: Path, patient_id: str) -> Path`
- Changes: `PatientReportOutcome.status` values to `created`, `updated`, `locked`, or `failed`
- Preserves: `write_patient(patient_id: str) -> PatientReportOutcome`

- [ ] **Step 1: Write destination and failure-safety tests**

```python
def test_patient_report_path_uses_approved_directory_and_name(tmp_path):
    review = tmp_path / "run_VPM_review.xlsx"
    assert patient_report_path(review, "26OUM12345") == (
        tmp_path / "VEDLEGG_APP" / "26OUM12345_VPM_Tolkning_APP.xlsx"
    )
```

Add a test where the writer raises after writing temporary bytes; assert an existing destination remains unchanged and temporary files are removed. Add created, updated, and locked outcome assertions.

- [ ] **Step 2: Run coordinator tests and confirm failure**

Run: `pytest tests/test_patient_report_coordinator.py tests/test_patient_excel.py -q`

Expected: current path is beside the review workbook and contains `Myolid_Tolkning`.

- [ ] **Step 3: Implement approved path and atomic replacement**

Create `VEDLEGG_APP` with `mkdir(parents=True, exist_ok=True)`. Write to a unique `.tmp.xlsx` path in that directory, close the workbook, then call `Path.replace(destination)`. In a `finally` block unlink only the exact temporary path if it still exists. Convert `PermissionError` to `locked` and other exceptions to `failed` without discarding outcomes for other patients.

- [ ] **Step 4: Run report tests**

Run: `pytest tests/test_patient_report_coordinator.py tests/test_patient_excel.py -q`

Expected: PASS.

- [ ] **Step 5: Commit safe output behavior**

```bash
git add src/archer_processor/reports/patient_report_coordinator.py src/archer_processor/reports/patient_excel.py tests/test_patient_report_coordinator.py tests/test_patient_excel.py
git commit -m "feat: write patient reports under VEDLEGG_APP"
```

### Task 2: Remove automatic report generation from searches

**Files:**
- Modify: `src/archer_processor/gui/app.py`
- Test: `tests/test_gui.py`

**Interfaces:**
- Database workers produce evidence only.
- `MainWindow._try_write_evidence_workbook(show_errors: bool) -> bool` remains the review-workbook checkpoint.

- [ ] **Step 1: Write negative side-effect tests**

Patch `PatientReportCoordinator` with a spy, complete API and browser worker runs, and assert the coordinator is never constructed or called. Assert the processed review workbook checkpoint still runs after each patient/provider completion.

- [ ] **Step 2: Run worker tests and confirm failure**

Run: `pytest tests/test_gui.py -q -k "worker and patient_report"`

Expected: current workers construct the coordinator and call `reconcile()`.

- [ ] **Step 3: Remove report coordination from both workers**

Delete coordinator construction and patient-report outcome emission from `DatabaseWorker.run()` and `BrowserReviewWorker.run()`. Keep evidence merge, progress, cancellation, and review-workbook writes unchanged.

- [ ] **Step 4: Run GUI worker and resume tests**

Run: `pytest tests/test_gui.py -q -k "worker or resume or checkpoint"`

Expected: PASS.

- [ ] **Step 5: Commit decoupling**

```bash
git add src/archer_processor/gui/app.py tests/test_gui.py
git commit -m "refactor: decouple reports from evidence searches"
```

### Task 3: Add explicit all/selected report generation

**Files:**
- Modify: `src/archer_processor/gui/app.py`
- Modify: `src/archer_processor/reports/patient_report_coordinator.py`
- Test: `tests/test_gui.py`
- Test: `tests/test_patient_report_coordinator.py`

**Interfaces:**
- Produces: `PatientReportWorker(coordinator, patient_ids)` with progress and finished signals
- Produces: `PatientReportCoordinator.write_patients(patient_ids: Sequence[str]) -> list[PatientReportOutcome]`
- Produces: `MainWindow._selected_patient_ids() -> list[str]`

- [ ] **Step 1: Write selection and summary tests**

Test no selection yields every completed patient; selecting two distinct patient rows yields only those two IDs; duplicate variant rows deduplicate the IDs. Feed outcomes `created`, `updated`, `locked`, and `failed` and assert the visible summary reports exact counts in Norwegian.

- [ ] **Step 2: Run explicit-generation tests and confirm failure**

Run: `pytest tests/test_gui.py tests/test_patient_report_coordinator.py -q -k "report and (selected or summary or generate)"`

Expected: explicit scope and status summary are absent.

- [ ] **Step 3: Implement dedicated report worker**

The button text is `Generer VEDLEGG_APP`. Disable it during generation, leave search controls independent, and pass selected patient IDs when present; otherwise pass all completed patient IDs. Emit one progress event per patient and one final list of outcomes.

- [ ] **Step 4: Render operator summary**

Use the exact categories `Opprettet`, `Oppdatert`, `Hoppet over/låst`, and `Feilet`. Include each failed/locked filename in the activity log without interrupting successful writes.

- [ ] **Step 5: Run GUI and coordinator tests**

Run: `pytest tests/test_gui.py tests/test_patient_report_coordinator.py -q`

Expected: PASS.

- [ ] **Step 6: Commit explicit report generation**

```bash
git add src/archer_processor/gui/app.py src/archer_processor/reports/patient_report_coordinator.py tests/test_gui.py tests/test_patient_report_coordinator.py
git commit -m "feat: generate patient reports on demand"
```

### Task 4: Consolidate button hierarchy and styling

**Files:**
- Modify: `src/archer_processor/gui/theme.py`
- Modify: `src/archer_processor/gui/app.py`
- Test: `tests/test_gui.py`

**Interfaces:**
- `PrimaryButton` is reserved for the next recommended action on each page.
- Destructive/interruption controls remain visually distinct and keyboard accessible.

- [ ] **Step 1: Write UI contract tests**

Assert primary text buttons have `minimumHeight() >= 44`, text/background combinations meet the existing high-contrast palette, `Generer VEDLEGG_APP` is not nested under search execution, and evidence controls remain reachable at 1120x720 without overlap.

- [ ] **Step 2: Run UI tests and confirm failure**

Run: `pytest tests/test_gui.py -q -k "button or layout or report"`

Expected: the current primary minimum height is 24 pixels and action hierarchy is crowded.

- [ ] **Step 3: Centralize shared QSS**

Move duplicated button, input, table, card, and status styles from `MainWindow._apply_style()` into `gui/theme.py`. Set primary and secondary text actions to a 44-pixel minimum height with readable padding. Keep compact icon-only controls at their existing accessible square size.

- [ ] **Step 4: Simplify the evidence action groups**

Group controls as `Søk`, `Gjenoppretting`, and `Rapporter`. Keep one filled primary action per group state; use secondary styling for pause, resume, workbook update, and retry-save operations. Preserve all object names used by tests and accessibility labels.

- [ ] **Step 5: Run UI tests at both target sizes**

Run: `pytest tests/test_gui.py -q`

Expected: PASS. Instantiate and resize the window to 1120x720 and 1440x900 in the layout test, process Qt events, and assert every action rectangle is inside its scroll viewport.

- [ ] **Step 6: Commit UI cleanup**

```bash
git add src/archer_processor/gui/theme.py src/archer_processor/gui/app.py tests/test_gui.py
git commit -m "style: clarify evidence and report actions"
```

### Task 5: Verify and document the manual workflow

**Files:**
- Modify: `README.md`
- Modify: `docs/clinical_workflow.md`

- [ ] **Step 1: Update workflow and output documentation**

Replace automatic-report wording with the explicit button flow and document the exact directory, filename, selection behavior, atomic overwrite, and locked-file result.

- [ ] **Step 2: Run complete tests**

Run: `pytest -q`

Expected: all tests pass.

- [ ] **Step 3: Perform a desktop smoke test**

At 1120x720 and 1440x900, load a synthetic completed review workbook, generate all reports, regenerate one selected patient after adding manual Kommentar/HSMD text, and confirm searches do not start.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md docs/clinical_workflow.md
git commit -m "docs: describe manual VEDLEGG_APP generation"
```
