# Provider failures: work-PC log follow-up

The supplied logs contain the following non-ClinVar result failures:

| Provider | Recorded outcomes requiring investigation |
|---|---|
| OncoKB | Four result-rendering timeouts; 139 successes |
| COSMIC | Three canonical-link timeouts, one identity mismatch, three explicit missing IDs; 29 successes |
| MTBP | Four Edge startup errors in an earlier run, three missing report rows; 139 successes |
| Franklin | 142 successes, no recorded result failures |

Counts refer to result events, not unique variants. Clinical identifiers and
variant values from the logs are intentionally omitted. The Edge profile
startup issue was addressed separately and is reported resolved by the user.

## Reproduced code weaknesses and changes

- **OncoKB:** unknown protein descriptions (`p.?`, `p.(?)`, `p.=`, `-`) no
  longer generate unusable protein-route queries. When available, the explicit
  GRCh37 genomic route is used first for these inputs. The cDNA route and its
  identity check agree when no genomic query is available.
- A protein-route rendering timeout may try the existing distinct genomic
  fallback, with at most two URLs. This does not bypass login failures, or
  convert two incomplete pages into an absence result. Rendering diagnostics
  identify missing overview/effect fields; waits are interruptible.
- **MTBP:** unknown protein placeholders no longer prevent exact cDNA matching
  in the report or screenshot-row check. cDNA matching requires the whole
  change: `c.100del` does not match `c.100delinsA`. Known conflicting protein
  descriptions retain the existing strict rejection behavior.
- **COSMIC:** cache reuse requires the same identifier and variant identity,
  rather than just the identifier. The same variant can still reuse a capture
  across samples. Different variants sharing an ID require a fresh lookup.
- Once all supplied identifiers have been tried, a resolved identity conflict
  becomes `manual_review`, with the conflict and attempted IDs preserved. A
  missing final ID does not hide an earlier conflict. A later verified match
  still succeeds normally. This avoids repeatedly presenting an unchanged
  source-ID conflict as a temporary technical failure.
- COSMIC canonical/GRCh37-link timeouts preserve structured failure stages and
  matching-link counts. This improves diagnosis of the three unresolved link
  timeouts without guessing which page the work PC displayed.

## What remains uncertain

The original logs do not contain the failed pages' DOM. The code defects above
are demonstrated with synthetic regression tests, but are not proof of the
cause of every logged timeout or missing MTBP row. Explicit database absences
are preserved. The COSMIC link timeouts still require a new run to distinguish
network/session trouble from changed page markup. No clinical variant is
accepted merely to eliminate an error.

Provider capture layouts, attachments and Edge profile configuration are not
changed by this slice. Tests use synthetic fixtures and no provider submissions.
