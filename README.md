# Archer Prosess

Clinical desktop tool for processing Archer Analysis VPM TSV files.

The app is being rebuilt as a clean PyQt6 project with a stable processing core:

- validate and read Archer variant TSV exports
- apply local production filtering rules
- edit the saved artifact list from Settings
- compare variants with the yearly VPM history workbook
- prepare review-ready Excel workbooks
- support database evidence workflows from ClinVar, COSMIC, gnomAD, OncoKB, MTBP, HSMD, Franklin and related manual sources

Current database status:

- ClinVar: live lookup through NCBI E-utilities.
- COSMIC: live basic/public lookup through NLM Clinical Tables v4; full COSMIC review remains licensed.
- gnomAD: live GraphQL lookup when genomic coordinates and ref/alt are available.
- OncoKB: token-ready lookup when an API token is saved in Settings; otherwise the serial browser workflow can sign in using an OncoKB email/password stored in Windows Credential Manager.
- Franklin: supported API lookup when a Premium token is configured; otherwise a serial browser lookup searches by transcript HGVS, verifies the returned variant identity, and imports only the suggested classification. Anonymous Franklin access is limited, so saved browser login is recommended.
- MTBP: login-assisted visible-browser batch submission and report parsing. Transcript HGVS is tried first; entries rejected by MTBP are retried as GRCh37 genomic HGVS derived from Archer coordinates/ref/alt. The app submits only gene/variant strings under a pseudonymous analysis ID, validates every returned row, and records pipeline/database versions. Its public instance is research-only.
- HSMD: prepared manual/login evidence link pending an API-enabled QIAGEN licence or an approved automation route.

API/database searches run in parallel with a bounded worker count from the Databases tab.
The **Included variants only** option is enabled by default, so excluded and
flagged records are not sent to external database websites unless the option is
deliberately cleared.
After that phase, selected OncoKB, Franklin and MTBP sources without API access
continue automatically in the serial browser phase. Long runs are expected.
The browser delay defaults to 15 seconds between individual Franklin and OncoKB
searches. MTBP always runs last and has a separately configurable 20-minute
default report timeout.
The default is 3 workers.
gnomAD lookups are rate-limited internally and default to `gnomad_r2_1` for Archer GRCh37/hg19 coordinates.
When a database search starts, the log reports each selected source as ready, token required, manual, rate limited, or error.

## Login-based browser review

The Databases tab also provides a visible Microsoft Edge workflow for OncoKB,
Franklin and MTBP. HSMD has been removed from this active workflow for now and
remains a manual evidence source:

1. Choose a source under **Login-based sources**.
2. Add OncoKB, Franklin and/or MTBP email/password in Settings. Passwords are encrypted
   by Windows Credential Manager and are never written to `config.json`.
3. Click **Sign In / Refresh Session**. With saved credentials the app signs in
   and releases the Edge profile automatically. Manual login remains available.
4. Set the inter-search delay, MTBP report timeout, and the exact MTBP cancer
   type in Settings (defaults: 15 seconds, 20 minutes, and `Blood`).
5. Select the desired sources and click **Run Browser Lookups**.

The app uses a separate persistent Edge profile per provider under
`%USERPROFILE%\.archer-prosess\browser_profiles`. Login passwords are stored by
Windows Credential Manager under `Archer Prosess/<provider>`; plaintext
passwords are never written to project files or application JSON. Browser
profiles contain sensitive authenticated cookies and must not be copied, shared
or committed to Git.

OncoKB, Franklin and MTBP have automated, fail-closed visible-page parsers. Franklin
uses the provider search form instead of constructing a genomic URL, which avoids
reverse-strand ref/alt errors, retries transient failures three times, and returns
only `[found] classification=<value>;`. The
app saves a viewport screenshot and a JSON audit record beside the review
workbook, using a hash or pseudonymous batch ID instead of the sample ID in
artifact filenames. MTBP runs after the other browser sources, submits one
de-duplicated batch, retries mapper failures using GRCh37 genomic HGVS, removes
only entries that MTBP still rejects, and waits up to the configured timeout for
the report. It imports functional relevance, evidence category, actionability
tier, source links and version provenance. Never enter patient or sample
identifiers in the MTBP cancer-type setting or browser form.

## Workbook report

The exported workbook opens with a compact summary dashboard, followed by an
**Included Variants** review table and a normalized **Database Evidence** table.
Evidence rows use readable status coloring and include clickable links to the
source page and any browser screenshot captured during OncoKB, Franklin or MTBP
review. Screenshots remain beside the workbook instead of being embedded at
full resolution, which keeps large batch reports responsive. Keep the generated
`*_browser_evidence` folder beside the workbook when moving or archiving a
report so its relative screenshot links remain valid. The original
**With Artifacts** and **Artifacts Removed** Archer tables remain available as
the detailed audit/raw-data views, along with the applied rules and local
history when present. The workbook is rewritten automatically when the selected
database/browser phases finish; the manual rewrite button remains available if
the file was open in Excel during that save.

## Patient PDF reports

Use **Export Patient PDFs** after processing or evidence lookup to create one
PDF per DIT identifier with included variants. Patient grouping uses the sample
prefix before `_VPM_` and validates the expected `YYOUM#####` format, for example
`26OUM00000` for a patient first registered in 2026. Reports are written to a
dedicated `<workbook>_patient_reports` folder.

Each patient PDF contains a decision-support summary, included variant details,
AF and gnomAD AF, depth/AO, quality/caller, known IDs, warnings, normalized
database evidence, source/screenshot links, capture and pipeline provenance,
a clinical review checklist, a physician conclusion/signature area, and explicit
limitations. Patients with no included variants do not receive a PDF. The
reports do not make an automatic diagnosis or treatment recommendation, and any
MTBP evidence retains its academic-research-only limitation.

## Run

```powershell
python -m pip install -e .[dev]
python -m archer_processor
```

## Test

```powershell
pytest
```
