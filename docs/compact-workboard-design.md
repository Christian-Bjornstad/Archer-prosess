# Compact workboard

The approved direction is the compact workboard with persistent navigation on the left. The app stays a native PyQt6 workstation and retains the existing blue palette and Segoe UI; it does not require downloaded fonts or web assets. UI/UX Pro Max informed density, keyboard focus, progressive disclosure, and recovery placement. Marketing-oriented suggestions from its generated design system do not apply to this clinical workstation.

- Import exposes New analysis and Continue analysis on the first screen, then guides users through Excel review and loading X selections.
- Evidence gives the patient table the available space. One main search action describes its scope. Pause/Stop exist once, in the run status strip. Report actions stay visible below the scroll area. Technical browser controls are disclosed under Advanced.
- Patient checkboxes determine command scope. Row focus only opens details. With no checked patients, the report action explicitly says it applies to all patients.
- Each provider shows an honest access state and a direct sign-in/recheck action when needed. A reference-file failure does not block repairing login.
- Status messages distinguish lookup outcome, technical failure, and pending workbook saves. Source progress never implies a clinical interpretation.
- Settings group reference files, account access, and advanced controls. File selection status distinguishes saved configuration from edits.
- Use 4/8px spacing, legible 13px native body text, visible keyboard focus, plain Norwegian labels, and 44px text actions. Tables may scroll horizontally inside their own viewport. Test 920×600, 1024×640, 1440×900, and increased Windows scaling.

Report generation internals, clinical reference contents, and attachment appearance remain unchanged. Reference snapshots per analysis are a separate persistence change; this UI round does not claim to implement them.

Validation: 624 tests passed, Python compilation and Git whitespace checks passed. Synthetic native previews were reviewed at 920×600, 1024×640, 1440×900 and 125% Qt display scaling. Independent review reproduced and fixed credential-save/configuration ordering and an extensionless output-path alias that bypassed the recreation guard. Managed Edge and provider sign-in still need validation on the intended work PC; the active Citrix session was not interrupted.
