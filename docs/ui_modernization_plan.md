# UI modernization plan

## Goal

Make Archer Prosess feel like a modern clinical workbench: calm, trustworthy,
fast to scan, and explicit about what will be sent to external databases. The
visual redesign must not change filtering or evidence decisions implicitly.

## Proposed information architecture

1. **Prepare** – input TSV, output location, run date, validation, and processing.
2. **Review** – the complete variant table with filters, warnings, history, and a
   visible database-search decision for every row.
3. **Evidence** – provider cards, login/session state, selected-variant count,
   patient queue, progress, and source-specific errors.
4. **Reports** – reviewed workbook, patient Excel, patient PDF, and artifact folder.
5. **Settings** – credentials, safety delays, MTBP settings, and artifact rules.

A narrow left workflow rail should replace the current equal-weight tabs. The
main content area then has one clear primary action per screen.

## Visual system

- Background: `#F5F7FA`; surfaces: white; primary navy: `#163B5C`.
- Interactive blue: `#2F75B5`; clinical teal: `#0F766E`.
- Success: `#3F7D52`; warning: `#B7791F`; error: `#B42318`.
- Use color for status and hierarchy, never as the only status signal.
- Segoe UI at 14 px minimum for body text, with 20–24 px page titles.
- Eight-pixel spacing rhythm, 10–12 px corner radius, subtle borders, and very
  limited shadows.
- Maintain WCAG AA contrast and obvious keyboard focus indicators.

## Key screen changes

### Prepare

- Two large file cards with drag/drop affordance and recent-path memory.
- Inline validation results instead of modal-only feedback.
- A compact run summary beside the primary **Process TSV** button.

### Review

- Sticky filters for patient, gene, decision, warning, and history match.
- First column: **Skip database search** checkbox, mirroring the workbook `X`.
- A selected/searchable count that updates immediately.
- A side inspector for the active variant so the table remains compact.
- Explicit states: included/excluded, database search yes/no, and evidence status.

### Evidence

- One card per provider with availability, authentication, last result, and retry.
- Provider display order: MTBP, Franklin, ClinVar, OncoKB, COSMIC.
- A patient-by-patient queue with current provider and variant counts.
- Persistent progress details in the screen, with the raw log collapsed by default.

### Reports

- Separate cards for reviewed workbook, patient Excel, patient PDF, and evidence
  artifacts.
- Show destination and naming preview, for example
  `26OUM00004_VPM_Tolkning.xlsx`, before export.
- Provide **Open folder** after a successful export.

## Delivery phases

1. Extract colors, typography, spacing, button variants, status badges, and cards
   into reusable Qt style helpers. No workflow changes.
2. Add the left workflow rail and rebuild Prepare/Review while preserving signals.
3. Rebuild Evidence around provider cards and the patient queue.
4. Rebuild Reports and Settings, then remove legacy layout code.
5. Accessibility and usability pass: keyboard navigation, contrast, 125–150%
   display scaling, narrow-window behavior, and clinical-user walkthrough.

## Acceptance criteria

- A first-time user can process, review, select, search, and export without using
  the raw log as navigation.
- The app always shows how many variants will be submitted before search starts.
- Destructive or external actions state their scope clearly.
- Every screen has one visually dominant primary action.
- Status remains understandable in grayscale and to color-vision-deficient users.
- Existing processing and evidence tests remain unchanged or gain equivalent UI
  coverage before legacy widgets are removed.
