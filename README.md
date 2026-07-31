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
- OncoKB: token-ready lookup when an API token is saved in Settings; otherwise the app records a prepared query.
- Franklin: live lookup when Premium API access is available through an API token or runtime email/password login; otherwise the app opens the exact public variant-review page. The password is not saved.
- MTBP: login-assisted visible-browser batch submission and report parsing. The app submits only gene/variant strings under a pseudonymous analysis ID, validates every returned row, and records pipeline/database versions. Its public instance is research-only.
- HSMD: prepared manual/login evidence link pending an API-enabled QIAGEN licence or an approved automation route.

Database searches run in parallel with a bounded worker count from the Databases tab.
The default is 3 workers.
gnomAD lookups are rate-limited internally and default to `gnomad_r2_1` for Archer GRCh37/hg19 coordinates.
When a database search starts, the log reports each selected source as ready, token required, manual, rate limited, or error.

## Login-based browser review

The Databases tab also provides a visible Microsoft Edge workflow for OncoKB,
Franklin and MTBP. HSMD has been removed from this active workflow for now and
remains a manual evidence source:

1. Choose a source under **Login-based sources**.
2. Click **Sign In / Refresh Session**.
3. Sign in on the provider's own page, including MFA when required, and close
   the Edge window when complete.
4. For MTBP, set the exact cancer type in Settings (the default is `Blood`).
5. Select the desired sources and click **Run Browser Lookups**.

The app uses a separate persistent Edge profile per provider under
`%USERPROFILE%\.archer-prosess\browser_profiles`. Passwords are never read or
saved by Archer Prosess. These profiles contain sensitive authenticated session
cookies and must not be copied, shared or committed to Git.

OncoKB, Franklin and MTBP have automated, fail-closed visible-page parsers. The
app saves a viewport screenshot and a JSON audit record beside the review
workbook, using a hash or pseudonymous batch ID instead of the sample ID in
artifact filenames. MTBP submits one de-duplicated batch, waits up to five
minutes for the report, and imports functional relevance, evidence category,
actionability tier, source links and version provenance. Never enter patient or
sample identifiers in the MTBP cancer-type setting or browser form.

## Run

```powershell
python -m pip install -e .[dev]
python -m archer_processor
```

## Test

```powershell
pytest
```
