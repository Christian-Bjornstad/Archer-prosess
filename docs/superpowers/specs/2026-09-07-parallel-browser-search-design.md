# Parallel Browser Search Design

## Goal

Reduce total database-search time by overlapping the slow MTBP patient report
with the remaining browser databases, while preserving safe website pacing,
resume checkpoints, and evidence correctness. COSMIC must search only by COSMIC
ID and must never fall back to genomic position.

## Architecture

For each patient, both `DatabaseWorker` (the normal Evidence run) and
`BrowserReviewWorker` (the separate Browser Sources run) split the selected
browser databases into two independent lanes:

1. An MTBP lane containing only MTBP.
2. An other-provider lane containing the remaining selected browser databases
   in their existing order.

When both lanes exist they run concurrently with at most two worker threads and
two independently constructed `BrowserReviewService` instances. Providers
inside a lane remain serial, so the application never runs concurrent requests
against the same provider. If only one lane exists, the current synchronous path
is retained. The next patient starts only after both lanes finish.

Each service continues to use its provider-specific Edge profile. This prevents
profile sharing between the concurrent Edge processes. Existing provider delays,
retry limits, MTBP patient batching, screenshots, and artifact paths remain
unchanged.

## Data Flow and State

Each lane returns its own evidence mapping. The owning worker merges both
mappings only after the futures complete, avoiding concurrent writes to shared
in-memory evidence. Existing checkpoint signals may still be emitted from either
lane; Qt queues these signals back to the GUI thread, which keeps workbook writes
serialized.

The combined patient result has exactly the same shape as before. A failure in
one lane is converted through the existing per-provider error handling and does
not cancel the other lane. Patient completion is emitted only after both lanes
have completed, so the visible patient progress and resume boundary remain
patient-based.

## Pause, Stop, and Retry

Both lanes share the existing `SearchPauseControl`. Cancellation checks capture
the owning Qt worker thread before child threads are started; child services must
not infer cancellation from their own executor thread. Requesting interruption
therefore stops both lanes cooperatively at their existing cancellation points.

Immediate retries remain scoped to the failed lookups in their original lane.
The existing final failed-source pass remains unchanged in behavior. Successful
lookups are not repeated.

## COSMIC Lookup Contract

COSMIC accepts only identifiers parsed from `VariantRecord.cosmic_id`.

- With no COSMIC ID, the result is not applicable and no browser search starts.
- With one or more IDs, each ID may be tried in the existing order.
- When all supplied IDs fail, the last ID result is returned as not found or
  identity mismatch.
- Genomic position, reference allele, and alternate allele are never submitted
  to COSMIC as a fallback.

Genomic fallback remains available to providers that explicitly support it; this
change is limited to COSMIC.

## Performance and Observability

The concurrency cap is fixed at two browser lanes. Status messages identify the
lane and elapsed time, allowing a real Citrix run to compare the old sum of MTBP
plus other-provider time with the new per-patient maximum. Tests use controlled
delays to prove overlap without depending on live external websites.

## Testing

Automated tests must prove:

- MTBP overlaps the other-provider lane.
- Providers within the other-provider lane remain serial.
- One selected lane uses the synchronous path.
- Evidence from both lanes is merged without loss.
- One lane failing does not suppress successful evidence from the other lane.
- Stop and pause callbacks are shared safely across both lanes.
- COSMIC never generates or opens a genomic fallback URL.
- Existing browser-review, GUI, checkpoint, and resume tests remain green.

## Scope

This change does not parallelize patients, variants within MTBP, or providers
within the other-provider lane. It does not change the report-generation button,
Excel layout, screenshot processing, login storage, or provider pacing.
