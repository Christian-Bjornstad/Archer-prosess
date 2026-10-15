# Historical Evidence Backup Implementation Plan

> **For agentic workers:** Use incremental implementation with focused agents and independent review; regressions precede production changes.

**Goal:** Supply exact-variant historical backup after unsuccessful provider lookups, visibly dated and retryable.

**Architecture:** A local SQLite store owns immutable successful snapshots and captures. Browser lookup records trusted successes; worker finalization applies fallback after retries and provider safety gates. Workbook loading can backfill validated original audits. Presentation shares one historical date/status helper.

**Tech Stack:** Python 3.11+, stdlib sqlite3, existing PyQt6/Pillow/openpyxl/reportlab.

**Spec:** `docs/superpowers/specs/2026-10-15-historical-evidence.md`

## Global Constraints

- Keep original clinical inputs and attachment layout unchanged.
- Use no external service or additional dependency for history.
- Store UTC; visibly display local date/time with timezone.
- Never turn a historical fallback into a fresh success or suppress current errors.
- Use synthetic fixtures only; keep clinical archives and replay data outside Git.

## Review Focus

- Current retry success takes precedence over history.
- Historical fallback cannot bypass MTBP retained-report handling or OncoKB pause.
- Missing/wrong genome, allele, gene, provider or MTBP context cannot reuse history.
- Reopening, saving or reusing a snapshot cannot refresh its capture time.
- Missing assets or SQLite failures cannot crash the current lookup.

### Task 1: Persistent exact-variant snapshots

Files: create `services/evidence_history.py`, `tests/test_evidence_history.py`.
Interfaces: `EvidenceHistoryStore(directory, *, clock=...)`,
`record(variant, evidence, *, context='', captured_at=None, assembly='GRCh37') -> bool`,
`fallback(variant, current, artifact_directory, *, context='', assembly='GRCh37') -> DatabaseEvidence`,
`current_attempt(evidence) -> DatabaseEvidence`, `EvidenceHistoryError`.

- [x] Write failing tests for matching, timestamp, asset retention, privacy,
  historical exclusion, provider/context separation and concurrency.
- [x] Implement trusted successful snapshots and exact-key lookup using
  parameterized SQLite; retain only clinical metadata and validated captures.
- [x] Verify the store tests and review no patient data crosses samples.

### Task 2: Background audit backfill

Files: `services/processed_workbook.py`, `tests/test_history_backfill.py`.
Interface: optional loader constructor `history_store=None`.

- [x] Reproduce backfill, original date preservation and untrusted audit rejection.
- [x] Import only post-migration completed found audits with qualified identity
  and an original aware timestamp. Treat history I/O errors as load warnings.
- [x] Verify loader, audit identity and backfill suites.

### Task 3: Current lookup integration

Files: `services/browser_review.py`, `services/evidence_audit.py`, `gui/app.py`,
new `tests/test_history_lookup_flow.py`.

- [x] Reproduce current retry precedence, fallbacks after failure, current MTBP
  report preservation, provider pause and historical explicit retry eligibility.
- [x] Add optional history injection to browser service/workers and record fresh
  success at publication/finalization. Add successful OncoKB identity metadata.
- [x] Decorate final patient results after retries/safety gates; persist the same
  decorated result delivered to checkpoints. Unwrap current attempts on MTBP
  resume. Wire the GUI's shared lazy local store and background backfill.
- [x] Verify focused lookup, GUI retry, MTBP and restoration tests.

### Task 4: Dated presentation and exports

Files: new `core/evidence_display.py`; status model/matrix and evidence dialogs;
workbook and patient report writers; `tests/test_historical_presentation.py`.

- [x] Reproduce historical status/date disappearing behind classifications.
- [x] Show amber text and original local date/time, including report summaries;
  keep historical classification distinct from current evidence.
- [x] Verify workbook roundtrip, PDF/Excel text and native compact UI preview.

### Task 5: Review and delivery

- [x] Independently review exact identity, timestamp provenance, privacy,
  immutable assets, retry/resume gates and report semantics.
- [x] Run full pytest, compile and diff checks; record actual results.
- [ ] Commit and push `feat/historical-evidence-backup`; open and attach a PR
  based on the existing reliability branch so the feature diff stays focused.

## Verification outcome

Final source validation: **971 tests passed**, with two existing Pillow deprecation warnings. Compile and diff checks passed. Native PyQt previews at 920/1440 pixels and synthetic PDF pages were visually inspected. Independent review covered identity, provenance, privacy, report protection and final SQLite lock ordering without remaining actionable findings.
