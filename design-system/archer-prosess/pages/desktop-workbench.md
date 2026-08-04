# Desktop workbench override

This page specification overrides the web-oriented content pattern and font
pairing in `MASTER.md` for the PyQt application.

## Structure

- Persistent 246 px navy sidebar with four workspace destinations: Prepare,
  Review, Evidence, and Settings.
- One selected destination at a time, identified by text, number, and a filled
  blue state rather than color alone.
- Compact page heading, always-visible run metrics, and one stacked workspace
  page in the main content area.
- Evidence order remains MTBP, Franklin, ClinVar, OncoKB, and COSMIC.

## Desktop visual system

- Use native Segoe UI for reliable Windows rendering and system text scaling.
- Primary `#0284C7`, sidebar/foreground `#0C4A6E`, background `#F0F7FB`,
  panels `#FFFFFF`, borders `#C9DFEA`.
- Keep dense 8/12/16/24 px spacing, subtle borders, and 7-11 px radii.
- Avoid motion and hover-dependent actions; every action remains keyboard
  reachable with an explicit focus border.
- Keep status labels textual in addition to semantic color.

## Interaction rules

- Navigation order follows the visible sidebar from top to bottom.
- Async actions disable their initiating controls and retain visible progress.
- No external evidence search is initiated from navigation alone.
- Maintain a minimum 44 px target height for primary navigation and actions.
