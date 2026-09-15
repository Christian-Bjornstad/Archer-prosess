# MTBP provider pause and recovery

## What the pause means

The application pauses only the MTBP lane when it cannot prove that a submitted
patient batch is either safely absent or fully captured. Other browser sources
continue. Later MTBP lookups are recorded as `deferred`, so they remain visible
and are included by **Resume Incomplete Search**.

The main safety rule is that an uncertain click is never repeated. Its
`ARCHER-...` analysis ID is stored before submission and used to reconcile the
queue and Reports List on resume.

## First checks

1. Open MTBP Reports List and search for the exact analysis ID shown in the log.
2. If it exists, leave it in place and use **Resume Incomplete Search**. The app
   will capture or finish that report before submitting anything new.
3. If the report list is at the five-report limit, close any open report page and
   resume. The app may delete only a completed app-generated `ARCHER-...` report;
   manual reports and unresolved IDs are protected.
4. If the ID is absent but the app still reports `submission_unknown`, retain the
   log and diagnostic artifacts for review. Do not create the same batch manually
   unless duplicate submission has been ruled out.

## Log fields

- `MTBP PREFLIGHT`: report count, protected report count, available slots and
  cleanup outcome before submission.
- `MTBP SUBMISSION`: analysis ID, query count, button state and current URL.
- `submission_unknown`: the click occurred, but acceptance could not be proven.
- `PROVIDER PAUSED`: no more patients will be submitted to that source this run.
- `deferred`: intentionally not attempted after a provider pause; safe to resume.

## Acceptance check on the job PC

Use a small synthetic batch with at least three patients. Confirm that a forced
MTBP acceptance failure results in one uncertain analysis ID, no submissions for
later patients, continued processing by other providers, and a resume that checks
the existing ID before any new submission. Also confirm that a completed mixed
report containing both found and legitimate not-found variants is deleted after
its local audit and screenshots have been written.
