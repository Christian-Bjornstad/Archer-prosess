# VPM Tolkning Operations Cockpit Redesign

**Date:** 2026-08-12
**Branch:** `redesign/status-workspace`
**Status:** Approved design direction; awaiting written-spec review

## Objective

Redesign VPM Tolkning as a compact clinical operations console that makes long
evidence searches easy to start, monitor, interrupt, resume, and audit. Preserve
the existing application icon, clinical terminology, provider order, search
behavior, and patient-report workflow.

The redesign must remain a native PyQt6 desktop interface suitable for a managed
Windows workstation. It must not introduce Node.js, browser UI frameworks, new
network services, or decorative features unrelated to variant interpretation.

## Design principles

- Prioritize operational state and recovery over workflow decoration.
- Communicate every state with text and shape or icon, never colour alone.
- Use a dense 8/12/16/24 px rhythm and keep useful content above the fold.
- Preserve the midnight-navy, teal, white, and evidence-green visual language.
- Keep one clear primary action per surface.
- Show recovery actions beside the problem they resolve.
- Avoid gradients, glass effects, oversized headings, excessive card grids, and
  animated decoration.
- Preserve keyboard access, visible focus indicators, 44 px primary controls,
  and readable contrast.

## Information architecture

The application retains four destinations, but removes numbered navigation and
replaces it with concise icon-and-label entries:

1. **Import** — start a new analysis or restore a recent processed workbook.
2. **Variants** — inspect, filter, and prioritize the loaded variants.
3. **Evidence** — configure and monitor database searches and report output.
4. **Settings** — manage files, browser access, pacing, and artifact rules.

The existing VPM icon remains at the top of the sidebar. The sidebar contains no
reference-profile, workflow, or credential-storage copy. A small analysis-state
area at the bottom may show the loaded workbook name and local state, but never
patient identifiers or credentials.

## Application shell

### Header

Each page has a compact header with page title, one-line purpose, and a persistent
status pill. The pill uses these canonical states:

- `Ready`
- `Loading workbook`
- `Running`
- `Pausing`
- `Paused`
- `Stopping`
- `Interrupted · resume available`
- `Complete · retry available`
- `Complete`
- `Report save pending`

The state pill is the single source of truth for the application state. Existing
ad-hoc stylesheet assignments are replaced by semantic state properties so that
appearance and wording cannot drift apart.

### Persistent run strip

When an evidence run exists, a compact strip remains visible above the current
page content. It contains:

- current patient index and pseudonymous DIT identifier;
- current provider and variant;
- patient progress and elapsed time;
- selected-source progress;
- Pause/Resume and Stop actions while active;
- Resume Incomplete Search after interruption;
- a short recovery message for retry or report-save states.

The strip collapses to a one-line summary after completion and can be expanded to
review the final counts. It does not obscure page content.

## Import workspace

The Import page uses two clearly separated paths:

### New analysis

The primary panel contains the TSV path, output workbook path, run date, and
Create Review Workbook action. Validation feedback appears directly below the
relevant path rather than only in a modal or log.

### Resume analysis

A secondary panel lists the most recent valid processed workbook, its modified
time, number of variants, and whether resumable evidence exists. The application
stores only the local workbook path and display metadata.

If the previous application session ended during a search, startup shows
`Interrupted · resume available` with two actions:

- **Restore analysis** — load the workbook and local audit data only.
- **Dismiss** — leave the current session empty.

Restoration never contacts provider websites and never automatically restarts a
search. Missing or moved workbooks produce a local recovery message and a Browse
action.

## Variant workspace

The Variants page emphasizes the table rather than metric cards.

- A compact toolbar contains search, decision filter, visible count, and clear
  filter action.
- Total, included, and excluded counts appear as small inline counters.
- Row status combines label and colour for included, excluded, artifact, strong
  priority, and weak priority.
- Column widths remain practical for gene, HGVSc, protein, AF, decision, history,
  and warnings.
- Empty and filtered-empty states explain how to restore rows.
- The table remains keyboard navigable and supports copying relevant values.

No review-flags feature, workflow stage, or additional clinical classification is
introduced.

## Evidence operations cockpit

The Evidence page becomes the main monitoring surface.

### Queue command bar

The command bar shows selected variants and providers, pending lookup count, and
the primary Run or Resume action. Pause/Resume and Stop are adjacent only while a
run is active. Browser sign-in is a secondary action and cannot visually compete
with Run.

### Provider selection

Providers remain ordered MTBP, Franklin, ClinVar, OncoKB, COSMIC. Each provider
row shows:

- selected state;
- purpose;
- local browser-session state when known;
- complete, pending, retry, or unavailable count for the loaded analysis.

Provider cards use compact rows rather than large tiles. A provider error offers
a specific Sign in or Retry action where appropriate.

### Current activity

The current-activity panel is the visual anchor during a run. It shows:

- patient `n / total`;
- DIT identifier;
- provider;
- current variant label;
- elapsed run time;
- current action, such as navigating, waiting, capturing, or writing report;
- determinate patient progress and overall patient progress.

The panel uses plain language and does not expose credentials, CDP internals, or
remote personal report URLs.

### Patient and provider status matrix

Rows represent patients and columns represent the five providers plus Report.
Cells use a short label and icon:

- `Queued`
- `Running`
- `Complete`
- `Not found`
- `Retry`
- `Stopped`
- `Skipped`
- `Report saved`
- `Save pending`

The matrix is derived from loaded variants, selected sources, canonical evidence
statuses, and patient-report outcomes. It is not a second persistence system.
Selecting a cell filters the activity timeline to that patient and provider.

### Activity timeline

The existing timestamped log is presented as a collapsible timeline with filters
for All, Warnings, and Errors. Each entry contains time, patient, provider, and a
concise message. Long technical details remain available through an expandable
details view rather than occupying the main surface.

### Evidence result table

The evidence matrix remains available below monitoring, but its cells use compact
status summaries rather than full links or long evidence text. Double-clicking a
cell opens the existing detail behavior. Result links stay in Excel reports, not
inside screenshot captions.

## Reports and save states

Patient workbooks continue to be generated automatically beside the processed
workbook after each selected patient completes.

The Report column and completion summary show:

- `Saved` with output filename;
- `Save pending · close in Excel`;
- `Not ready` while required work remains;
- `Retrying save` during reconciliation.

A locked report is nonfatal. The UI exposes **Retry Pending Saves** without
restarting provider searches. The manual Create Patient Workbooks action remains
available as a secondary recovery/export action.

## Settings workspace

Settings are grouped into four compact sections:

1. **Local files** — history workbook, output directory, recent-analysis behavior.
2. **Browser access** — provider credentials and sign-in controls.
3. **Search pacing** — request delay range, MTBP timeout, background Edge mode.
4. **Artifact rules** — editable HGVSc catalog and AF exception.

Sections use clear labels and inline validation. API controls, reference-profile
copy, credential-storage explanations, and PDF options remain absent.

## State and data flow

The redesign introduces a presentation-only run-status model assembled from
existing worker signals and evidence records. The model contains:

- application phase;
- active patient, provider, and variant;
- patient and provider totals;
- canonical per-source evidence status;
- report outcome per patient;
- start time and elapsed time;
- latest concise activity message.

Workers continue to own provider execution. UI widgets consume signals and render
the model. Widgets never infer clinical evidence or mutate provider results.

Recent-analysis metadata stores only the last processed workbook path and local
timestamps in the existing settings JSON. On restoration, the workbook loader and
audit index remain authoritative. No patient evidence is duplicated into config.

## Failure and recovery behavior

- Application or PC interruption: restore the last workbook locally and show
  incomplete sources; do not automatically contact providers.
- Provider failure: mark only that patient/source as Retry and continue when safe.
- Workbook/report file lock: show Save pending with a direct retry action.
- Missing recent workbook: remove it from the quick-resume surface after informing
  the user and offer Browse.
- Malformed workbook or audit: keep the app usable and show the local file that
  needs attention.
- Stop: retain all completed audit records and show Resume Incomplete Search.
- Pause: preserve the exact worker queue and show the current safe checkpoint.

## Visual system

- Keep the current icon unchanged.
- Use Segoe UI because this is an offline native Windows application.
- Use midnight navy for navigation and primary text, teal for active operations,
  green for verified completion, amber for waiting/retry, and red for errors/stop.
- Use white content surfaces on a cool gray-blue background.
- Use one radius scale: 6 px controls, 8 px panels, 12 px status surfaces.
- Use borders and spacing before shadows; reserve subtle shadow for overlays.
- Use a consistent simple outline icon family rendered as local vector paths or
  Qt standard icons. Every icon is paired with visible text except conventional
  disclosure controls.

## Accessibility and interaction

- Normal text meets at least 4.5:1 contrast.
- Interactive controls have visible keyboard focus and logical tab order.
- Primary controls are at least 44 px high; compact table controls remain at least
  32 px high with sufficient spacing.
- Status is never colour-only.
- Progress updates use concise text without repeatedly stealing keyboard focus.
- The interface remains usable at the existing 1120 × 720 minimum and at
  1440 × 900 without horizontal page scrolling.
- Motion is limited to native progress and brief state transitions; reduced-motion
  environments do not lose information.

## Architecture boundaries

The redesign should reduce pressure on the existing large `gui/app.py` file.
Reusable presentation components and status mapping belong in focused modules,
for example:

- `gui/status_model.py` — semantic application, source, and report states;
- `gui/widgets/run_status.py` — persistent run strip and current activity;
- `gui/widgets/status_matrix.py` — patient/provider matrix;
- `gui/widgets/navigation.py` — sidebar navigation;
- `gui/theme.py` — palette, spacing, typography, and semantic QSS.

`MainWindow` remains the coordinator and existing worker/service APIs remain the
execution boundary. Refactoring is limited to UI responsibilities required by the
redesign.

## Testing and verification

Automated tests must cover:

- semantic state-to-label/style mapping;
- interrupted-session quick-resume metadata without provider contact;
- matrix aggregation for complete, retryable, skipped, and report-pending states;
- report-save retry without database rerun;
- worker progress updating the active patient/provider display;
- keyboard-accessible actions and expected enabled states;
- existing stop, pause, resume, workbook-loading, and report-generation behavior;
- minimum-size layout construction without errors.

Visual verification must render all four pages at 1120 × 720 and 1440 × 900,
including Ready, Running, Paused, Interrupted, Complete with retry, and Save
pending states. The existing full test suite must pass before merge.

## Non-goals

- No changes to evidence parsing or clinical matching rules.
- No automatic provider search on startup.
- No new API integrations or background services.
- No dark mode in this redesign.
- No charts that duplicate the status matrix.
- No replacement of the existing application icon.
- No PDF output or review-flags workflow.
