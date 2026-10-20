# Edge startup on the external work PC

The work PC can start installed Edge with remote debugging and an isolated
`--user-data-dir="%TEMP%\EdgeDebugTest"`. That establishes that local CDP works
for that test directory; it does not establish that the app's profile directory
is writable or that its broker finishes within the old three-second grace.

The app already passed `--user-data-dir` and automatically created provider
directories under `%USERPROFILE%\.archer-prosess\browser_profiles`. Missing
directories did not require copying Edge's normal profile. The old login error
replaced policy, profile, permissions and startup-timeout reasons with the same
generic RemoteDebuggingAllowed advice.

## Changes

- A clean Edge launcher exit (code 0) can be a broker handoff. The app now waits
  for the announced endpoint through its existing 20-second startup deadline.
  Nonzero exits retain the short failure grace and owned-process cleanup.
- Login, lookup and session-check startup use one diagnosed boundary. Expected
  CDP/filesystem errors include their type, concrete reason and selected provider
  profile. Arbitrary exception contents and page/credential data are not exposed.
- **Innstillinger → Tilkoblinger → Edge-profilmappe** provides an explicit folder
  choice. Blank keeps the previous profile location. **Bruk lokal mappe** selects
  `%LOCALAPPDATA%\.archer-prosess\browser_profiles` when available. The change
  is applied after saving, and sign-in, session checks and searches use the same
  saved base directory with separate provider subdirectories.
- Selecting another base directory invalidates previous access confirmations.
  Profiles are created when used. Existing directories are not moved, deleted,
  copied or silently replaced; a new location needs sign-in again.

## Work-PC verification

Update to the branch containing this fix. In **Innstillinger → Tilkoblinger**,
choose **Bruk lokal mappe** and **Lagre innstillinger**. Then use **Sjekk
innlogging** and the provider's **Logg inn** action. A persistent local folder
keeps browser sessions between launches; a temporary folder may be cleaned by
Windows. If startup still fails, the new error identifies the actual directory,
failure type and, for endpoint failures, the startup-log location to inspect.

Managed policy is never changed. Microsoft permits remote debugging when
[RemoteDebuggingAllowed](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/remotedebuggingallowed)
is enabled or unconfigured. A mandatory
[UserDataDir](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/userdatadir)
overrides the command-line directory, so the app continues to refuse that case
to preserve profile isolation. Target-level restrictions can also be imposed by
[DeveloperToolsAvailability](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/DeveloperToolsAvailability).

Validation uses synthetic failures and a fake startup clock, including endpoint
announcements at 5 and 19 seconds, deadline expiry and a nonzero exit. The
external PC's exact failure remains unverified until the updated app is tested
there; this change does not claim that the manual test identified it conclusively.

## Follow-up: existing DevTools endpoint reported as unreliable

The next work-PC error identified the preflight check of an existing Franklin
profile, before Edge launch. A real local Windows reproduction found a closed
loopback port timed out at 0.5 and 2 seconds, but returned explicit WinError
10061 after approximately 2.08 seconds when allowed 3 seconds. The former
half-second preflight therefore classified a stale announcement as uncertain
and blocked reuse of an idle profile.

The idle probe now waits up to 3 seconds. Only explicit connection refusal
allows reuse; responding, stalled, malformed or otherwise uncertain endpoints
remain protected. The check preserves the announcement file, and normal launch
removes stale metadata only after idle state is confirmed. If the probe still
fails, the message includes its local port and underlying error. No cookies or
profiles are reset, and other Edge/Citrix instances are not closed.

Final verification: **680 tests passed**, with two existing Pillow deprecation
warnings. Python compilation and Git whitespace checks passed. Independent
review covered startup diagnostics and profile ownership; synthetic connection
settings previews were checked at 920×600, 1024×640 and 1440×900.
