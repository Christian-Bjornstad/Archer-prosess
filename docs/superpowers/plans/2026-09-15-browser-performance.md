# Browser Performance Optimization Plan — 2026-09-15

## Goal

Reduce browser evidence runtime and repeated work without weakening variant
identity, COSMIC GRCh37 selection, Franklin screenshot stability, capture
validation, checkpointing, pause/stop behavior, or patient isolation.

## Measured baseline

The supplied four-patient log was measured from 09:00 onward:

- 303 log events and 25 workbook updates.
- 13 initial Franklin variants expanded to 36 Franklin variant passes.
- Transcript plus GRCh37 fallback produced 72 Franklin query submissions.
- Franklin safety buffers consumed 464.9 seconds; OncoKB 121.4 seconds, COSMIC
  47.3 seconds, and ClinVar 17.8 seconds.
- Other-database wall times by patient were 723.8, 362.2, 1936.2, and 1066.7
  seconds.
- Franklin queries repeatedly took about 47 seconds in the log, but the old log
  cannot distinguish navigation timeout, selector timeout, or page rendering.

## Implemented first slice

- Default Franklin processing performs one service pass. `not_found` and
  `identity_mismatch` are terminal after transcript and GRCh37 genomic queries;
  they are not repeated in a loop.
- The GUI performs at most one immediate retry, and only for transient statuses:
  `error`, `timeout`, `session_lost`, or `partial_capture`. The batch-end third
  retry was removed.
- Franklin records status and elapsed seconds for every transcript/genomic query
  in the evidence audit and progress log. Timeout/error evidence also records the
  exact last stage: page opening, search input, hg19/somatic selection, query
  submission, route resolution, or classification rendering. This makes the next
  live run capable of locating the recurring 47-second wait before any timeout is
  shortened.
- Default between-variant buffers are reduced from 10–20 to 3–8 seconds. Exact
  former default configurations migrate automatically; customized values remain
  unchanged. COSMIC keeps its explicit 3–8-second provider buffer.
- Each patient uses at most three provider lanes: Franklin, MTBP, and a serial
  COSMIC/OncoKB/ClinVar fast lane. Patients and variants within each provider
  remain serial, and each provider retains its own persistent Edge profile.

For the same input shape, Franklin terminal work is expected to fall from 36 to
13 variant passes and from 72 to at most 26 query submissions, about 64% less
duplicated work. Franklin safety-buffer time should fall from 464.9 seconds to
roughly 65–75 seconds for 13 serial variants at the new delay range. The
three lanes additionally overlap Franklin and MTBP with the faster providers;
live wall time must be measured rather than inferred from summed log durations.

## Safety boundaries

- Do not parallelize variants or open two concurrent sessions against the same
  website/account.
- Keep Franklin's fixed one-second classification-tab render buffer and screenshot
  validation. Do not gate valid captures on provider-layout width heuristics.
- Do not change COSMIC's explicit GRCh37 verification.
- Do not guess a new Franklin route from stale browser history.
- A provider failure must not discard evidence completed by another lane.
- All lanes must finish or checkpoint their state before the next patient begins.

## Next measured experiments

1. Run the same representative batch and compare total patient time, Franklin
   `query_timings`, retry counts, and provider failure rates with this baseline.
2. If Franklin still clusters near 45–47 seconds, identify the exact timed stage.
   Only then replace that wait with a more specific DOM-ready or route-ready
   condition, or lower the narrow timeout involved.
3. Measure the 25 workbook checkpoint writes. Coalesce writes only if they are a
   material wall-time cost; never sacrifice recovery after a completed provider.
4. Consider splitting COSMIC, OncoKB, and ClinVar into separate lanes only if a
   live run shows meaningful remaining fast-lane time and Citrix remains stable
   with three concurrent Edge processes.
5. Revisit MTBP poll frequency only if timing proves polling itself is material;
   report readiness and timeout behavior remain correctness constraints.

## Verification gates

- Deterministic tests prove three-lane overlap, one-lane fallback, evidence merge,
  patient ordering, terminal-status handling, and the transient retry cap.
- Settings tests prove migration and preservation of customized delays.
- Franklin tests prove bounded passes, transcript/GRCh37 ordering, and query timing
  audit data.
- Run the full test suite and `git diff --check` before commit.
