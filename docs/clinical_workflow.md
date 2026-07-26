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

- Exclude `NM_004119.2:c.1419-4dup`.
- Exclude `NM_004119.2:c.1419-4del`.
- Exclude `NM_004972.3:c.3291+16dup`.
- Exclude `NM_004972.3:c.3291+16del`.
- Exclude `NM_015338.5:c.1934dup` only when AF is below 4.5%.

Special review flags from the current clinical notes:

- ASXL1 missense variants.
- CEBPA variants in b-ZIP region.
- FLT3-ITD should be handled separately from SNP/InDel TSV processing.
- NPM1 type should be kept table-focused.
- Splice donor/acceptor consensus variants need careful classification.
- TP53 variants may need multihit or germline assessment, especially around high or 40-60% AF.
- SeqDirBias/sample strand bias and possible index hopping should be visible to the reviewer.

Database evidence sources:

- ClinVar can be queried through NCBI E-utilities.
- COSMIC is queried through the NLM Clinical Tables COSMIC endpoint for basic mutation evidence; the official COSMIC site may still require registration/licensing for full clinical review.
- gnomAD is queried through the public browser GraphQL endpoint when genomic location plus ref/alt alleles can be converted to a GRCh37 variant ID.
- OncoKB is token-based. Add the API token in Settings to enable live annotation; without a token the app records a prepared query and direct review link.
- MTBP, HSMD and Franklin currently require login/license/manual review. The app records prepared queries and direct review links.
- Evidence must be presented as support for human interpretation, not as automatic final classification.

Parallel search:

- Database searches run in a background GUI thread.
- Within that thread, variants are searched concurrently with a bounded worker pool.
- The default is 3 workers and the UI allows 1-8 workers.
- Use lower worker counts for public APIs without tokens; use higher counts only when local/network policy and API credentials allow it.
