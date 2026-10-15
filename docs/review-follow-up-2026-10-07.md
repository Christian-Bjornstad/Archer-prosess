# Review follow-up and editable reference lists

The second review covered worker ownership, delayed persistence, restored
evidence identity, managed Edge timeouts and reference-list/settings input.
It used synthetic data and offscreen Qt; the active Citrix run was untouched.

## Correctness fixes

- Finished Qt workers release their owning references. Repeated workbook
  resume and mixed search Stop/Pause no longer dereference deleted objects.
- Search completion retains analysis ownership until the final workbook save
  and queued priority reports finish. Save failure preserves completed evidence
  and permits an explicit update retry. A new analysis/search clears stale
  pending report IDs.
- Audit schema 3 records sample and complete requested variant identity in
  both the payload and file digest. Restore rejects another sample or an
  explicit genomic conflict. Modern audits remain usable after patient-folder
  renumbering; identity-poor legacy files are limited to their patient folder.
- Distinct records sharing the existing sample/HGVSc evidence key are rejected
  before import, resume or provider search. Compatible duplicates remain valid.
  Report layouts and their key contract are unchanged.
- CDP socket timeouts retain their timeout type and cleanup still releases
  sockets/processes/profile leases. Unrelated events do not restart a command's
  receive timeout.
- Corrupt reference workbooks become validation errors and release file
  handles, allowing replacement on Windows. WHO CSV loading respects a named
  gene column and quoted delimiters. Invalid records/formulas report row errors.
- Invalid saved settings recover field by field with visible warnings while
  preserving other valid settings. Expected Credential Manager/save errors no
  longer escape GUI callbacks; saving recent-history information is nonessential.

## Reference-list behavior

See [reference_lists/README.md](../reference_lists/README.md) for usage.
The two editable Excel tables exactly reproduce all 51 existing artifact rules
and 54 WHO genes. Threshold zero and precision are preserved. Explicitly empty
manual rules remain empty rather than silently restoring default filters.

The hardcoded constants remain available. No new clinical rules or WHO content
have been introduced. Attachment/report generation and layout are unchanged;
only reference loading and GUI operation coordination are affected.

## Verification

The review findings were reproduced with synthetic regressions. The final
complete suite passed **558 tests**, with two existing Pillow deprecation
warnings. Python compilation and Git whitespace checks passed. Both reference
files match the built-in content exactly, retain Excel tables/frozen headers,
and accept additional records beyond the original table range. Offscreen Qt
previews were inspected at 1024×640 and 1440×900. Live managed Edge/provider
behavior still requires validation on the intended work PC.
