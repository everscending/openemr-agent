# AUDIT.md — OpenEMR System Audit

Full audit of the forked OpenEMR system, conducted before designing the
Clinical Co-Pilot AI layer (see [ARCHITECTURE.md](ARCHITECTURE.md),
[USER.md](USER.md)). Five dimensions: security, performance, architecture,
data quality, compliance/regulatory. Findings cite file:line evidence against
this tree (essentially upstream OpenEMR master — the fork adds only a pruned
base import and deploy commits, so these are properties of real code, not
injected bugs). Data-quality findings include live queries against the demo
database.

Severity legend: **Critical** (PHI exposure / patient-safety) · **High** ·
**Medium** · **Low** · **Positive** (a control that works and should be
preserved).

---

## Summary (key findings)

The audit's central conclusion: **OpenEMR is a sound platform to build on, but
it enforces authorization by role, not by patient — and that single gap shapes
the entire AI integration.** The five findings that most change the plan:

**1. No per-patient access control (Critical).** `AclMain::aclCheckCore()`
(`src/Common/Acl/AclMain.php:166`) decides access from phpGACL section/role
pairs plus a superuser shortcut — its signature contains no `pid`, provider,
care-team, or panel parameter (confirmed: a grep for
`provider|care_team|patient_panel|assigned` in AclMain returns nothing). Any
user with a chart-access role can open **any** patient's record. This is the
most important finding: an AI agent that inherits a user's role inherits
unrestricted patient reach. It is why the agent must be patient-context-bound
by construction, log every access, and treat scoping as a detective control
(audit) rather than assume the EMR prevents cross-patient reads.

**2. Reflected-origin CORS with credentials on the PHI API (Critical).**
`CORSListener.php:57` reflects the caller's own unvalidated `Origin` header
while sending `Access-Control-Allow-Credentials: true` (`:69`) — with an
in-code `@TODO: review security implications`. Any malicious website can make
credentialed cross-origin calls to the FHIR/REST API in a logged-in user's
browser. This directly threatens the agent's browser-embedded panel and must
be hardened (origin allowlist) before exposing the agent.

**3. Native audit logs the token, never the AI (Critical for compliance).**
OpenEMR's audit machinery is strong and on by default (§3 of the compliance
section), but REST/FHIR access is attributed to the OAuth token bearer — it
records *that token X read patient Y*, not that an AI mediated the access, what
the clinician asked, what the agent answered, or the verification verdict. A
compliant integration must add a separate decision/disclosure log. OpenEMR
already provides the HIPAA §164.528 hook for this — `recordDisclosure()` →
`extended_log` (`EventAuditLogger.php:567`) — and a BAA-gating precedent
(`library/MedEx/API.php:3107`) the agent's PHI-egress control should copy.

**4. Patient data is ambiguous and multi-sourced (High, patient-safety).**
68 of 132 `patient_data` columns default to empty string and 54 to NULL with
no semantic difference, so an agent cannot distinguish "no known allergies"
from "never recorded." Worse, medications live in three tables that can
disagree: in the demo data, patient 2's Lisinopril is `active=1` in
`prescriptions` but `activity=0` (discontinued) in `lists`, with no foreign
key reconciling them. Absence-based negatives and source conflicts are direct
agent failure modes.

**5. Uncached per-request bootstrap dominates latency (High).**
`interface/globals.php` re-loads all ~500 globals from the DB on every request
(full table scan + per-user settings query), with no caching layer anywhere in
`src/`. A parallel fan-out of ~6 FHIR reads pays this fixed ~100–300ms tax six
times with zero reuse — the dominant floor under the agent's <10s target.

**What works (preserve it):** crypto is genuinely sound (AES-256-CBC
encrypt-then-MAC, constant-time compare, `CryptoGen.php:195`); the audit
subsystem has tamper checksums, breakglass forced-logging, and disclosure
accounting, mostly on by default; the FHIR service layer is single-query, not
N+1; and the custom-module + Summary-Card event system gives a clean
integration seam that needs no core changes. The agent should build **beside**
this platform (separate service on the FHIR API), not inside it.

---

## 1. Security Audit

### Critical

**C1 — No per-patient access control.** `src/Common/Acl/AclMain.php:166`
`aclCheckCore($section, $value, $user, $return_value)` evaluates only phpGACL
section/value role pairs plus a global superuser shortcut (`:174`). No `pid`,
provider, care-team, or panel parameter exists in the signature or body.
Callers (`src/Patient/Cards/DemographicsViewCard.php:60`,
`CareTeamViewCard.php:86`,
`src/RestControllers/SMART/PatientContextSearchController.php:86`) gate on role
alone. *Impact:* an embedded agent inheriting any chart-access role can read
every patient's PHI — no data-scoping backstop. *Remediation:* enforce a
patient-scoping layer (care-team / assigned-provider filter) on every PHI
query; never rely on the ACL alone. *Agent implication:* this is the evidence
behind the architecture's stated panel-scoping limitation — the agent is
patient-context-bound and audits every access rather than assuming prevention.

**C2 — Reflected-origin CORS with credentials.**
`src/RestControllers/Subscriber/CORSListener.php:57,73` set
`Access-Control-Allow-Origin` to the caller's own unvalidated `Origin`, with
`Access-Control-Allow-Credentials: true` (`:69`) and an in-code
`@TODO: review security implications`. *Impact:* any malicious site can make
credentialed cross-origin PHI-API calls in a logged-in user's browser.
*Remediation:* validate `Origin` against a registered-client allowlist before
reflecting. *Agent implication:* must be fixed before the browser panel ships.

### High

**H1 — Session fixation: no session regeneration on login.** A repo-wide grep
for `session_regenerate_id` (excluding vendor) returns zero matches; the
`AuthUtils` login path never rotates the session ID after authentication
(independently confirmed by a second audit pass). *Impact:* a fixed/pre-auth
session ID survives the privilege transition to an authenticated PHI session.
*Remediation:* `session_regenerate_id(true)` immediately after successful
auth/MFA.

**H2 — MFA is not enforced by the global session guard.**
`confirmUserPassword()` calls `setUserSessionVariables()`
(`AuthUtils.php:488`) — populating the full authenticated session — *before*
any MFA check; the TOTP/U2F challenge lives only in
`interface/main/main_screen.php`, and `authCheckSession()` re-checks only
user/pass, never MFA completion. *Impact:* a client that posts valid
credentials then requests a different authenticated endpoint already holds a
valid session; MFA is a landing-page speed-bump, not a gate. *Remediation:*
gate the session guard on an `mfa_complete` flag. *Agent implication:* if the
agent service trusts an OpenEMR session as identity proof, MFA adds less
assurance than it appears.

**H3 — Per-IP brute-force throttle bypassable via `X-Forwarded-For`.**
`collectIpAddresses()` (`library/sanitize.inc.php:29-46`) keys the `ip_tracking`
throttle on a string containing the attacker-controlled `HTTP_X_FORWARDED_FOR`,
so a unique XFF per request yields a fresh counter and defeats
`ip_max_failed_logins`. *Remediation:* key on a trusted-proxy-validated client
IP; never trust raw XFF.

**H4 — Weak default password-hashing cost & insecure shipped defaults.**
bcrypt cost defaults to PHP's 10 (`AuthHash.php:113`,
`globals.inc.php:2310`); the dev compose ships `MYSQL_ROOT_PASSWORD: root`,
`OE_PASS: pass`, a live phpMyAdmin, and `oauth_password_grant: 3`
(`docker/development-easy/docker-compose.yml:14,67,84,146`). *Remediation:*
bcrypt cost ≥12 / tuned Argon2id; never expose the easy stack or password
grant beyond an isolated dev host.

### Medium

**M1 — Dynamic SQL identifier interpolation.** Most queries are parameterized,
but several interpolate identifiers/clauses: `library/ippf_issues.inc.php:75`
(`"REPLACE INTO lists_ippf_gcac SET $sets"`), `library/options.inc.php:233,3443`
(`ORDER BY $order_by_sql`), `library/patient.inc.php:346-352`. *Impact:*
injection if any `$var` originates from request input. *Remediation:* whitelist
column/order identifiers; audit each source.

**M2 — Verbose error display toggles.** `interface/globals.php:813,827,832`
force `ini_set('display_errors','1')` for debug modes 2–4; stack traces can
leak PHI/schema if left on. *Remediation:* hard-disable in production.

### Positive

Crypto is authenticated and correct: AES-256-CBC with encrypt-then-MAC
(`CryptoGen.php:195,200`), constant-time compare (`:251,302`). OAuth TTLs are
reasonable (access `PT1H`, auth code `PT1M`,
`AuthorizationController.php:110,712`). Password hashing uses
`password_hash`/`password_verify` with Argon2id available and `hash_equals`
throughout; timing-attack mitigation and `sodium_memzero` on password memory.
Login form sets anti-clickjacking headers; CSRF is HMAC-SHA256 over a
per-session secret; session cookies default `SameSite=Strict`.

*(One deliberate weakening to note: `SessionConfigurationBuilder::forCore()`
sets `setCookieHttpOnly(false)` at `:88`, so the main EHR session cookie is
JS-readable — any XSS yields token theft. `cookie_secure` also defaults false
for core/portal. Harden both for any non-localhost deployment.)*

---

## 2. Performance Audit

Target context: the agent's parallel fan-out of ~6 FHIR reads under a 10s
snapshot budget.

**P1 — Uncached per-request globals bootstrap (highest fixed cost).**
`interface/globals.php` (lines ~384–540), required by every entry point
including the FHIR dispatcher (`apis/dispatch.php`), runs
`SELECT ... FROM globals ORDER BY gl_name` (a full scan of 500+ rows) plus a
`user_settings LIKE 'global:%'` query and a `SET time_zone` round-trip on
**every request**. There is no caching layer (no `src/Common/Caching/`, no
Redis/APCu in `src/`). Each of the 6 parallel FHIR reads pays this ~100–300ms
tax independently. *Mitigation:* cache the assembled globals (APCu/Redis) keyed
by site+user; short-circuit bootstrap for token-authenticated FHIR requests.
Biggest single win.

**P2 — `procedure_result` has no patient index.** `sql/database.sql:10493` —
keyed only on `procedure_result_id`, `uuid`, `procedure_report_id`; no `pid`.
Assembling Observations requires joining `procedure_result → procedure_report →
procedure_order` to reach `pid`, so lab reads scale with total result volume,
not patient volume — the slowest of the six resources. *Mitigation:*
denormalized `pid` / covering index, or a materialized per-patient lab read
model.

**P3 — EAV "forms" system multiplies queries.** `forms` (`:2460`) is a registry
pointing at 38 `form_*` tables; assembling an encounter's clinical content is
one query per form row. Encounter-heavy patients drive query count linearly.
*Mitigation:* batch form retrieval or a denormalized encounter read model.

**P4 — Positive: the FHIR search layer is single-query, not N+1.**
`BaseService::search()` (`:487-520`) builds one JOIN query and maps rows in
memory; child observations are batched via a single `IN`-style field
(`ObservationService.php:313-320`). *Residual N+1:* avoid `_revinclude=provenance`
in the snapshot path — it constructs provenance per record
(`FhirServiceBase::getAll()` ~`:279`).

**Index coverage** is otherwise adequate on hot columns: `patient_data` (unique
`pid`/`uuid`), `lists` (`pid`,`type`), `form_encounter` (`pid_encounter`,
`encounter_date`). *Bottom line:* the read path is fundamentally sound; the
bootstrap tax (P1) and labs join (P2) are the real, bounded costs — both argue
for caching / a read model rather than live EAV assembly.

---

## 3. Architecture Audit

**Layers.** Four coexisting layers mid-migration: `/src` (~2,000 files, PSR-4
`OpenEMR\`, PHP ≥8.2 — modern services/events/controllers), `/library` (~524
procedural `.inc.php` legacy helpers), `/interface` (~1,048 files — UI that
mixes controller, business logic, and HTML in the same file), and `/templates`
(Twig) plus Smarty. A real service layer exists (`src/Services/*`, ~80 services
on `BaseService`) but `/interface` pages frequently bypass it and query the DB
directly — so there is no single clean seam inside core to intercept.

**Data access** spans three eras: legacy `sqlStatement()/sqlQuery()`
(`library/sql.inc.php`, ADODB), modern `QueryUtils` (a typed façade still over
ADODB), and `BaseService` (the healthiest repository-ish pattern, with
`UuidRegistry` and FHIR search builders). Doctrine DBAL exists via
`DatabaseConnectionFactory` but is `@deprecated`. New code should read through
BaseService/QueryUtils or FHIR — never raw `sqlStatement`.

**Integration points for the agent (the key section).** OpenEMR offers a
clean, sanctioned extension path that needs no core edits:
- **Custom modules** in `interface/modules/custom_modules/` with an
  `openemr.bootstrap.php`; `src/Core/ModulesApplication.php` bridges Laminas
  modules to the Symfony `EventDispatcher`. `oe-module-dashboard-context` is a
  working template.
- **The golden UI hook:** `src/Events/Patient/Summary/Card/RenderEvent.php`
  injects a card into the patient summary (`interface/patient_file/summary/`) —
  the intended way to embed the co-pilot panel, cleaner than an ad-hoc include.
- **API extension:** modules register routes via
  `src/Events/RestApiExtend/RestApiCreateEvent.php` and OAuth scopes via
  `RestApiScopeEvent.php`.
- **Recommended shape:** a separate service consuming the FHIR/REST API, plus a
  thin module that hooks the Summary Card event to render the panel. Confirmed
  by the deployment model — the stack is already multi-service Docker
  (`docker/development-easy/docker-compose.yml`: openemr, mariadb, couchdb,
  ldap, mailpit, selenium) with `rest_api`/`rest_fhir_api`/`oauth_password_grant`
  pre-enabled, so the agent drops in as one more compose service on the same
  network.

**Tech debt affecting extensibility:** `$GLOBALS`/`$_SESSION` used as a
service locator app-wide; simultaneous Laminas MVC + Symfony components + ADODB
+ Doctrine + Twig + Smarty; business logic embedded in UI controllers.
*Implication:* modifying core risks entangling with global state and two
frameworks — strongly favor the external-service approach.

---

## 4. Data Quality Audit

Evidence: live queries against `development-easy-mysql-1` plus schema from
`information_schema` and `sql/*`.

**D1 — Empty-string-as-default masks "not recorded" (highest impact).** Of 132
`patient_data` columns, 68 default to `''` and 54 to NULL, used
interchangeably with no semantic distinction. Demo patient 3 (Wanda Moore) has
`street/city/phone_home/email/race` all empty and `providerID=0`, while
patients 1–2 are populated — nothing flags the row as incomplete. *Failure
mode:* the agent states "no allergies / no phone" when the field was merely
never filled. *Mitigation:* treat empty AND NULL as "unknown"; never render a
blank clinical field as a negative assertion.

**D2 — Negatives are absence-based, not asserted.** The `lists` table has no
"no known allergies" marker — absence of an allergy row is the only signal;
demo patients 2–3 have zero allergy rows, and patient 1's single penicillin
allergy has `severity_al=NULL`, `reaction=''`. *Failure mode:* the agent
asserts "no allergies" for a merely-silent record — a drug-safety-grade error.
*Mitigation:* require an explicit NKDA sentinel; expose severity/reaction as
"not recorded" when empty.

**D3 — Clinical facts are largely free-text, not coded.** The one
`prescriptions` row has `rxnorm_drugcode=NULL`; all 5 `lists` medications have
empty coding and free-text titles (`Norvasc`, `Metformin`, `Lipitor`); the
`drugs`, `drug_templates`, and coded `lists_medication` tables are all empty.
Problems use retired **ICD9** codes (`ICD9:401.0`) or free text. *Failure
mode:* the agent must string-match brand names, cannot reliably
deduplicate/interaction-check, may mis-map deprecated codes. *Mitigation:*
require RxNorm on meds, SNOMED/ICD10 on problems; flag uncoded entries
low-confidence.

> *Update 2026-07-11 — partially stale; re-verified against the live DB.*
> This finding described the original 3-patient demo set. After the Synthea
> seed the dataset is largely coded: 689 `prescriptions` rows (~91% with real
> RxNorm CUIs), 633 `lists` medications (~99% coded). Still true: `drugs` and
> `lists_medication` remain empty, and the original patients (1–2) — used by
> several fixtures — remain free-text, so the failure mode survives for the
> uncoded tail. Separately, a live probe found OpenEMR's native drug–drug
> check unusable *regardless of coding*: it calls the NLM RxNav Interaction
> API, retired January 2024 (HTTP 404 today), and on that failure renders
> "No interactions found" — a silent clean pass
> (`controllers/C_Prescription.class.php:190-231`). The interaction-check
> blocker is therefore the knowledge source, not the data. See
> PRD_EXCEPTIONS.md E1 and ARCHITECTURE.md §5 Layer 2.

**D4 — Multiple authoritative sources disagree (consistency).** Medications
live in `prescriptions`, `lists` (type=medication), and `lists_medication`,
with no reconciling foreign key. In the demo, patient 2's Lisinopril is
`active=1` in `prescriptions` but `activity=0` (inactive) in `lists` — a direct
contradiction. *Failure mode:* the agent reports the med active or inactive
depending on which table it read. *Mitigation:* define source precedence and
reconcile/flag conflicts rather than silently picking one.

**D5 — Status & dates are subtle and unreliable.** `activity`/`active` flags
mark current-vs-resolved but are easily missed on a type-only filter;
`enddate`/`onset_date` are NULL across the demo; the `0000-00-00` zero-date
sentinel appears throughout upgrade scripts. *Failure mode:* resolved problems
shown as current; date math on `0000-00-00`/NULL yields garbage. *Mitigation:*
always filter `activity=1`/`active=1`; coerce zero-dates/NULL to "unknown."

**D6 — Staleness & thin dataset.** All demo encounters are dated 2014-02-01;
`patient_data` has no auto-updated freshness column. Only 3 patients / 3
encounters / 1 prescription exist — too sparse to exercise or trust agent
evaluation, and every row is obvious seed data. *Mitigation:* surface the
newest available timestamp and warn on staleness; populate realistic coded
records via `openemr-cmd import-random-patients N` (Synthea) before trusting
any eval.

---

## 5. Compliance & Regulatory Audit (HIPAA)

**CR1 — BAA / third-party PHI disclosure is the central obligation
(§164.502(e), §164.308(b)).** Sending PHI to an LLM provider is a disclosure to
a business associate. The codebase's only BAA precedent is `library/MedEx/API.php:3107-3156`,
which blocks activation behind an explicit "I have read and accept the Business
Associate Agreement" gate — the model the agent should copy. There is no
existing egress allowlist or minimum-necessary filter (§164.502(b)), and
FHIR/REST responses are full resource payloads, so an agent forwarding them
ships more than the minimum necessary. *The agent must guarantee:* a signed
BAA with no-training/no-retention terms, a MedEx-style affirmative egress gate,
minimum-necessary redaction before transmission, TLS in transit, and a
disclosure record per egress (CR2).

**CR2 — Native audit records the token, not the AI (§164.312(b), §164.528).**
REST/FHIR access is logged by `ApiResponseLoggerListener.php` into `api_log`/
`log` via `EventAuditLogger::recordLogItem` (`:642`); the `Event` DTO
(`Audit/Event.php:33-47`) captures user, patient_id, method, url, body,
response, timestamp. But this records *that token X read patient Y* — not that
an AI mediated it, the clinician's prompt, the agent's answer, or the
verification verdict. *Required:* a separate append-only decision log
capturing mediating agent + model/version, prompt, output, the minimum-necessary
payload sent to the provider, and verification outcome. OpenEMR already
provides the §164.528 accounting hook — `recordDisclosure()` → `extended_log`
(`EventAuditLogger.php:567-626`), UI at
`interface/patient_file/summary/record_disclosure.php` — which the agent should
call on every PHI egress.

**CR3 — Native audit foundation is strong and on by default (§164.312(b)) —
positive.** `enable_auditlog` defaults `'1'` (`globals.inc.php:2778`) with
patient-record/order/lab/query/http sub-flags all on; full bound-value SQL is
captured (`EventAuditLogger.php:446-452`); `LogTablesSink.php:63,83` writes
`sha3-512` tamper checksums verified by `audit_log_tamper_report.php`;
disabling auditing is itself audited (`:533-556`). **Breakglass** forces
logging even when auditing is off (`BreakglassChecker.php:39`,
`gbl_force_log_breakglass` default `'1'`). ATNA/RFC-5425 TLS syslog export
exists but is off by default. This is a solid base the agent extends rather
than replaces.

**CR4 — Breach-notification readiness is partial (§164.400-414).** The `log`
table + tamper report support post-hoc "who accessed what when" reconstruction,
but there is **no anomaly/threshold detection, access-review tooling, or
alerting** — detection is manual. The agent adds a novel breach surface (PHI in
provider infrastructure) native tooling cannot see; detection for that channel
must be built into the decision log.

**CR5 — Data retention & disposal is weak/inconsistent (§164.310(d)(2)).** No
retention/purge policy config exists; audit logs are never auto-purged
(unbounded but satisfies the 6-year floor). Deletion is inconsistent —
`interface/patient_file/deleter.php:82` hard-`DELETE`s while financial tables
soft-delete. *Agent implication:* define explicit retention/disposal for
prompts and outbound payloads, contractually bound at the provider.

**CR6 — Encryption & access controls (§164.312(a),(e)) — positive.**
Authenticated AES-256 at rest (`CryptoGen.php`); automatic logoff via `timeout`
default 7200s / portal 1800s (§164.312(a)(2)(iii)); unique user IDs on every
audit path; platform-wide TLS. The agent should reuse CryptoGen primitives for
any at-rest storage of prompts/responses and enforce TLS to the provider.

---

## Appendix — Method & Evidence

Findings were produced by five parallel dimension-specific reviews of the
codebase, with the data-quality pass additionally querying the running demo
database (`development-easy-mysql-1`). Security was covered by two independent
passes (an authentication/session deep-dive and a full data-exposure sweep)
that corroborate on session fixation and MFA enforcement. File:line references
are to this repository at audit time. The demo database state at audit: 3
patients, 3 encounters, 1 prescription, 9 list items, 9 users.
