# Evidence Search Reliability and Recovery Design

**Status:** Approved design

**Date:** 2026-08-11
**Scope:** Browser evidence collection, processed-workbook recovery, patient report generation, and priority highlighting

## Context

A production-sized run containing 28 patients and 123 selected variants took roughly five hours. Most evidence was retained, but several reliability problems appeared:

- Franklin sometimes returned no accepted transcript result even though the same variant was available under a different transcript.
- Franklin classification screenshots could be blank, incomplete, or duplicated because element presence was treated as readiness.
- Franklin classification parsing could raise `list index out of range` when a category element existed before its text loaded.
- ClinVar sometimes accepted the first broad search result even when it represented another variant or gene, and the accepted assembly was not proven to be GRCh37.
- Repeated Edge launches eventually produced local CDP connection and port errors.
- Opening a large processed workbook repeatedly scanned the entire evidence directory from the GUI thread, making the application appear to hang or crash.
- Failed searches were not always represented by complete audit records, so recovery could not reliably distinguish a verified result from incomplete work.
- Per-patient reports were not guaranteed to be generated during a long unattended run.

The evidence folder `D:\CFB_app_browser_evidence2` demonstrated these failure modes. The design must recover useful existing work and rerun only evidence that is incomplete, mismatched, or unverifiable.

## Goals

1. Accept database evidence only when it can be tied to the requested variant and required reference assembly.
2. Capture complete Franklin evidence without globally slowing every successful request.
3. Reuse Edge sessions so long runs do not exhaust local browser or CDP resources.
4. Make large processed workbooks load without blocking the application window.
5. Preserve every search outcome in a form that supports safe resume.
6. Generate each patient workbook as soon as that patient's database work is complete, then reconcile reports at the end of the run.
7. Apply the agreed clinical-priority colors consistently in the GUI and Excel output.
8. Keep failures visible and recoverable without terminating the entire run.

## Non-goals

- The application will not make a clinical classification or interpretation automatically.
- The design does not replace provider websites with unsupported private APIs.
- It does not alter the user's selected variants, artifact catalog, or database order.
- It does not increase parallel browser activity. Provider searches remain deliberately conservative.
- It does not attempt to bypass provider authentication, licensing, quotas, or access controls.

## Decision

Adopt a reliability-first browser workflow with four explicit guarantees:

1. **Identity before import:** a provider result is imported only after variant identity is verified.
2. **Content before capture:** a screenshot is accepted only after the expected evidence content is ready.
3. **Persistence before progress:** every completed attempt is written to a canonical audit record before the run advances.
4. **Resume instead of restart:** transient, partial, legacy-unverified, and identity-related failures remain eligible for targeted retry.

The existing services remain the integration boundary. Focused helpers will be added for provider session lifetime, variant identity, capture readiness, audit indexing, and patient completion. This avoids a broad application rewrite.

## Architecture

### Evidence run session

One run-scoped session coordinates browser providers. It lazily opens one Edge/CDP context per provider and reuses it across variants and patients.

Responsibilities:

- Start a provider browser only when that provider is first needed.
- Reuse the provider page or context for sequential searches.
- Preserve the existing signed-in browser profile where required.
- Run the popup guard after navigation and immediately before capture.
- Detect a lost CDP connection and restart that provider session once.
- Close all owned sessions when the run finishes or is stopped.

Provider isolation is retained: a failed Franklin session must not invalidate MTBP, ClinVar, COSMIC, or OncoKB. MTBP continues to submit exactly one variant per report, but multiple reports reuse the same authenticated browser session.

### Identity verification

A normalized requested identity is constructed from available input fields:

- gene
- transcript-qualified HGVSc
- HGVSp
- GRCh37 chromosome and position
- reference and alternate alleles

Provider adapters return a candidate identity plus the evidence payload. A candidate is accepted only through a provider-specific verifier. Verification metadata is written to the audit record so the application never has to infer later whether an old result was trustworthy.

### Capture readiness

Capture is split into three stages:

1. Find the target section or evidence card.
2. Wait for provider-specific semantic content and a stable layout.
3. Capture, then validate the resulting image and extracted text.

An existing DOM element is not sufficient evidence of readiness. Empty text, a loading indicator, missing expected headings, implausibly small content, or an empty image marks the capture as incomplete. Only that incident receives an additional wait and retry; successful pages do not receive a blanket delay.

For new captures, validation uses the extracted DOM text together with image dimensions and a low-content image check. For legacy captures that lack stored DOM validation, the loader can prove only file presence and basic image content; any critical legacy image that is missing, unreadable, or effectively blank becomes `partial_capture`.

### Canonical evidence record

Each patient/source/variant combination has one canonical audit JSON record. It includes:

- schema version
- patient and normalized variant key
- provider
- status and whether the status is retryable
- all query attempts in order
- requested and returned identities
- identity-verification result and reason
- required assembly and verified assembly when applicable
- evidence summary and source URL where appropriate
- screenshot paths and capture-validation results
- provider/database version when available
- timestamps, duration, retry count, and error details

Franklin will no longer create both base and `-computed` audit records for the same variant. Screenshots may retain descriptive suffixes, but the audit record is singular.

## Provider behavior

### Franklin

Franklin uses an ordered query strategy:

1. Search by `gene:HGVSc` when transcript HGVSc is available.
2. If no acceptable candidate is found, search by GRCh37 genomic identity using:

   ```text
   chr<chromosome>-<position> <reference>><alternate>
   ```

   Example:

   ```text
   chr7-139097298 T>TC
   ```

The genomic query is attempted when the first query is unresolved, ambiguous, timed out, or produces only identity-mismatched candidates. It is not run after a verified first-query result.

A Franklin candidate is accepted when either:

- its transcript HGVSc is an exact normalized match, or
- its GRCh37 chromosome, position, reference allele, and alternate allele match the requested genomic identity.

The second rule intentionally permits a different displayed transcript for the same genomic variant, such as the observed LUC7L2 case. A same-gene but different genomic variant is rejected.

For computed classification, the capture workflow waits for meaningful content in both ACMG and oncology classifications. Each visible evidence box is captured once. Overview captures must end before the first evidence box so sections such as Case Control Studies are not duplicated. Functional Data and oncology evidence boxes use content-aware height and text stabilization so expandable content is included.

Prediction and population captures wait for their section-specific values or explicit no-data states. When required content remains absent after the normal wait, Franklin receives one incident-only extended wait and recapture. A still-empty section is saved as `partial_capture`, not `found`.

The parser must treat an empty category element as a loading state. It must never index the first text line until a non-empty line exists.

### ClinVar

ClinVar resolution is GRCh37-specific and fails closed.

The resolver performs exact transcript-HGVS and GRCh37 coordinate searches, requests multiple candidates, and fetches candidate records. A candidate is accepted only when its ClinVar sequence-location data explicitly contains:

- `Assembly="GRCh37"`
- matching chromosome
- matching VCF position
- matching VCF reference allele
- matching VCF alternate allele

Transcript HGVS may help rank candidates but cannot override a conflicting genomic identity. Broad gene/protein searches must never cause the first result to be accepted automatically. If the necessary genomic fields are unavailable or no candidate matches, the result is `not_found` or `identity_mismatch` with the attempted queries recorded.

The browser is opened only for the already-verified ClinVar variation record. Its screenshot remains limited to the requested summary area.

Legacy ClinVar `found` audits that do not explicitly record GRCh37 identity verification are migrated in memory to `verification_required`. Resume rechecks those records before treating them as complete.

### MTBP

MTBP retains one-variant-per-analysis behavior so each screenshot and report belongs to exactly one variant. The provider session is reused for successive one-variant submissions.

After submitting a variant, the workflow waits for a terminal report state. A ready report is verified against the submitted normalized identity before evidence is imported. A missing screenshot target is handled as a readiness problem: the workflow locates the report content again, waits for it to become visible, and makes one incident-only capture retry. It must not fail solely because a previously found element became detached or hidden.

MTBP report links remain excluded from Excel because they may point to a private authenticated report.

### Popup guard

A shared popup guard runs on all browser providers after navigation and before screenshots. It searches only within recognized dialog, consent, banner, overlay, and modal containers.

Supported actions include provider-specific and common labels such as:

- Reject all
- Accept essential or necessary only
- Only necessary
- No thanks
- Maybe later
- Got it
- a close control belonging to the recognized overlay

The guard must not click a generic `Close` or similar control elsewhere in the page. Unknown overlays are logged and may cause `partial_capture` if they obscure the target.

## Status and resume model

Terminal verified statuses:

- `found`
- `not_found`, when the provider completed an identity-aware search and no matching record exists
- `invalid_query`, when required input is structurally unavailable

Retryable statuses:

- `error`
- `timeout`
- `identity_mismatch`
- `partial_capture`
- `verification_required`
- `quota_exhausted`
- `session_lost`

Authentication failure remains retryable after the user restores the session. A provider-level CDP failure triggers one automatic provider-session restart. If the restarted session also fails, the current and remaining affected requests are persisted as retryable outcomes; the application continues with other providers and patients.

Pause waits at a safe checkpoint without discarding the current completed attempt. Stop closes owned browser sessions after persisting the latest outcome. Restart loads the audit index and schedules only missing or retryable evidence.

## Processed-workbook loading and existing-run recovery

### Audit index

When a processed workbook is opened, the application builds one evidence index keyed by canonical audit filename and normalized variant key. Recursive directory traversal happens once per evidence root, not once per variant/source probe.

Screenshot paths stored under an older drive or root are rebased when the same relative path exists under the selected workbook's evidence directory. Missing images are recorded, not treated as a fatal workbook error.

When legacy Franklin output contains both a base audit and a `-computed` audit for one variant, loading is deterministic: prefer a valid canonical audit; otherwise prefer the record with verified identity and the most complete valid screenshot set, using the newest timestamp only as a final tie-breaker. Loading does not delete either legacy file. The next successful write produces the single canonical record.

### Background loading

Workbook parsing, evidence indexing, legacy-status migration, and screenshot discovery run in a worker thread. The GUI remains responsive and reports loading progress. The completed model is applied to widgets on the GUI thread.

Any malformed individual audit is isolated and shown as a warning. It does not prevent other patients from loading.

### Recovery of `D:\CFB_app_browser_evidence2`

Opening the existing processed workbook must retain verified evidence while marking the following for targeted resume:

- Franklin errors, identity mismatches, and blank or incomplete critical captures
- ClinVar results without explicit GRCh37 identity proof
- browser/CDP failures
- missing or malformed audit records
- any provider result whose required screenshot is missing

Clean, verified provider results must not be searched again.

## Patient report generation

The search coordinator emits a patient-completed event only after all selected providers for all selected variants in that patient have reached either a terminal verified status or a persisted retryable status.

That event queues a background write of:

```text
<DIT>_VPM_Tolkning.xlsx
```

The report is written beside the main processed workbook. A locked destination produces a user-visible warning and a pending-report entry; it does not stop the search. At the end of the run, final reconciliation creates or updates every missing or stale patient report.

The final application state distinguishes:

- evidence search complete and all patient reports written
- evidence search complete with retryable evidence
- evidence search complete with pending locked reports
- stopped by user with resumable progress saved

## Priority highlighting

Highlight decisions are applied in this precedence order:

1. **Known artifact:** orange
2. **Tier priority:** strong green when Tier I + Tier II count is greater than 5
3. **Germline priority:** strong green when Germ count is greater than 10 and AF is at least 35%
4. **Possible germline priority:** weak green when Germ count is greater than 10 and AF is below 35%
5. No priority fill

Boundary behavior is explicit:

- Tier total 5 is not highlighted; 6 is strong green.
- Germ count 10 is not highlighted; 11 is eligible.
- AF 34.99% is weak green; AF 35.00% is strong green.
- Artifact orange wins even when a tier or germline condition also matches.
- A missing or invalid AF cannot receive a germline-priority color and is surfaced as a data warning.

The same categories and colors are used in the GUI tables, processed workbook, and patient workbooks.

## Error handling and user feedback

- No provider exception may terminate the GUI process.
- Every exception crossing a provider boundary is converted into a persisted result with patient, source, variant, timestamp, duration, and a concise reason.
- Logs keep timestamps and show query fallback, identity verification, incident-only retries, provider restarts, report writes, and final outcome counts.
- Expected user-action errors, such as a locked Excel file or expired login, are shown as clear warnings without a traceback-only failure.
- Unexpected programming errors retain diagnostic details in logs while the UI presents a concise message.
- A completed run shows an unmistakable `Search complete` state and summary, including counts requiring retry and reports still pending.

## Privacy and safety

- Browser queries continue to use pseudonymous identifiers and variant data only.
- Credentials are not added to workbooks, logs, audits, screenshots, configuration files, or this repository.
- MTBP authenticated report URLs are not exported.
- Provider evidence remains decision support and must be human-verified before clinical use.
- Identity ambiguity always fails closed; convenience must not override variant correctness.

## Test strategy

### Unit tests

Franklin:

- produces `chr7-139097298 T>TC` from the observed input fields
- attempts transcript query before genomic fallback
- skips fallback after a verified first result
- accepts a different transcript only when genomic identity matches
- rejects a same-gene, different-position candidate
- treats empty category text as not-ready without raising an index error
- identifies blank or implausibly incomplete captures as `partial_capture`
- captures each evidence box once and excludes it from the overview capture

ClinVar:

- accepts only a candidate with matching GRCh37 VCF fields
- rejects an otherwise similar GRCh38 location
- rejects the wrong gene or allele returned by a broad search
- marks legacy unverified `found` evidence as `verification_required`

Highlighting:

- verifies Tier totals 5 and 6
- verifies Germ counts 10 and 11
- verifies AF values immediately below and exactly at 35%
- verifies artifact precedence

Audit and resume:

- writes one canonical audit per patient/source/variant
- retries every documented retryable status
- preserves verified terminal results
- rebases moved screenshot paths
- isolates malformed audits

### Integration tests

- Multiple variants for the same provider launch one provider session.
- A simulated lost CDP connection restarts the provider once and preserves progress.
- MTBP performs separate one-variant reports through one session.
- Popup dismissal acts only inside recognized overlays.
- Franklin incident-only retry waits after incomplete content but not after a successful first capture.
- Loading hundreds of evidence records uses one directory index and does not call recursive search per cell.
- Patient completion queues its workbook without waiting for the entire cohort.
- A locked patient workbook is reported and reconciled later without stopping evidence collection.
- Stop and restart search only missing or retryable work.

### Regression fixture

A privacy-safe synthetic fixture modeled on the failure shapes from `D:\CFB_app_browser_evidence2` will cover:

- a same-genomic-variant/different-transcript Franklin result
- an empty Franklin category followed by loaded content
- a false broad ClinVar hit
- a GRCh37/GRCh38 coordinate distinction
- a missing screenshot under a moved evidence root
- a provider session loss after several successful variants

Real patient identifiers and credentials must not be copied into the test suite.

## Acceptance criteria

The change is complete when all of the following are true:

1. Franklin's genomic fallback uses the agreed `chr-position REF>ALT` form and resolves the same-genomic-variant transcript case.
2. Empty Franklin categories no longer cause `list index out of range`.
3. Blank or incomplete required screenshots are not recorded as successful evidence.
4. ClinVar evidence is accepted only after explicit GRCh37 coordinate and allele verification.
5. A multi-variant run reuses one browser session per provider and survives a recoverable provider restart.
6. Loading the 311-variant processed workbook performs one evidence-directory indexing pass and keeps the GUI responsive.
7. Resuming the existing run schedules only incomplete, retryable, or legacy-unverified evidence.
8. Patient workbooks are generated incrementally and reconciled at run completion.
9. Locked workbooks and individual provider failures warn the user without shutting down the application.
10. Priority highlighting matches all specified threshold boundaries and is consistent across GUI and Excel outputs.
11. Automated tests cover the identity, capture, recovery, session, report, and highlighting behavior described above.

## Alternatives considered

### Add longer fixed waits everywhere

This is simple but would make a five-hour run substantially longer while still failing when content loads beyond the chosen delay. It also cannot prevent wrong ClinVar matches or browser process exhaustion.

### Rerun every patient after any failure

This is operationally straightforward but wastes provider queries, increases runtime and quota pressure, and risks replacing good evidence. It does not solve the inability to distinguish verified from incomplete evidence.

### Reliability-first targeted recovery — selected

Identity verification, semantic readiness, canonical audits, persistent provider sessions, and indexed background recovery directly address the observed root causes. The implementation is broader than adding sleeps, but it remains bounded to the existing evidence and report services and supports safe incremental rollout.

## Implementation boundaries

The implementation plan should organize work into independently testable slices:

1. priority rules and boundary tests
2. canonical statuses, audit schema, and indexed recovery
3. Franklin query identity and content-aware capture
4. ClinVar GRCh37 candidate verification
5. provider-session reuse and popup guard
6. MTBP one-variant session reuse and capture recovery
7. background processed-workbook loading
8. incremental patient reports and final reconciliation
9. end-to-end resume and GUI completion verification

Each slice must preserve existing verified evidence and land with focused tests before the next slice begins.
