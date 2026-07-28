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
- Franklin: live lookup when an API token is saved in Settings, or with Franklin email plus runtime password login; the password is not saved.
- MTBP, HSMD: prepared manual/login evidence links.

Database searches run in parallel with a bounded worker count from the Databases tab.
The default is 3 workers.
gnomAD lookups are rate-limited internally and default to `gnomad_r2_1` for Archer GRCh37/hg19 coordinates.
When a database search starts, the log reports each selected source as ready, token required, manual, rate limited, or error.

## Run

```powershell
python -m pip install -e .[dev]
python -m archer_processor
```

## Test

```powershell
pytest
```
