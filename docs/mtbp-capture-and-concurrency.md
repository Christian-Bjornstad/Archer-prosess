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

## Concurrency assessment (not enabled)

The browser workflow remains sequential. The generic API service has a worker-pool
method, but that is not a safe switch for browser-based searches: each provider
uses persistent browser/profile state, login, navigation and capture, and MTBP
also manages report capacity/cleanup. Sharing those mutable sessions between
workers risks mixed pages, profile locks and misassigned captures.

Potential next experiment: at most two *different* providers concurrently, each
with its own browser/session and one in-flight request, preserving provider delays,
backoff, pause/stop and serial checkpoint/report writes. Compare elapsed time and
error rate on a synthetic batch before enabling. Do not parallelize patients or
variants within MTBP, Franklin or COSMIC. Website permission/terms have not been
established by this code review; technical feasibility is not authorization.

Batch MTBP once per patient and crop locally first: this saves portal work without
increasing concurrency. No measured speedup is claimed for the proposed experiment.

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
