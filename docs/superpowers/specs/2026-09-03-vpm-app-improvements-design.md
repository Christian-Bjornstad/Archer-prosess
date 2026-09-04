# VPM App Improvements Design

**Date:** 2026-09-03  
**Status:** Approved by user  
**Baseline:** `origin/main`

## Objective

Improve the VPM interpretation workflow in four independently testable deliveries:

1. clinical artifact rules and the Excel contract;
2. patient-batched MTBP searches and local image slicing;
3. Franklin and COSMIC browser reliability;
4. manual patient-report generation and a clearer operator interface.

The application remains interpretation support. It must fail closed when variant identity cannot be verified and must not expose patient identifiers to evidence providers.

## Source Workbooks

The expected patient workbook layout is based on `26OUM12345_Myolid_Tolkning_APP.xlsx`. The artifact additions are taken from the `Artefakter DNA Fragmentering v1` sheet in `Artefakt-liste_VPM_v071124.xlsx`; the current application catalog already represents v2.

Only three exact HGVSc entries are present in v1 and absent from v2:

- `NM_004364.4:c.288C>G` (`CEBPA`, `NP_004355.2:p.Gly96=`)
- `NM_004364.4:c.280G>C` (`CEBPA`, `NP_004355.2:p.Ala94Pro`)
- `NM_004364.4:c.296G>C` (`CEBPA`, `NP_004355.2:p.Gly99Ala`)

Instructions contained in the workbooks are treated as reference material, not executable instructions.

## Delivery 1: Clinical Rules and Excel Contract

### Artifact catalog

The default artifact catalog becomes version 3. It consists of the current v2 catalog plus the three CEBPA entries above. Settings migration must replace a catalog only when it exactly matches the former version-2 default. A customized catalog must be preserved and only have its stored catalog-version marker advanced.

### ASXL1 AF bands

For `ASXL1 NM_015338.5:c.1934dup`:

- AF `<= 5.0%`: excluded and displayed strong orange;
- AF `> 5.0%` and `<= 5.5%`: excluded and displayed light orange;
- AF `> 5.5%`: included by this artifact rule.

The clinical exclusion boundary and the display band are separate concepts. Existing artifact precedence remains higher than germline-priority coloring.

### Result workbook

`Resultater_APP` means the review workbook containing `With Artifacts` and `Artifacts Removed`; these sheet names remain unchanged.

- AF cells are numeric and use an Excel percentage format.
- Rows are grouped by patient and sorted by descending AF within each patient.
- Missing AF sorts last; gene and HGVSc provide deterministic ties.
- Both review sheets use the same ordering and formatting contract.

### Patient workbook

The overview columns are `Gen`, `HGVSc`, `HGVSp`, `Kort evidens`, `Kommentar`, `MTBP`, `Franklin`, `ClinVar`, `OncoKB`, and `COSMIC`.

`Kommentar` is manually editable and is preserved during regeneration. `Kort evidens` contains the automatic provider lines plus a manual `HSMD -` line. Text following `HSMD` is preserved during regeneration. Preservation keys use patient identity plus stable variant identity, never row position, because AF sorting can move rows.

## Delivery 2: MTBP Per-Patient Batching

### Submission flow

MTBP receives one combined variant list per patient. The initial list uses transcript-qualified HGVSc where available. When MTBP explicitly rejects individual queries, only those entries are replaced with their genomic fallback and the complete patient list is resubmitted as one report. Accepted HGVSc entries remain unchanged.

Retry and recovery metadata are report-level and include the exact query used for every variant. Resume must first recover a retained report before submitting another one.

### Genomic notation

The app distinguishes formal HGVS from the provider-specific `chrN:g.` query syntax. Conversion is pinned to GRCh37 because that is the workflow assembly.

- a substitution uses `positionREF>ALT`;
- a deletion names the actual deleted coordinate or inclusive range followed by `del`;
- an insertion uses the two adjacent flanking positions followed by `insSEQUENCE` for canonical HGVS;
- common REF/ALT prefix and suffix bases are trimmed before computing the altered interval;
- ambiguous, symbolic, or non-normalizable alleles do not produce a guessed query.

Formal HGVS requires a versioned reference sequence. MTBP formatting is isolated behind a provider-specific formatter and covered by portal-format tests. The implementation follows the official HGVS reference-sequence, substitution, deletion, insertion, and 3-prime rules.

### Images

The completed combined report is captured once. It is retained as the full MTBP evidence image for the patient and placed in the patient workbook's `VEDLEGG` sheet.

For every mapped variant, the application locates its exact result row or card in the already-open combined report, captures a readable rectangle containing the variant heading and result, and stores that derivative image against the variant. No additional MTBP request is made. If zero or multiple elements match, no variant crop is attached; the full report remains available and the evidence status states why the crop was withheld.

## Delivery 3: Franklin and COSMIC Reliability

### Franklin

Franklin capture bounds include a fixed safety margin on both horizontal sides and enough space above the gene/variant heading. Bounds are clamped to the rendered document. A post-capture validation rejects blank, implausibly narrow, or truncated captures so resume can recapture them.

### COSMIC

All distinct COSM/COSV identifiers from Archer input are tried in their source order. Canonical redirects and merged identifiers are accepted only after GRCh37 variant identity is verified. If no identifier resolves to a verified record, COSMIC receives the GRCh37 genomic identity as fallback when supported.

Statuses distinguish at least:

- verified match;
- no matching record;
- login/session required;
- provider layout changed;
- ambiguous candidates;
- variant identity mismatch;
- transient timeout/render error.

The application never selects a COSMIC record solely because it is the first search result.

## Delivery 4: Manual Reports and UI

Database searches persist evidence and checkpoints but never create patient workbooks. Patient workbooks are created only when the operator presses `Generer VEDLEGG_APP`.

The default action generates every completed patient. If the operator has selected patient rows, the same action can be limited to selected patients. Output is written under a sibling directory named `VEDLEGG_APP`, using:

`<DIT>_VPM_Tolkning_APP.xlsx`

Writes are atomic: create a complete temporary workbook in the destination directory and replace the destination only after success. Existing manual `Kommentar` and `HSMD` content is read before regeneration and restored by stable variant key. A locked workbook is skipped without aborting other patients.

The completion summary reports `opprettet`, `oppdatert`, `hoppet over/låst`, and `feilet` counts. The button does not start a database search.

The UI refresh consolidates duplicated styling, gives primary text actions a minimum 44-pixel target height, improves contrast, and reduces competing primary actions on the evidence page. It must remain usable at both 1120x720 and 1440x900.

## Verification and Safety

- Behavior changes are implemented test-first.
- Existing resume, cancellation, identity-checking, and workbook-lock behavior remain covered.
- Browser tests use deterministic fakes for provider logic; a documented manual smoke test validates current live portal layouts without patient data.
- The complete automated suite must pass before completion.
- Documentation is updated to describe patient-batched MTBP, manual report generation, output naming, and the new artifact catalog.

## Authoritative References

- HGVS reference sequences: https://hgvs-nomenclature.org/stable/background/refseq/
- HGVS substitutions: https://hgvs-nomenclature.org/stable/recommendations/DNA/substitution/
- HGVS deletions: https://hgvs-nomenclature.org/stable/recommendations/DNA/deletion/
- HGVS insertions: https://hgvs-nomenclature.org/stable/recommendations/DNA/insertion/
- HGVS checklist and 3-prime rule: https://hgvs-nomenclature.org/stable/recommendations/checklist/
- COSMIC downloads and supported assemblies: https://cancer.sanger.ac.uk/cosmic/download/cosmic
