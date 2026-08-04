# Clinical Workflow Notes

This project is a clinical workbench for Archer Analysis VPM TSV exports.

Core workflow:

1. Load `all_samples_filtered_variants.tsv` or equivalent Archer TSV export.
2. Validate required columns before processing.
3. Apply local artifact/filter rules.
4. Compare variants against the yearly history workbook, normally `2026_VPM_Variantfunn.xlsx`.
5. Review included, excluded, warning-flagged, and history-matched variants.
6. Search or record external database evidence.
7. Export an Excel workbook for review and interpretation.

Initial production rules:

- Exclude configured artifact variants. The default artifact list contains `NM_004119.2:c.1419-4dup`, `NM_004119.2:c.1419-4del`, `NM_004972.3:c.3291+16dup`, and `NM_004972.3:c.3291+16del`.
- Exclude `NM_015338.5:c.1934dup` only when AF is below 4.5%.

The artifact list can be reviewed and edited in Settings. Use Reset Defaults to restore the current default artifact list.

Special review flags from the current clinical notes:

- ASXL1 missense variants.
- CEBPA variants in b-ZIP region.
- FLT3-ITD should be handled separately from SNP/InDel TSV processing.
- NPM1 type should be kept table-focused.
- Splice donor/acceptor consensus variants need careful classification.
- TP53 variants may need multihit or germline assessment, especially around high or 40-60% AF.
- SeqDirBias/sample strand bias and possible index hopping should be visible to the reviewer.

Database evidence sources:

- ClinVar is resolved through NCBI E-utilities and the browser captures only the variant title plus germline/somatic classification summary above Variant Details.
- COSMIC is opened by the input `COSMICID` in a signed-in browser. The app captures Overview, Tissue distribution, and a Samples table filtered to `lymphoid`. The NLM Clinical Tables v4 endpoint remains only as a basic/public fallback and does not contain the full panels. Confirm that the organisation's COSMIC licence permits patient-care reporting before clinical deployment.
- OncoKB is token-based. Add the API token in Settings to enable live annotation; without a token the app records a prepared query and direct review link.
- Franklin is token-based. Add the API token in Settings, or enter Franklin email plus runtime password for login during the current app session. The password is not saved to config. Without credentials the app records a prepared query and direct review link.
- Franklin evidence should be reviewed for computed ACMG classification, ACMG rule triggers, population frequency, REVEL/aggregated prediction data, ClinVar evidence, and transcript match.
- MTBP and HSMD currently require login/license/manual review. The app records prepared queries and direct review links. The 26OUM10350 manual report suggests recording classification, MTBP functional relevance/evidence category, HSMD actionability tier, clinical review status, population frequency, references, and notes.
- Evidence must be presented as support for human interpretation, not as automatic final classification.

The processed workbook includes a **Database Selection** sheet containing every
variant. Mark unwanted rows with `X` in **Skip Database Search (X)**, reload that
workbook in the Databases tab, and only the remaining variants are submitted.
Selections are matched back to the same Archer run using Sample plus HGVSc.

Parallel search:

- Database searches run in a background GUI thread.
- Within that thread, variants are searched concurrently with a bounded worker pool.
- The default is 3 workers and the UI allows 1-8 workers.
- Use lower worker counts for public APIs without tokens; use higher counts only when local/network policy and API credentials allow it.
