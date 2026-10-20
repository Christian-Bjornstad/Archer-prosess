# Workstation and evidence improvements

The user delegated implementation and routine design decisions while away.
The existing Citrix run must remain untouched, and attachment/report generation
and layouts must remain unchanged. Work happens in an isolated checkout based
on the latest local application branch (`dc32d9d`).

## Intended result

- A clearer import page: local TSV/workbook drop target, separate new/resume
  paths, a visible current workbook and a direct route to Evidence after review.
- A denser Evidence workspace that fits smaller workstation windows. Source
  selection stays explicit, with patient search and report actions available.
- One asynchronous **Sjekk innlogging** action checks all five provider profiles.
  It reports authenticated, public, login required, unknown, or failed separately.
  It never submits variants, enters credentials, or runs while another operation
  owns browser profiles. Results include when the check was performed.
- Live patient cells retain selection, focus and scrolling, distinguish sources
  outside the queue, and show incremental results from parallel browser lanes.
- Provider retries require evidence of a transient failure. Loading pages are
  never final negative results; explicit identity conflicts remain rejected.
- Managed Edge startup diagnoses policies that prevent remote debugging or
  override isolated profiles, with actionable messages and no policy changes.

## Boundaries and verification

Keep direct installed Edge/CDP, GRCh37 and somatic identity safeguards, existing
credential storage and existing report contracts. Do not connect to the active
Citrix session or submit live data. Use synthetic fixtures, regression tests,
the complete existing pytest suite and offscreen Qt renders at workstation
sizes. Live work-PC/provider validation is a remaining environmental check.

Baseline: 366 tests passed (two existing Pillow deprecation warnings).
