# Archer Prosess

Clinical desktop tool for processing Archer Analysis VPM TSV files.

The app is being rebuilt as a clean PyQt6 project with a stable processing core:

- validate and read Archer variant TSV exports
- apply local production filtering rules
- compare variants with the yearly VPM history workbook
- prepare review-ready Excel workbooks
- support database evidence workflows from ClinVar, gnomAD, COSMIC, OncoKB, MTBP, HSMD, Franklin and related manual sources

## Run

```powershell
python -m pip install -e .[dev]
python -m archer_processor
```

## Test

```powershell
pytest
```
