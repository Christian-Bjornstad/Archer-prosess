# Provider recovery from the v7 run and supplied evidence

Investigated the new run-log ZIP, both v6/v7 TSV files and the v7 browser-evidence
ZIP. Archives were read without executing their contents or modifying originals.
Clinical identifiers, actual variant values and patient/report paths are omitted
from this document and the regression fixtures.

## Input and recorded outcomes

Both TSV files passed the existing importer, key guard and filter pipeline:

| Input | Rows | Samples | Included | Excluded |
|---|---:|---:|---:|---:|
| v7 | 412 | 28 | 142 | 270 |
| v6 | 325 | 28 | 91 | 234 |

All parsed rows produced queries for ClinVar, Franklin, OncoKB and MTBP. This is
offline query-generation validation, not proof of database coverage.

The new run's result events show:

| Source | Found | Other recorded results |
|---|---:|---|
| ClinVar | 102 | 36 rendering errors; 3 manual reviews |
| MTBP | 76 | 66 missing row matches |
| OncoKB | 140 | 2 rendering errors |
| Franklin | 139 | 3 partial captures |
| COSMIC | 29 | 105 without an ID; 3 missing IDs; 1 manual review; 3 canonical-link timeouts |

The evidence ZIP contains duplicate audit files for some outcomes; log-event
counts above avoid counting those duplicates as additional failed lookups.

## Confirmed findings and changes

### ClinVar

Every remaining rendering error was on the search page, with zero recognized
result links. A real public Edge lookup using a synthetic nonexistent term
showed the new wording **Search results: 0 variants**. The old check did not
recognize this as a completed empty result and stopped instead of using its
fallbacks. The new check recognizes this explicit count. A bare empty/loading
table still does not prove absence.

More precise fallbacks now run before multiple protein-search candidates are
sent to manual review. Unresolved ambiguity remains manual; no candidate is
accepted without the existing gene/change/GRCh37 verification. Variants without
a usable protein query also get a gene/cDNA search alternative.

### MTBP

Of the 66 row-matching failures, 63 correspond to inputs without a protein
description. The saved report previews contain corresponding gene rows in many
cases, but noncoding rows only display descriptions such as splice region or
5 prime UTR and a **more** control. The input cDNA is absent from the short
visible text. Multiple rows for the same gene prevent safe gene-only matching.

MTBP's [public SmallPopup client code](https://mtbp.org/Templates/MTBP2025/js/smallPopup.js)
binds each `data-small-popup-id` trigger to `#small-popup-id-<id>`. The extractor
now reads the exact linked box's textContent alongside the alteration cell.
It does not open the popup, select any clinical context, or change the capture
layout. Matching and frozen screenshot geometry share the same extraction code.
Direct screenshot-row selection also uses those same identity details.

Additional hardening:

- Find columns by normalized header names instead of fixed indexes, including
  Functional relevance evidence. Propagate shared rowspan cells.
- Normalize equivalent one-/three-letter amino-acid notation for frameshifts,
  duplications and synonymous changes, preserving positions and stop distance.
- When hidden cDNA is transcript-qualified, require the requested accession
  family (NM/NR/XM/XR/ENST), including separate input transcript fields.
  Explicit genomic contradictions override cDNA/protein
  matches. Parsing and screenshot selection use the same checks.
- Shared rowspan gene cells retain their visible gene context in frozen crops;
  these captures are marked as gene context rather than an isolated variant row.
- Accept an exact normalized genomic change only with an adjacent GRCh37 label
  when recovering an input without protein identity. Missing/wrong assembly,
  shifted position and different change remain rejected.
- Report row/gene/match counts and matching basis for unsuccessful results.
  A declared nonempty report with no extractable rows is an extraction error,
  not confirmed database absence.

The archived audits do not contain the hidden popup DOM, so the number of the
66 failures that this resolves must be measured in a new work-PC run. Synthetic
Edge DOM checks prove that linked hidden identities can recover the exact row
without mixing two variants of the same gene or making the popup visible.

### OncoKB

The two timeout audits actually contain the explicit message **We do not have
any information for this variant**. The wait/parser now recognize it as a
completed absence result. Previously only the similar gene message was handled.
An explicit sign-in requirement on a variant route is handled separately from
a rendering timeout; alternative queries do not bypass it.

### Franklin

On new profiles, optional material/tissue onboarding is skipped only within a
visible contextual dialog with an unambiguous Skip/Skip for now button. The
handler runs before input and while resolving/waiting for the result, so a
late dialog is handled too. It does not choose blood, tumor or other material,
and unrelated dialogs are left alone.

The three partial-capture audits report assessment elements not reaching the
visible state. One bounded retry reloads only the resolved assessment view
after a transient failure. Ambiguous panels are not retried automatically.
Persistent partial capture remains visible, with its capture failure stage.

### COSMIC

The archived audits still do not show the page behind the three canonical-link
timeouts. Final transient failures now save a local diagnostic screenshot and
text preview, outside successful evidence records. This permits diagnosis from
the next evidence export without turning the failed page into an attachment.
Explicit missing IDs and the existing unresolved identity conflict remain
separate from transient failures.

## Verification

Regression tests reproduce the old failures before the fixes and cover negative
identity cases, empty/loading pages, bounded retry, unrelated dialogs and row
extraction failures. `scripts/verify_provider_recovery_dom.py` uses the app's
pure-Python CDP adapter and a separate temporary Edge profile with synthetic
data URLs. It verifies onboarding Skip, moved columns/shared cells, hidden
details and exact screenshot-row selection without contacting providers.

A separate public ClinVar check recognized the current empty-result page in
0.12 seconds after navigation, using a synthetic term. No supplied clinical
variant or sample was used for that request. Neither real evidence ZIP nor TSV
files are copied into the repository.

Live managed-PC validation remains necessary, especially for MTBP hidden detail
contents, unfamiliar Franklin dialog wording and the unresolved COSMIC page.
Database absence and genuine ambiguity are retained; the system does not claim
that every variant must exist in every database.

An independent code review found the transcript/context and shared-cell capture
edge cases above; both were reproduced before correction and covered by negative
identity tests and the offline CDP check.

Final validation: **788 tests passed** (two pre-existing Pillow deprecation
warnings), including 34 new provider recovery cases. Independent review found
no remaining actionable issues after the corrections. Compile and diff checks
passed, and the offline pure-Python Edge DOM checks passed.
