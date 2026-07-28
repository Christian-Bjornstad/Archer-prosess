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

- ClinVar can be queried through NCBI E-utilities.
- COSMIC is queried through the NLM Clinical Tables COSMIC v4 endpoint for basic/public mutation evidence; the official COSMIC site may still require registration/licensing for full clinical review.
- gnomAD is queried through the public browser GraphQL endpoint when genomic location plus ref/alt alleles can be converted to a GRCh37 variant ID.
- For Archer VPM data, default gnomAD dataset is `gnomad_r2_1` because Archer Analysis exports hg19/GRCh37 coordinates. Use `gnomad_r4` only when the variant coordinates are known to be GRCh38.
- gnomAD evidence should be interpreted as population-frequency context: aggregated AF, exome AF, genome AF, max population AF, homozygote/hemizygote counts, filters, and direct browser URL.
- gnomAD requests are rate-limited by the app to respect the public limit of 10 requests per IP per 60 seconds.
- OncoKB is token-based. Add the API token in Settings to enable live annotation; without a token the app records a prepared query and direct review link.
- Franklin is token-based. Add the API token in Settings, or enter Franklin email plus runtime password for login during the current app session. The password is not saved to config. Without credentials the app records a prepared query and direct review link.
- Franklin evidence should be reviewed for computed ACMG classification, ACMG rule triggers, population frequency, REVEL/aggregated prediction data, ClinVar evidence, and transcript match.
- MTBP and HSMD currently require login/license/manual review. The app records prepared queries and direct review links. The 26OUM10350 manual report suggests recording classification, MTBP functional relevance/evidence category, HSMD actionability tier, clinical review status, population frequency, references, and notes.
- Evidence must be presented as support for human interpretation, not as automatic final classification.

Parallel search:

- Database searches run in a background GUI thread.
- Within that thread, variants are searched concurrently with a bounded worker pool.
- The default is 3 workers and the UI allows 1-8 workers.
- Use lower worker counts for public APIs without tokens; use higher counts only when local/network policy and API credentials allow it.
