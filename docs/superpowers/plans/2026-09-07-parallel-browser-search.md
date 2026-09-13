# Parallel Browser Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run MTBP concurrently with the serial queue of other browser providers, and restrict COSMIC searches to COSMIC identifiers.

**Architecture:** `DatabaseWorker` and `BrowserReviewWorker` partition each patient's selected databases into a one-provider MTBP lane and one serial other-provider lane. Two independent `BrowserReviewService` instances run through a two-worker executor, return isolated evidence maps, and are merged after completion; a captured owner-thread cancellation callback keeps stop and pause correct from executor threads.

**Tech Stack:** Python 3.11+, PyQt6 `QObject`/`QThread`, `concurrent.futures.ThreadPoolExecutor`, pytest.

**Spec:** `docs/superpowers/specs/2026-09-07-parallel-browser-search-design.md`

## Global Constraints

- At most two browser lanes may execute concurrently.
- MTBP is the only provider in its lane; all other selected providers remain serial.
- Patients remain serial and complete only after both lanes finish.
- Provider-specific Edge profiles, pacing, retries, checkpoints, and artifact paths remain unchanged.
- COSMIC may submit only COSM/COSV identifiers, never genomic coordinates.
- The unrelated untracked `docs/edge-cdp-dynamic-port.md` must not be modified or committed.

---

### Task 1: Remove COSMIC genomic fallback

**Files:**
- Modify: `src/archer_processor/services/browser_review.py`
- Modify: `tests/test_browser_review.py`

**Interfaces:**
- Consumes: `BrowserReviewService._lookup_cosmic_with_retry(...) -> DatabaseEvidence`.
- Produces: `_lookup_cosmic_variant(...) -> DatabaseEvidence` whose `query_attempts` contains COSMIC IDs only.

- [ ] **Step 1: Replace the fallback expectation with a failing identifier-only test**

```python
def test_cosmic_stops_after_all_identifiers_miss(tmp_path, monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[3]
    variant.cosmic_id = "COSM111; COSV222"
    service = BrowserReviewService(profile_root=tmp_path)
    attempts = []

    def lookup(page, candidate, query_url, artifact_directory, *, progress):
        attempts.append(candidate.cosmic_id)
        return DatabaseEvidence("COSMIC", "not_found", "missing", accession=candidate.cosmic_id)

    monkeypatch.setattr(service, "_lookup_cosmic_with_retry", lookup)
    evidence = service._lookup_cosmic_variant(object(), variant, tmp_path, progress=None)

    assert attempts == ["COSM111", "COSV222"]
    assert evidence.status == "not_found"
    assert evidence.accession == "COSV222"
    assert evidence.raw["query_attempts"] == ["COSM111", "COSV222"]
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest tests/test_browser_review.py::test_cosmic_stops_after_all_identifiers_miss -q`

Expected: FAIL because the current implementation attempts `_cosmic_genomic_query` after both IDs miss.

- [ ] **Step 3: Implement the identifier-only result path**

Delete the genomic-fallback branch from `_lookup_cosmic_variant`. Return `last_result` after the identifier loop while preserving `query_attempts` and `query_attempt_results`. Remove `_lookup_cosmic_genomic_with_retry`, `_resolve_cosmic_genomic_page`, and `_cosmic_genomic_query` only after `rg` proves they have no non-test callers. Remove the obsolete fallback-only fixtures and tests.

- [ ] **Step 4: Run COSMIC tests and verify GREEN**

Run: `python -m pytest tests/test_browser_review.py -k cosmic -q`

Expected: all retained COSMIC tests pass and no test expects `lookup_mode == "genomic_fallback"`.

- [ ] **Step 5: Commit the COSMIC behavior change**

```bash
git add src/archer_processor/services/browser_review.py tests/test_browser_review.py
git commit -m "fix: restrict COSMIC searches to COSMIC identifiers"
```

### Task 2: Add bounded per-patient browser lanes to both run paths

**Files:**
- Modify: `src/archer_processor/gui/app.py`
- Modify: `tests/test_gui.py`

**Interfaces:**
- Consumes: `BrowserReviewService.search_variants(variants, databases, artifact_directory, ...)`.
- Produces: a shared database-lane partition plus patient searches in both worker classes that return the same `dict[str, list[DatabaseEvidence]]` shape as today.

- [ ] **Step 1: Write failing tests for lane partitioning and real overlap**

Create controlled fake services with `threading.Barrier`. Assert for both the normal Evidence worker and the separate Browser Sources worker that a patient selecting `COSMIC`, `Franklin`, and `MTBP` creates two lanes, that calls contain `["COSMIC", "Franklin"]` and `["MTBP"]`, and that both calls enter before either is released. Also assert evidence from both calls is present in the final mapping.

- [ ] **Step 2: Run the new worker tests and verify RED**

Run: `python -m pytest tests/test_gui.py -k "browser_worker and (parallel or lane)" -q`

Expected: FAIL because one service currently receives all databases serially.

- [ ] **Step 3: Implement the two-lane coordinator**

Import `ThreadPoolExecutor` and `as_completed`. Capture `owner_thread = QThread.currentThread()` at the beginning of `run` and pass `lambda: owner_thread.isInterruptionRequested()` into each independently built service. Partition selected databases as:

```python
other = [database for database in self.databases if database != "MTBP"]
lanes = []
if other:
    lanes.append(("other", other))
if "MTBP" in self.databases:
    lanes.append(("mtbp", ["MTBP"]))
```

For two lanes, submit one `_search_patient_with_retries` call per independently built service, using lane-local database lists and evidence state. Wait for both futures, merge their returned evidence on the owner worker, and emit patient completion only after both finish. For one lane, execute directly without an executor. Include elapsed-time status messages for each lane.

- [ ] **Step 4: Make retry state lane-local and cancellation owner-thread-safe**

Pass the lane's database list into `_search_patient_with_retries` and `_run_search_pass` instead of reading `self.databases`. Protect snapshots and merges of `_pass_evidence` with a lock, or keep lane accumulators local and merge them after completion. Change `_wait_if_paused` and `_check_cancelled` to use the captured owner-thread predicate so executor threads observe the GUI stop request.

- [ ] **Step 5: Test isolation, serial fallback, and failures**

Add tests proving a run with only MTBP constructs one service and uses no concurrent second lane; a failure result from one lane does not remove successful evidence from the other; and databases in the other lane are passed to one service in canonical serial order.

Run: `python -m pytest tests/test_gui.py -k "browser_worker" -q`

Expected: all browser worker tests pass.

- [ ] **Step 6: Commit the bounded concurrency change**

```bash
git add src/archer_processor/gui/app.py tests/test_gui.py
git commit -m "feat: overlap MTBP with other browser searches"
```

### Task 3: Regression and performance verification

**Files:**
- Modify if needed: `docs/mtbp-capture-and-concurrency.md`

**Interfaces:**
- Consumes: the complete project test suite and package build configuration.
- Produces: verified branch with documented concurrency behavior.

- [ ] **Step 1: Update the concurrency assessment**

Change the document from “not enabled” to the implemented two-lane model, including the fixed two-session cap and the fact that live Citrix speedup still requires observation.

- [ ] **Step 2: Run static and full regression checks**

Run the repository's configured formatter/linter/type checks discovered from `pyproject.toml`, then run `python -m pytest -q`.

Expected: commands exit 0 with no new warnings or failures.

- [ ] **Step 3: Verify branch hygiene**

Run: `git diff --check`, `git status --short`, and `git log --oneline -5`.

Expected: only the pre-existing untracked `docs/edge-cdp-dynamic-port.md` remains; implementation changes are committed atomically.

- [ ] **Step 4: Commit documentation if changed**

```bash
git add docs/mtbp-capture-and-concurrency.md
git commit -m "docs: record bounded MTBP concurrency"
```
