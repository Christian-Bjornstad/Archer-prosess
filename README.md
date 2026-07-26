# Archer Prosess

Clinical desktop tool for processing Archer Analysis VPM TSV files.

The app is being rebuilt as a clean PyQt6 project with a stable processing core:

- validate and read Archer variant TSV exports
- apply local production filtering rules
- compare variants with the yearly VPM history workbook
- prepare review-ready Excel workbooks
- support database evidence workflows from ClinVar, COSMIC, gnomAD, OncoKB, MTBP, HSMD, Franklin and related manual sources

Current database status:

- ClinVar: live lookup through NCBI E-utilities.
- COSMIC: live basic lookup through NLM Clinical Tables.
- gnomAD: live GraphQL lookup when genomic coordinates and ref/alt are available.
- OncoKB: live lookup when an API token is saved in Settings.
- MTBP, HSMD, Franklin: prepared manual/login evidence links.

Database searches run in parallel with a bounded worker count from the Databases tab.
The default is 3 workers.
gnomAD lookups are rate-limited internally and default to `gnomad_r2_1` for Archer GRCh37/hg19 coordinates.

## Run

```powershell
python -m pip install -e .[dev]
python -m archer_processor
```

## Test

```powershell
pytest
```
