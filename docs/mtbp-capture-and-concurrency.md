# MTBP variant images and Evidence layout — 2026-09-06

## Changed

- Preserve the full patient report in Vedlegg. Derive variant images locally from
  the same image and the DOM geometry captured with it; no per-variant portal requests.
- Include the MTBP accordion section title, column headings and complete evidence
  row (including functional relevance and evidence A/B/C).
- Prefer a unique gene + protein match in Alteration; cDNA is a fallback when
  protein identity is unavailable. A different transcript's cDNA must not reject
  an exact displayed protein match (regression: CBL p.Cys401Trp versus p.His398Tyr).
  If none or multiple match, include
  all rows for that gene, with a visible `genkontekst - variant ikke entydig` warning.
  Never include another gene. A missing gene still fails capture validation.
- Gene-context images do not change database match status or import an uncertain
  classification as exact evidence. They may accompany not-found/ambiguous results.
- Evidence has one scrolling area including search controls. The duplicate
  activity/log tabs, evidence matrix, fixed serial-worker field and privacy notice
  are removed. The command group is named Run queue. The duplicate current-activity
  panel and Rerun Failed Sources button are also removed; progress remains in the
  top progress area and messages still go to the Import log. Serial processing,
  queue resume and privacy safeguards are unchanged.
- COSMIC `not_applicable` displays as `Ikke funnet` in Oversikt (display text only; internal status unchanged).

## Bounded browser concurrency

The normal Evidence run and the separate Browser Sources run now use two lanes
per patient: MTBP runs alone while the remaining selected browser providers run
serially in their canonical order. Each lane owns a separate service instance,
and every provider still uses its own persistent Edge profile. There are never
more than two browser lanes, and neither patients nor variants within a provider
are parallelized.

Both lanes must finish before the next patient begins. Existing provider delays,
backoff, pause/stop checks and queued checkpoint/report writes are preserved.
Synthetic tests prove the two lanes overlap and that a single selected lane runs
directly. A live Citrix run is still needed to measure the real elapsed-time gain
and observe whether either portal changes its failure rate.

MTBP remains batched once per patient and its variant crops are derived locally,
so concurrency does not add per-variant MTBP requests.

## Verification and rollout

Unit tests exercise exact selection, ambiguous/missing identity, gene exclusion,
context headings and scrolling Evidence controls. `scripts/verify_capture_locally.py`
exercises a real Edge browser against local synthetic HTML, including nested
scroll containers, section headings, pixel scaling and Franklin captures.

The live Citrix report was visually inspected, but the changed code has not been
installed or run there. After updating the app, capture a new MTBP report and
generate the patient workbook using its report button. Old PNG geometry lacks
section headings and is not silently rewritten. Check a patient with two variants
in one gene, a putative/unknown section and the complete Vedlegg image.
