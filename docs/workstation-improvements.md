# Workstation improvements — 7 October 2026

## Everyday use

1. Drop a TSV onto **Slipp filen her**, or use Browse. Create the review workbook.
2. Open it in Excel and mark unwanted searches with X. Continue to Evidence and
   load the reviewed selection workbook.
3. Click **Sjekk innlogging**. ClinVar has public access; the other sources need
   a confirmed session. **Ikke bekreftet** means access could not be proven.
   Select a provider and Sign In when needed, then repeat the check.
4. Run all pending work or prioritize selected patients. The view moves to
   patient progress; cells update per variant and preserve selection/focus.
5. Scroll horizontally on smaller displays to read all source/status columns.

Import and source settings are locked during work. A live worker also prevents
the app from being closed before it can release its resources. Stop Search
ends an evidence run safely; completed results remain available. Attachment
generation, workbook layout and existing report behavior have not changed.

## Managed Edge and Citrix

The app continues to use installed Microsoft Edge directly over local CDP.
It requires no Node.js, Selenium, WebDriver or downloaded browser runtime.
Local DevTools traffic ignores environment proxies and refuses redirects.

The preflight reads **RemoteDebuggingAllowed** and **UserDataDir** from the
Windows registry. A disabled debugging policy prevents automation. A mandatory
UserDataDir overrides the app's isolated profile argument, so startup refuses
that configuration before it can open the workstation's ordinary Edge profile.
Errors name the policy/profile condition to discuss with IT; the app never
changes policy, disables TLS checks or closes other Edge windows.

Microsoft documents these policy behaviors in
[RemoteDebuggingAllowed](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/remotedebuggingallowed)
and [UserDataDir](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/userdatadir).

## Search reliability

- ClinVar waits for completed result rows/record identity before deciding
  whether a variant exists. Incomplete rendering remains retryable.
- Confirmed mismatched ClinVar records are not reopened by later fallback queries.
- COSMIC retries transient network/rendering errors once and requires exact
  identifier matching; ambiguous or conflicting results remain rejected.
- Franklin distinguishes authentication, layout/ambiguity and transient errors
  before choosing whether to retry. Explicit genomic identity conflicts reject
  a textual protein/cDNA match.
- Persisted per-variant snapshots are sent to the UI before the next lookup.
  Excel writes remain grouped per patient; cancellation keeps completed evidence.

## Validation boundary

Development used synthetic browser responses, local regression tests and
offscreen PyQt renders at 1024×640 and 1440×900. The final suite passed all 466
tests; compilation and whitespace checks also passed. Independent review found
no remaining actionable issues and passed 113 focused checks. The two suite
warnings are existing Pillow deprecations in capture regression tests.

The existing Citrix run and live provider sessions were
not inspected or altered because this chat's DOM browser inventory did not
expose them. Work-PC policies, real sign-in redirects and current provider
layouts still need a live check in the intended job environment after this
branch is installed. The sign-in checker reports unknown rather than guessing
when a site's authenticated marker cannot be identified.
