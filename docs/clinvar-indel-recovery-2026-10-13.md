# ClinVar indel and result-rendering recovery

The supplied work-PC logs contained 114 ClinVar results with a `result_rendering`
timeout. The main run also had 49 successful results, so browser startup alone
could not explain this pattern. No patient identifiers or logged variants are
included here or in the regression fixtures.

A synthetic reproduction showed that the readiness check accepted a complete
record with a single GRCh37 position but timed out on the otherwise identical
record with a coordinate range. Seventy-one of the main run's 90 failures had
deletion, duplication, insertion or delins descriptions. The logs did not record
the failed DOM, so this does not establish the cause of every failure.

## Changes

- Read assembly-labelled single positions and ranges, including thousands
  separators and dash variants. A complete GRCh38 record reaches the identity
  check instead of being mistaken for an unfinished page.
- Require an exact reference span or exact span after removing shared VCF
  anchors. Insertions may use their exact flanking positions. Mere coordinate
  overlap does not verify identity. Gene and cDNA/protein checks remain required.
- Search indels using chromosome, position, reference, alternate and explicit
  GRCh37 first, as supported by [NCBI's search documentation](https://www.ncbi.nlm.nih.gov/clinvar/docs/help/).
  Transcript and gene/cDNA queries remain fallbacks. Unknown and frameshift
  protein descriptions are not used as exact protein search keys.
- Recognize explicit “No results were found” messages as completed empty searches
  so the next search alternative can run.
- Preserve retryable errors for genuinely incomplete pages. Their audit result
  and activity log now include page type, number of attempts, result-link count
  and whether record metadata, an HGVS title and genomic coordinates were read.
  Diagnostics do not dump page text, cookies or query strings.

## Limits and validation

This does not perform reference-sequence-based left/right normalization of
repeat variants. A shifted representation without a verified matching span is
rejected, rather than accepted merely because it is nearby. The capture layout,
attachments and Edge profile configuration are unchanged.

Regression coverage includes deletion, insertion, duplication, delins, VCF
anchors, ranges, wrong assembly/chromosome/change, overlapping wrong spans,
empty results and unfinished records. A real Edge DOM smoke check used an
isolated temporary browser context and intercepted every request locally with
synthetic HTML. Live lookup on the external work PC remains to be confirmed.
