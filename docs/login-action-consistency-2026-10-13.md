# Consistent login and access checks

The work-PC report identified that login under **Avansert** worked, while the
source controls did not. The source button previously selected its operation
from a cached status: it used login only for `login_required`, and otherwise
checked existing access. An `unknown` or `error` state kept repeating checks
instead of providing the visible login operation. That check also always
minimized Edge, ignoring the saved visibility setting.

Each authenticated source now has separate, fixed **Logg inn** and **Sjekk**
actions. Source login and Advanced login call the same `_start_browser_login`
function and worker. Both validate/save browser choices, capture the explicit
provider, use the selected profile directory and launch visible sign-in. ClinVar
is public and retains only **Sjekk**.

Single-source and all-source checks remain read-only and use the saved profile
directory. An unsaved folder choice must be saved, or applied by starting login,
before a check uses it. Checks now honor the saved visible/background choice.
They do not fill credentials, submit variants or pretend that unknown access is
authenticated.

Source, Advanced and global access controls share operation ownership. Controls
stay disabled until the owning thread exits, and refresh after background Excel
writes so they cannot stay disabled after resources are released. Existing
profile locks, endpoint checks, cookie storage, lookup/report generation and
attachment formats are unchanged.

Regression tests compare both actual Qt button paths for all four login sources
and five cached states, observing provider, saved profile and browser settings
at the service boundary. They also cover stable check behavior, saved visibility,
and worker/background-save release. Validation uses synthetic sign-in responses;
the new source login buttons still need confirmation on the external work PC.

Verification: **706 tests passed**, with two existing Pillow deprecation warnings.
Python compilation and Git whitespace checks passed. Synthetic native previews
were reviewed at 920×600, 1024×640 and 1440×900; the README image was updated.
