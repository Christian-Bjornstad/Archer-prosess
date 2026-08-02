# Priority database automation research

Verified 2026-07-31 using public, non-patient test data only.

## Decision summary

| Source | Verified access | Recommended integration | Blocker |
| --- | --- | --- | --- |
| OncoKB | REST API and public API metadata; demo annotation works for BRAF, TP53 and ROS1 | Keep the existing REST adapter and obtain a hospital/patient-services licence plus bearer token | No token is configured locally; clinical use requires the appropriate OncoKB licence |
| Franklin | Public SNP result pages work without login; supported API authentication exists | Use the existing API adapter when Franklin Premium is enabled; otherwise open the exact public page and retain manual review | API access is a Premium feature; the public page uses undocumented internal endpoints and must not be treated as a stable API |
| MTBP | Current site uses Keycloak login; accepts variant lists/VCF and produces HTML reports | Login-assisted visible-browser batch workflow with pseudonymized variants and fail-closed report parsing | Public instance is explicitly research-only and cannot support routine clinical reporting |
| HSMD | Licensed web application with limited named users | Request an API-enabled QIAGEN licence or written approval for browser automation | No public supported API was identified; licence terms control automation and reuse |

## Live checks

### OncoKB

- `GET https://www.oncokb.org/api/v1/info` returned HTTP 200 without a token.
- The response reported data version `v7.4` dated `07/31/2026` and API version `v1.6.0` during this check.
- `GET https://demo.oncokb.org/api/v1/annotate/mutations/byProteinChange?hugoSymbol=BRAF&alteration=V600E` returned a complete demonstration annotation.
- Production annotation remains bearer-token protected. The demo instance contains full information only for a small demonstration gene set and is suitable for contract tests, not clinical lookup.

Implementation notes:

- Pass `hugoSymbol`, normalized protein alteration, reference genome, and—when available—OncoTree tumor type.
- Preserve the complete JSON and `dataVersion` in the audit record.
- Distinguish `geneExist=false`, `variantExist=false`, unauthorized, and an annotation with no actionable biomarker.
- Treatment levels without a tumor type are pan-cancer context and must not be displayed as patient-specific recommendations.

### Franklin

- `GET https://api.genoox.com/v2/search/snp/` without a bearer token returned an authorization error, confirming that the existing API client cannot run anonymously.
- The browser search for `chr7-140453136-A-T` resolved directly to:
  `https://franklin.genoox.com/clinical-db/variant/snp/chr7-140453136-A-T`.
- The public page displayed BRAF `c.1799T>A`, `p.Val600Glu`, transcript `NM_004333.6`, suggested ACMG classification and evidence rules without login.
- Franklin's documented username/password-to-token flow is compatible with the current client shape, but current Franklin documentation states that API access is part of Premium.

Implementation notes:

- Prefer the supported API for automated extraction.
- Treat the public page as a review surface, not a supported API contract.
- The current browser adapter searches by gene plus transcript HGVS, selects the matching/canonical transcript result, and verifies returned HGVSc/protein identity. It deliberately imports only the suggested classification; ACMG rule lists are retained neither in the summary nor structured evidence.
- Direct genomic result URLs are not constructed from Archer `Ref/Alt Allele`: TP53 testing showed these values can be transcript-oriented and produce the wrong reverse-strand genomic allele.
- Anonymous Franklin use is limited (the current user observed 15 searches). Saved browser credentials avoid relying on that anonymous allowance.
- Fail closed when a required section or normalized variant identity changes.

### MTBP

- The former `mtbp.herokuapp.com` address in the app was obsolete.
- `https://mtbp.org/analyse/` redirects to a Keycloak login.
- MTBP supports SNVs, small indels, copy-number alterations and fusions through VCF or free-text variant lists.
- The public portal states that it is for academic research only. Its FAQ directs requests for programmatic or local access to `mtbp@scilifelab.se`.

Validated login-assisted workflow:

1. Export only gene/variant data; exclude sample and patient identifiers.
2. Store the login password in Windows Credential Manager or log in interactively; never store it in application JSON.
3. Submit one pseudonymized batch using transcript-qualified HGVS first.
4. When MTBP rejects transcript mapping, retry only those entries as GRCh37 genomic HGVS derived from the Archer position/ref/alt; remove only entries rejected in both forms.
5. Run MTBP after all other browser databases and wait up to the configurable report timeout (20 minutes by default).
6. Capture the report URL, screenshot and structured audit JSON.
7. Verify every returned alteration against the submitted normalized variants.
8. Import functional class, evidence category, actionability tier, source links and pipeline/database versions.

## Required provider questions

### MTBP

- May Oslo University Hospital use the public portal output in patient-service workflows?
- Is programmatic submission/report retrieval available for our institution?
- Is a local or production instance available?
- What input/output retention and rate limits apply?
- May generated HTML reports be stored in the clinical audit trail?

### Franklin

- Does the current subscription include Premium API access?
- Which base URL and environment should be used for production?
- Is the `/v2/search/snp/` endpoint included in the agreement for clinical use?
- Are public clinical-database pages permitted to be opened or parsed automatically?

### OncoKB

- Obtain the hospital/patient-services licence and API token.
- Confirm annual report volume and whether reanalysis is covered.
- Confirm whether treatment descriptions and evidence text may be retained in local reports.

### HSMD

- Ask QIAGEN whether the current HSMD subscription includes API-enabled access.
- If it does not, request a supported batch/API export before considering browser automation.
- Confirm the number of automation/service accounts allowed and the permitted retention of evidence text/screenshots.

## Implemented browser foundation

The application now includes a serial visible-Edge workflow with isolated,
persistent provider profiles and optional passwords encrypted by Windows
Credential Manager. OncoKB and Franklin web lookups have been tested end to end
with synthetic data. Franklin resolves transcript HGVS through its search UI,
checks returned identity, and saves only the classification plus screenshot/JSON
audit evidence.

OncoKB browser credentials can also be saved in Windows Credential Manager.
Database and browser searches expose an **Included variants only** option,
enabled by default, so filtered records are not submitted unnecessarily. The
report workbook provides a compact included-variant view and one normalized row
per database result, with source-page and captured-screenshot links.

The application can also create one patient-level PDF per validated DIT
(`YYOUM#####`) using only included variants. The PDF keeps source links,
classification/significance, capture timestamps, MTBP pipeline/cancer-type
provenance, review flags, and a physician conclusion/sign-off area. It is a
decision-support summary and does not generate a diagnosis or treatment
recommendation.

MTBP was validated with synthetic `TP53:p.R175H` data and with the rejected
`CEBPA`/`EZH2` transcript cases using an authorized account. For the latter, the
portal accepted the automatic GRCh37 genomic fallbacks, queued the job, and
returned both variants. The adapter recognizes duplication protein notation,
validates each returned gene/variant identity before importing evidence, and
retains every attempted query in the audit JSON.

HSMD has been removed from the active login-based browser workflow for now. It
remains available only as a manual evidence source while institutional access is
clarified.

## Next implementation gate

The MTBP integration supports either an interactive login/MFA session or saved
credentials protected by Windows Credential Manager. The normal database-search
button automatically continues into selected login-based sources after the API
phase, while the dedicated browser button remains available for reruns. Use only
non-identifying variant data and retain the portal's research-only warning with
every imported audit record.
