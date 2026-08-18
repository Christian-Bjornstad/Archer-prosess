# Myolid Tolkning desktop workbench

This native PyQt override replaces the web-oriented landing-page guidance in
the master design system.

## Product direction

- Present the application as a clinical variant interpretation console, not an
  Archer processing utility or a step-by-step workflow wizard.
- Use Myolid Tolkning branding and analysis language throughout the interface.
- Somatic and hg19 / GRCh37 remain implementation requirements, but are not
  repeated as decorative profile labels in the interface.
- Evidence order remains MTBP, Franklin, ClinVar, OncoKB, and COSMIC.

## Layout

- Persistent 226 px midnight-navy sidebar with Import, Variants, Evidence, and
  Settings destinations.
- Compact header, 58 px horizontal metric cards, and a single task surface.
- Use command bars for primary actions, table toolbars for filtering, and
  source cards for database selection.
- Avoid numbered instruction cards, workflow arrows, duplicated explanations,
  and oversized empty activity surfaces.
- Keep the sidebar limited to brand and navigation; do not add workflow,
  reference-profile, local-processing, or credential-storage footers.
- Provider configuration is website-only. Do not expose API token controls or
  API readiness diagnostics in the application.
- Patient output actions are Excel workbooks only; do not show PDF export.
- The Import page offers two clearly separated paths: create a new review from
  TSV as the primary action, or resume an existing processed VPM workbook in a
  compact secondary card. A successful resume confirms what was restored and
  moves the user directly to Variants.

## Visual language

- Native Segoe UI, dense 8/12/16/24 px rhythm, white analysis surfaces on
  `#F3F7F9`, midnight navy `#08283A`, teal `#087EA4`, and evidence green
  `#18794E`.
- Labels and controls use transparent backgrounds unless they represent a
  deliberate chip, state, or input surface.
- The application icon represents variant analysis and evidence synthesis. It
  must fill all four corners with opaque navy and must not use Archer, archery,
  an A-shaped mark, or a white outer background.

## Interaction

- Primary actions remain textual, keyboard reachable, and at least 44 px high.
- Long evidence searches show patient-level determinate progress plus the
  active patient, variant count, and source count.
- Place a textual red-outline **Stop Search** control beside the evidence run
  action. Keep it disabled while idle, change it to **Stopping…** after use,
  interrupt cooperatively, and retain completed evidence with a persistent
  stopped-state explanation.
- Source selection, state, and warnings are communicated by text as well as
  colour.
- The Variants page shows only total, included, and excluded counts. Do not add
  a separate review-flags metric, filter, note, or workflow state.
- The review workbook uses two data sheets: With Artifacts and Artifacts Removed.
  Selection happens in the first sheet through Skip Database Search (X).
