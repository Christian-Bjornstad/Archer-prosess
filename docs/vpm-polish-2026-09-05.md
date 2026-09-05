# VPM Tolkning: UI and evidence capture corrections

Implemented against main at 562bbe0.

- Replaced the unsupported Qt stylesheet `:not(...)` selector that prevented
  the workstation theme from loading. Restored readable navigation, button
  hierarchy and focus states, and changed the visible application name to
  VPM Tolkning. Design review used ui-ux-pro-max; retained Segoe UI and the
  existing clinical blue palette for the Windows/Citrix environment.
- Added the pale orange merged comment field at Oversikt!E4:J7. Moved metadata
  into A:D. Regeneration preserves the patient comment and manual HSMD line.
  Overview row heights now account for wrapped text, with additional padding.
- MTBP retains one batch submission per patient. Capture the full report once
  with a geometry sidecar, then crop each exact variant row with its table
  heading locally. Never use a gene-only match. Missing/ambiguous capture
  identities remain retryable. Long full-report images retain readable width.
- Recovery retains a shared remote report until all pending variants have
  been handled; full captures are reused within the recovery operation.
- CDP screenshots with a nonzero clip now capture at document origin and crop
  using actual pixel dimensions, accounting for zoom and device scaling.
  This applies to both Franklin classification views and other clipped images.

## Validation and rollout

Unit/regression tests cover scaled crop coordinates, exact MTBP selection,
duplicate rejection, comment preservation, wrapping and shared-report recovery.
`scripts/verify_capture_locally.py` exercises real Edge at 80%, 100%, 125% CSS
zoom using a local fixture with no database requests. Offscreen Qt renders were
checked at 1180x760. The current Citrix application was inspected but has not
been replaced with this build. A new run is required to replace old captures;
regenerating Excel alone reuses previously captured files.

## Performance decision

Local MTBP cropping removes per-variant browser captures and associated waits
without increasing requests. Keep the existing serial patient/provider queue
and request delays for this release. A future bounded concurrency change should
run at most two different providers, one worker/session per provider, with a
single writer for checkpoints and explicit pause/cancel/recovery tests. Actual
runtime improvement must be measured on the four-patient trial after rollout.

## Follow-up: observed Citrix failures

The second Citrix inspection confirmed a nested MTBP scrolling panel and
ClinVar's `self-signed certificate in certificate chain` failure. Further
real-Edge testing exposed an adapter bug: `page.evaluate` returned JavaScript
function objects rather than invoking them, so report geometry became `{}`.

- Invoke function expressions, including async functions, while retaining normal
  expression/statement evaluation. Local Edge exercises all three forms.
- Temporarily expand nested scrolling ancestors during MTBP parsing/capture and
  both Franklin classification overviews. Restore original styles and scrolling
  afterward. Include visible text separators in MTBP variant identities.
- Retry NCBI certificate failures with the Windows/system trust store. Continue
  verifying certificates and hostnames; a genuinely untrusted chain still fails.
- Remove the instruction label above E4:J7. Preserve the comment field itself.
- Keep search controls outside the content scroller. Add a resizable activity/
  full-log panel, readable disabled buttons, wrapped activity messages, and a
  read-only evidence detail window on double-click.

Real-Edge local fixtures confirm complete nested-scroll capture, exact second-row
MTBP cropping, Franklin's gene header after horizontal scrolling, and restoration
of the original scroll position. Citrix's installed build and actual NCBI trust
chain require verification after updating to this commit; existing captures are
not repaired by merely regenerating Excel.

## Follow-up: Edge startup and compressed Import layout

The live Import page showed overlapping input rows and clipped log text at
Citrix window height. Import now scrolls, with explicit row spacing, readable
control minimum heights and a minimum log height. A short-window regression
checks that inputs cannot overlap.

All provider launch exceptions previously became an unsupported "profile in
use" claim. They now retain the actual startup error. DevTools HTTP/WebSocket
connections bypass proxies only for the local browser control channel. Edge
startup output is retained in the provider profile's `vpm-edge-startup.log`;
exit/timeout errors include the actual connection error, exit code when known,
and the log location. No profiles, cookies or unrelated Edge processes are
deleted. The particular Citrix startup failure still needs reproduction with
these diagnostics; a lock was not proven by the old dialog.
