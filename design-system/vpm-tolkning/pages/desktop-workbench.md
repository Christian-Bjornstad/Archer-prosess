# VPM Tolkning desktop workbench

This native PyQt override replaces the web-oriented landing-page guidance in
the master design system.

## Product direction

- Present the application as a clinical variant interpretation console, not an
  Archer processing utility or a step-by-step workflow wizard.
- Use VPM Tolkning branding and analysis language throughout the interface.
- The fixed reference profile is always visible as `Somatic · hg19 / GRCh37`.
- Evidence order remains MTBP, Franklin, ClinVar, OncoKB, and COSMIC.

## Layout

- Persistent 226 px midnight-navy sidebar with Import, Variants, Evidence, and
  Settings destinations.
- Compact header, 58 px horizontal metric cards, and a single task surface.
- Use command bars for primary actions, table toolbars for filtering, and
  source cards for database selection.
- Avoid numbered instruction cards, workflow arrows, duplicated explanations,
  and oversized empty activity surfaces.

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
- Source selection, state, and warnings are communicated by text as well as
  colour.
