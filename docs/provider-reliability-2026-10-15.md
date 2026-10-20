# Provider matching and recovery follow-up

The follow-up targets missed exact matches, unnecessary retries and incorrect
identity acceptance. Original TSV/evidence archives remain unchanged; actual
variants, samples, report identifiers and local replay data are not committed.
Attachment layout is unchanged.

## Matching corrections

- MTBP evidence parsing and screenshot row selection now share one matching
  policy and combine both alteration and linked identity text. When a report
  omits protein, an exact qualified transcript/cDNA or explicit exact GRCh37
  identity can recover a match. A conflicting reported protein or genomic
  identity still blocks it.
- Genomic-only inputs require matching genomic evidence, and inputs without any
  usable variant identity cannot import an arbitrary gene row.
- One unidentified gene row cannot verify multiple distinct submitted variants
  in the same gene. The existing gene-context fallback requires the complete
  submission and a single distinct input for that gene. Retained-report recovery
  lacks that complete context and cannot use this fallback.
- Missing row identity and multiple possible matches are manual review outcomes.
  Explicit different identities remain no-match outcomes.
- COSMIC requires complete identifier and gene tokens and bounded HGVS changes;
  an ID prefix, longer gene name, frameshift extension or unknown protein
  placeholder cannot prove identity. Exact gene/change matches still support
  canonical identifier aliases.

## Retry and restoration corrections

- A transient failure retries only the affected variant/source pairs, once.
  Another source's login, identity, layout or ambiguity outcome is preserved.
  Nontransient failure kinds do not trigger that automatic pass, and a stop
  request prevents the retry from starting.
- The recent-analysis inspector accepts the current audit schema as well as
  version 2, so current failed audits can advertise local recovery correctly.
- MTBP records carry matching revision 2. Older no-match results that contained
  gene rows are reopened for verification, while current checked no-matches
  remain completed. Legacy gene-only successes also require a new check unless
  they prove the current single-input matching rule. This preserves their local
  evidence while preventing a previous loose match from bypassing the new guard.
- Legacy ambiguous statuses can be retried deliberately through the retry
  action; they are not automatically repeated.
- MTBP finalization and cleanup recovery now update the identity-qualified
  canonical audit before remote deletion and refresh it with the cleanup
  outcome afterward. An interrupted recovery can therefore restore the newly
  captured result instead of an older canonical negative. Legacy capture files
  are retained; audit selection still requires the existing identity checks.

## Verification evidence

The three actual ClinVar cases previously marked manual review were replayed
against the public website in a separate Edge profile using the production
lookup and capture code. **All three were found and passed identity verification**
after two query attempts each. All three screenshots passed capture validation.
No sample identifiers were sent to ClinVar. These results add to the previous
34-case failed-render replay; they do not imply universal ClinVar coverage.

Offline MTBP replay supplied all variants from each of the 28 original analyses:
all **76 previous successes remain matched**, including the four gene-context
cases, each of which has one submitted variant in its gene. Two of the 66 old
failures are recovered; 64 remain unmatched in the archived visible identities.
The archive lacks the linked popup DOM needed to measure recovery of those 64
in the new extractor. A fresh authenticated run is still required for that.

The isolated Edge DOM smoke check now also covers a known input protein omitted
from the report, proving that exact hidden DNA selects the same row for parsing
and screenshot capture without opening the popup.

Final validation: **848 tests passed**, including 60 additional regression
cases; the two existing Pillow deprecation warnings remain. Compile and diff
checks passed. All four isolated Edge DOM checks passed. An independent review
and 181 relevant tests found no further actionable blocker in this change.
