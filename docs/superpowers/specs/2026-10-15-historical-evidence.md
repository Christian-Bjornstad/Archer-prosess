# Historical evidence backup

Approved intent: show a previously retrieved result when a current lookup fails
or returns no match, only for the exact same variant. Label it **Tidligere
resultat** with its original date and time. Do not add a source field; the
existing database column supplies context. Continue the approved compact UI and
preserve attachment appearance.

## Behavior

- Always try the current provider first, including the existing bounded retry.
  Historical evidence is a fallback and never counts as a fresh verified result.
- Use a persistent local SQLite store under the application's configuration
  directory. Keep immutable copies of validated per-variant evidence captures.
- Match provider, gene, reference assembly, chromosome, position, REF and ALT
  exactly after conservative normalization. MTBP additionally requires the same
  cancer context. Never match only on gene or protein, and do not shift indels.
- Store only trusted successful lookups with complete variant identity and
  validated captures. Exclude identity-free MTBP gene context and all historical
  results. Remove patient/sample, file, credentials and remote analysis metadata
  from the shared historical snapshot.
- Use the original timezone-aware capture timestamp, normalized to UTC. Opening
  or saving a workbook, falling back, and publishing twice cannot refresh it.
- Preserve the current failed attempt in historical metadata. Automatic retry,
  provider circuit breakers and MTBP report protection continue to use current
  lookup outcomes. Historical results remain explicitly retryable.
- Display an amber historical label and visible local date/time in Evidence and
  exported result text. Historic classifications must not silently appear fresh.
  Existing capture layout is unchanged.
- Backfill from identity-qualified, validated successful audits when opening an
  existing review workbook in the background. Missing dates or identities must
  be skipped, not inferred from file modification time.
- An unavailable/corrupt history store or invalid capture must not interrupt the
  current lookup or workbook load. Report a nonfatal diagnostic.
- The first version is local to the current Windows user and computer. It does
  not synchronize between PCs. A fresh genuine capture can update the last
  observed time; reusing the same capture cannot.

## Verification

Use synthetic data only in committed tests. Cover exact-match exclusions,
provider/context separation, cross-sample privacy, immutable capture recovery,
timestamp stability, concurrent SQLite access, failure isolation, successful
live retry precedence, provider pause/MTBP resume preservation, workbook
roundtrip and visible historical labeling. Verify the native UI preview, focused
regressions, then the full suite. Push a separate feature branch and pull request.
