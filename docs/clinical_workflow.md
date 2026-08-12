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
- Exclude `NM_015338.5:c.1934dup` as an artifact through 5.5% AF; retain it above 5.5%.

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
- OncoKB is reviewed in the signed-in web interface; cookie overlays are rejected before capture.
- Franklin uses the signed-in web interface with explicit hg19 and Somatic selection. It tries transcript HGVSc first, then the exact `chr-position REF>ALT` genomic form only when needed and verifies the returned variant identity.
- Franklin captures Computed Classification (ACMG and Oncology cards), Predictions, and Population Frequencies. Dynamic panels are validated and receive a one-time five-second incident retry when incomplete.
- MTBP submits one pseudonymous variant per report, records no personal report link, and retries a detached/hidden screenshot target once after rediscovering the exact report row.
- Evidence must be presented as support for human interpretation, not as automatic final classification.

The processed workbook includes a **Database Selection** sheet containing every
variant. Mark unwanted rows with `X` in **Skip Database Search (X)**, reload that
workbook in the Databases tab, and only the remaining variants are submitted.
Selections are matched back to the same Archer run using Sample plus HGVSc.

Search and recovery behavior:

- Evidence collection and processed-workbook restoration run in background GUI threads.
- The operations cockpit keeps the current patient, provider, action,
  patient/provider matrix, and timestamped activity visible during long runs.
- Matrix states are Queued, Running, Complete, Not found, Retry, Skipped, Report
  saved, Save pending, and Not ready.
- Recent-analysis recovery is local and passive: startup can offer the last
  workbook, but it never opens Edge or contacts a provider until the operator
  explicitly starts or resumes evidence collection.
- **Retry Pending Saves** retries report writing only; it does not restart an
  evidence provider.
- Completed source results are restored from one indexed audit-directory scan.
- Errors, timeouts, identity mismatches, partial captures, and unverified legacy ClinVar results remain resumable.
- Patient workbooks are written beside the processed workbook as each patient completes; an Excel file lock is nonfatal and is retried at reconciliation.
- Priority colouring uses artifact precedence, strong green for `Tier I + Tier II > 5`, and strong/weak green for `Germ > 10` at AF `>=35%` / `<35%` respectively.
