# Clinical Workflow Notes

This project is a clinical workbench for Archer Analysis VPM TSV exports.

Core workflow:

1. Load `all_samples_filtered_variants.tsv` or equivalent Archer TSV export.
2. Validate required columns before processing.
3. Apply local artifact/filter rules.
4. Prioritize rows from Archer `Tier I`, `Tier II`, `Germ`, and AF values.
5. Review included, excluded, and warning-flagged variants.
6. Search or record external database evidence.
7. Export an Excel workbook for review and interpretation.

Initial production rules:

- Exclude configured artifact variants. The default catalog combines DNA
  Fragmentering v2 with the v1-only CEBPA entries `NM_004364.4:c.288C>G`,
  `NM_004364.4:c.280G>C`, and `NM_004364.4:c.296G>C`.
- Exclude `NM_015338.5:c.1934dup` through 5.5% AF. Display it strong orange at
  AF `<=5.0%`, light orange at AF `>5.0%` and `<=5.5%`, and retain it above
  5.5%.

The artifact list can be reviewed and edited in Settings. Use Reset Defaults to restore the current default artifact list.

The review workbook keeps AF numeric, displays it as a percentage, and sorts
variants by descending AF within each patient, with missing AF last. Patient
overview regeneration preserves the manual `Kommentar` cell and the manual
`HSMD -` line by patient and variant identity rather than row number.

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
- MTBP submits one pseudonymous combined report per patient. Transcript-qualified
  HGVSc is tried first; only entries explicitly rejected by MTBP are replaced by
  GRCh37 genomic notation before the complete patient batch is resubmitted. The
  full report is captured once for `Vedlegg`, while exact variant rows/cards are
  cropped locally for their variant sheets without additional portal searches.
  No personal report link is recorded. After verified local evidence is saved,
  the exact `ARCHER-` report is deleted from the portal. Before a new submission,
  remaining app-generated `ARCHER-` reports are cleared so they cannot fill the
  five-report limit; manually named reports are left untouched.
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
- Patient workbooks are written beside the processed workbook during final reconciliation; an Excel file lock is nonfatal and can be retried.
- Priority colouring uses artifact precedence and strong/weak green only for `Germ > 10` at AF `>=35%` / `<35%` respectively. Tier I and Tier II do not affect row colour.
