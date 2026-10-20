# Workstation Improvements Implementation Plan

> **For agentic workers:** Use focused parallel workers for independent service
> changes and a final independent whole-branch review.

**Goal:** Make importing, evidence search and browser readiness clearer and more reliable.

**Architecture:** Keep the PyQt desktop and direct Edge CDP backend. Add a small
session-check service/Qt worker and improve existing status and import components.

**Tech Stack:** Python 3.11+, PyQt6, installed Microsoft Edge, pytest.

**Spec:** ../specs/2026-10-07-workstation-improvements-design.md

## Global constraints

- Do not touch the existing Citrix/browser run.
- Do not change attachment/report code or layout.
- No credential/policy changes or live variant submissions during development.
- Preserve identity verification, somatic workflow and GRCh37.

## Review focus

- Loading/empty result pages: unfinished rendering remains retryable.
- Wrong variant identity: explicit genomic conflicts reject a textual match.
- Concurrent browser/profile access: readiness checks are disabled during work.
- Changing evidence/scope: preserve cell focus/selection and truthful source state.
- Small work-PC windows: primary actions fit and content remains scrollable.

## Tasks

- [x] Provider reliability: regression tests, semantic readiness and bounded
  classification-aware retry fixes in browser_review.py (search worker).
- [x] Edge/session readiness: policy/profile diagnostics and
  BrowserSessionCheckService.check_all(on_result=...) in new service modules
  and edge_cdp.py, with isolated browser doubles (Edge worker).
- [x] Patient table: incremental StatusMatrix.set_rows, retain current cell,
  selection and both scroll axes, with GUI regression tests (table worker).
- [x] UI integration: asynchronous session results, queue/scope semantics,
  compact source selection, safe import/drop paths and loaded-workbook guidance;
  write failing behavioral tests before implementing (root).
- [x] Verification: complete pytest suite, compile check, synthetic offscreen
  screenshots at small/normal workstation sizes, independent diff review.
- [x] Delivery: logical commits, push new feature branch, create draft PR and
  attach it to this chat. Keep main and the active checkout untouched.

## Progress

- Baseline complete: 366 passed, two existing Pillow deprecation warnings.
- Parallel investigations identified premature negative results, ineffective
  retry classification and full-table rebuilding. All fixes are implemented.
- Final suite: 466 passed, two existing Pillow deprecation warnings. Python
  compilation and whitespace checks passed. Synthetic previews were inspected
  at 1024×640 and 1440×900; narrow-window behavior was also reviewed at 920×600.
- Independent review: no remaining actionable findings; 113 focused tests
  passed. ClinVar rendering, cross-process Edge profile exclusion and readable
  narrow-window table columns have regression coverage.
- Delivery: published `feat/workstation-improvements-2026-10-07` and attached
  [draft PR #9](https://github.com/Christian-Bjornstad/Archer-prosess/pull/9),
  targeting the existing `feat/app-ui-settings-cleanup` baseline. The original
  checkout remains unchanged. Live managed-workstation validation remains
  outside this test run.
