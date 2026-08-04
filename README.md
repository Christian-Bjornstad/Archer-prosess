# VPM Tolkning

Clinical desktop workstation for somatic VPM variant interpretation, evidence research, and patient reporting. Archer Analysis TSV exports are supported as the input format.

The app is being rebuilt as a clean PyQt6 project with a stable processing core:

- validate and read Archer variant TSV exports
- apply local production filtering rules
- edit the saved artifact list from Settings
- compare variants with the yearly VPM history workbook
- prepare review-ready Excel workbooks
- support the focused evidence workflow: MTBP, Franklin, ClinVar, OncoKB, and COSMIC

Current database status:

- ClinVar: public website resolution followed by a focused browser capture of the variant title and germline/somatic classification summary.
- COSMIC: authenticated browser lookup by the Archer `COSMICID`, capturing the mutation Overview, Tissue distribution, and Samples filtered to `lymphoid`. The older NLM Clinical Tables v4 lookup remains in the service for basic/public records, but it does not provide these full website panels.
- OncoKB: signed-in website lookup using the serial Microsoft Edge workflow.
- Franklin: signed-in website lookup that explicitly searches GRCh37/hg19 in Somatic mode, verifies the returned variant identity, and imports only the suggested classification. It captures the complete Somatic Clinical Evidence scroller, then Computed Classification through De Novo Data (excluding Add More Evidence), followed by the prediction/population panels after their render buffer.
- MTBP: login-assisted visible-browser batch submission and report parsing. Transcript HGVS is tried first; entries rejected by MTBP are retried as GRCh37 genomic HGVS derived from Archer coordinates/ref/alt. The app submits only gene/variant strings under a pseudonymous analysis ID, validates every returned row, and records pipeline/database versions. Its public instance is research-only.

Evidence searches run patient-by-patient. For each patient, the selected
websites run serially, with MTBP kept last for operational safety;
only then does the next patient start again at the first selected source.
The **Included variants only** option is enabled by default, so excluded and
flagged records are not sent to external database websites unless the option is
deliberately cleared.
Website safety delays are randomized independently between
website variants, provider changes, and consecutive browser-only patients. It
defaults to 10-20 seconds, with editable minimum and maximum values in Settings.
MTBP always runs last for each patient and has a separately configurable 20-minute
default report timeout. Queue waiting checks both the live queue and the exact
pseudonymous entry in Reports List, recovering completed reports after transient
navigation stalls without resubmitting the analysis. Completed patient evidence is saved to the workbook during
the run so a later failure does not discard earlier patients.
When a database search starts, the log reports each selected source as a Microsoft Edge website lookup.

## Login-based browser review

The Databases tab also provides a visible Microsoft Edge workflow for COSMIC,
OncoKB, Franklin, ClinVar and MTBP:

The app starts the installed Microsoft Edge directly and controls it over the
localhost Edge DevTools Protocol (CDP) using a pure-Python WebSocket client. It
does not install or launch Playwright, Node.js, Selenium, Selenium Manager, or a
separate Edge WebDriver executable. Work-PC policy must allow the managed Edge
binary and the Edge `RemoteDebuggingAllowed` policy.

1. Choose a source under **Browser provider**.
2. Add COSMIC, OncoKB, Franklin and/or MTBP email/password in Settings. Passwords are encrypted
   by Windows Credential Manager and are never written to `config.json`.
3. Click **Sign In**. With saved credentials the app signs in
   and releases the Edge profile automatically. Manual login remains available.
4. Set the randomized inter-search delay range, MTBP report timeout, and exact
   MTBP cancer type in Settings (defaults: 10-20 seconds, 20 minutes, and `Blood`).
5. Select the desired sources and click **Run Browser Sources**.

The app uses a separate persistent Edge profile per provider under
`%USERPROFILE%\.archer-prosess\browser_profiles`. Login passwords are stored by
Windows Credential Manager under `Archer Prosess/<provider>`; plaintext
passwords are never written to project files or application JSON. Browser
profiles contain sensitive authenticated cookies and must not be copied, shared
or committed to Git.

COSMIC, OncoKB, Franklin, ClinVar and MTBP have automated, fail-closed visible-page parsers. COSMIC
uses the input `COSMICID` directly and captures three evidence panels; Franklin
uses the provider search form instead of constructing a genomic URL, which avoids
reverse-strand ref/alt errors, retries transient failures three times, and returns
only `[found] classification=<value>;`. It captures the complete internally
scrollable Somatic Clinical Evidence tab, followed by non-overlapping Computed
Classification sections ending after De Novo Data. It deliberately excludes Add
More Evidence. It also captures a tightly cropped assessment-tools view containing Predictions and
Population Frequencies. ClinVar captures only the title and classification-summary
region above Variant Details. OncoKB captures the variant overview and mutation-effect
view. The app saves screenshots and a JSON audit record beside the review workbook,
using a hash or pseudonymous batch ID instead of the sample ID in artifact filenames.
MTBP captures the exact alteration-centric evidence section for each safely matched
variant. MTBP runs after the other browser sources, submits one
de-duplicated batch, retries mapper failures using GRCh37 genomic HGVS, removes
only entries that MTBP still rejects, and waits up to the configured timeout for
the report. Generated reports remain available in MTBP until the app detects six
`ARCHER-` reports; it then deletes all six as one housekeeping batch. The same
safe cleanup runs before submission if MTBP's ten-report capacity is already full.
Local screenshots and audit evidence are written before any post-analysis cleanup,
and manually named reports are never automatically deleted. It imports functional relevance, evidence category, actionability
tier, source links and version provenance. Never enter patient or sample
identifiers in the MTBP cancer-type setting or browser form.

## Workbook report

The exported workbook opens with a compact summary dashboard, followed by an
**Included Variants** review table, an editable **Database Selection** sheet, and
a normalized **Database Evidence** table. Review the full variant list and place
`X` in **Skip Database Search (X)** for variants that should not be submitted;
then use **Load X Selections** in the app before collecting evidence.
Evidence rows use readable status coloring and include clickable links to the
source page and any browser screenshot captured during COSMIC, OncoKB, Franklin or MTBP
review. Screenshots remain beside the workbook instead of being embedded at
full resolution, which keeps large batch reports responsive. Keep the generated
`*_browser_evidence` folder beside the workbook when moving or archiving a
report so its relative screenshot links remain valid. The original
**With Artifacts** and **Artifacts Removed** Archer tables remain available as
the detailed audit/raw-data views, along with the applied rules and local
history when present. The workbook is rewritten automatically when the selected
database/browser phases finish; the manual rewrite button remains available if
the file was open in Excel during that save.

## Patient Excel reports

Use **Export Patient Excel Reports** to create one image-led workbook per DIT
identifier. Files are named `<DIT>_VPM_Tolkning.xlsx`. Each workbook contains
**Oversikt**, **Vedlegg**, and one sheet per selected variant. Oversikt contains
compressed findings and live database links; Vedlegg contains only the DIT number
for manual additions. Variant sheets embed screenshots in the order MTBP,
Franklin, ClinVar, OncoKB, and COSMIC. A unique gene uses the gene symbol as its
sheet name; repeated genes add the protein change, or the coding-DNA change when
protein information is unavailable.
The workbooks are written to `<workbook>_patient_excel_reports` and remain usable
without the separate screenshot folder because the images are embedded.

The COSMIC text panel summarizes the captured page while the main
database evidence cache retains the full source response for audit and later
report refinement.

## Run

```powershell
python -m pip install -e .[dev]
python -m archer_processor
```

## Test

```powershell
pytest
```
