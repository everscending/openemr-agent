# ARCHITECTURE.md — Clinical Co-Pilot Integration Plan

How the Clinical Co-Pilot will be built into this OpenEMR fork. The target
user, workflow, and use cases this design serves are defined in
[USER.md](USER.md); every capability here traces to a use case there.
Audit findings that shaped this plan are in AUDIT.md.

---

## Summary

The Clinical Co-Pilot is a chart-embedded conversational agent for a primary
care physician doing pre-visit review: it answers "what do I need to know
about this patient right now?" in seconds, with every claim cited to a
specific record, inside the 90-second window between patients.

**Shape.** The system is a thin OpenEMR custom module plus a separate Python
agent service. The module lives in the patient chart (OpenEMR's custom-module
system, `interface/modules/custom_modules/`), inherits the logged-in session,
and hands the agent service a SMART-on-FHIR OAuth token bound to the current
user *and* the currently open patient. The agent service runs the LLM
orchestration loop, a small registry of read-only tools, and a verification
layer, and exposes `/health` and `/ready` endpoints. I rejected an in-process
PHP agent: PHP's agent/eval/observability ecosystem is weak, long LLM calls
would pin Apache workers, and a separate service scales independently and
gives a clean trust boundary.

**Authorization is inherited, not re-implemented.** The agent reaches patient
data exclusively through OpenEMR's existing FHIR R4 API using the user's
token, so the EMR's ACL engine and OAuth scope enforcement decide what the
agent can see — a nurse's token sees what a nurse sees. The agent holds no
database credentials. (V1 demo caveat, disclosed: the relay presents a
service-account token, with per-request ACL checks at the relay seam as the
compensating control — §11.10, PRD_EXCEPTIONS.md E2.) It is additionally patient-context-bound: it can only be
invoked from an open chart and only queries that patient. Known limitation,
stated up front: OpenEMR's ACL is role-based, not panel-based — any
chart-access role can open any chart, and the agent inherits exactly that
boundary. It does not widen the aperture; it does not narrow it either.

**The agent is read-only.** Zero write access to the EMR in v1. A read-only
agent can produce a bad summary; a writing agent can corrupt a chart. This
also collapses most prompt-injection risk: retrieved chart content is treated
as untrusted input, and injected instructions have no write tools to escalate
into.

**Verification is deterministic in the hot path.** Tools return structured
FHIR resources with IDs; the agent must cite a resource ID for every clinical
claim; a non-LLM post-processor verifies every cited ID actually appeared in
this request's tool results. Uncited or falsely-cited claims fail closed.
Numeric claims are additionally checked against the cited resource's fields
where parseable. This verifies *grounding*, not *entailment* — a claim can
cite a real lab while misstating it. Full semantic checking (LLM-as-judge)
runs in the offline eval suite, not per-request, because a judge would double
latency and add a second model to trust. I can state precisely what the
verifier catches and what it does not.

**Speed over exhaustiveness, explicitly.** A single parallel snapshot tool
serves the default question in one agent step (targets: first token < 3s,
complete < 10s — to be validated under load, not promised). Deeper questions
trigger targeted tool calls on demand. The governing invariant for all
failure modes: **the physician always knows what the agent did NOT check** —
a failed lab fetch produces "labs unavailable," never a silently narrower
summary.

**Accountability.** Every invocation carries a correlation ID through every
log line, tool call, and LLM interaction; is written to OpenEMR's own audit
log (`EventAuditLogger`) with user, patient, and timestamp; and is traced in
LangSmith — via vendor-neutral OpenTelemetry instrumentation, so the trace
backend is swappable by design (§7). PHI in traces is covered by the assumed
BAA over all third-party services; trace masking applies minimum-necessary
anyway.

*(~500 words)*

---

## 1. System Overview

```mermaid
flowchart LR
    subgraph OpenEMR
        UI[Chart panel<br/>custom module] --> AUTH[OAuth2 / SMART<br/>AuthorizationController]
        FHIR[FHIR R4 API<br/>src/RestControllers/FHIR] --> ACL[ACL + scopes<br/>AclMain / ScopePermissionParser]
        AUDIT[(EventAuditLogger)]
    end
    subgraph Agent service — Python/FastAPI
        API[/chat endpoint<br/>+ /health /ready/] --> ORCH[Agent loop<br/>tool registry]
        ORCH --> VER[Verification layer<br/>citation + numeric checks<br/>+ CDR rule flags]
    end
    UI -- "SMART launch token<br/>(user + patient context)" --> API
    ORCH -- "FHIR reads with<br/>the user's token" --> FHIR
    ORCH --> LLM[LLM provider<br/>BAA]
    API -- "OTel export" --> LS[(LangSmith<br/>BAA-covered, swappable)]
    API -. audit events .-> AUDIT
```

Request lifecycle: physician opens a chart → panel loads with session context
→ physician asks a question → panel calls agent service with the SMART token
and a fresh correlation ID → agent loop plans, calls tools against OpenEMR's
FHIR API with that token, generates a cited draft → verification layer checks
citations, numerics, and clinical-rule flags → response streams to the panel
→ invocation is audit-logged in OpenEMR and traced in LangSmith.

## 2. Components

| Component | Description | Codebase anchors |
|---|---|---|
| **Chart panel** | Custom module `oe-module-clinical-copilot` (`openemr.bootstrap.php` entry point), rendered as a **persistent right-hand column on the patient Dashboard tab** (`demographics.php`) — **no core edits**. Mechanism: the module subscribes to a Dashboard render event (`PatientDemographics\RenderEvent`, e.g. `EVENT_SECTION_LIST_RENDER_AFTER`) and its PHP listener echoes an on-disk asset bundle (HTML fragment + CSS + JS) inline into the page; the JS wraps the existing dashboard (`#container_div`) into a narrower left region and adds the co-pilot as a new right column (same-document injection, not an iframe). Panel markup reuses OpenEMR/Bootstrap standard classes (`.card`, `.btn`, form controls) so it inherits native styling. Chat UI; v1 ships **render-only citation chips** (§5 Layer 3 status note — evidence cards and open-in-chart navigation deferred, §11.8/T035). **Rejected alternatives:** a Summary *card* via `Card\SectionEvent` (renders below the fold, not prominent); a surgical `demographics.php` layout edit (works, but forfeits the clean drop-in — the docs' preferred shape, AUDIT.md:235); an iframe (loses direct `window.top` access needed for §5 Layer 3 citation navigation, though same-origin would have allowed it). | `src/Events/PatientDemographics/RenderEvent.php`, `src/Core/ModulesApplication.php`, `interface/patient_file/summary/demographics.php` (`#container_div` anchor), `src/Common/Session/PatientSessionUtil.php` |
| **Token handoff** | Design: a SMART-on-FHIR token scoped to the logged-in user and the open patient, short-lived, per-conversation. **V1 demo (disclosed downgrade, 2026-07-10):** the panel POSTs to a same-origin module relay (session-authenticated, CSRF-protected, per-request ACL check on the open patient) which presents a **service-account** OAuth2 token to the agent. Per-user SMART EHR-launch is deferred (T027; §11.10; PRD_EXCEPTIONS.md E2). | `src/RestControllers/AuthorizationController.php`, `src/RestControllers/SMART/ScopePermissionParser.php` |
| **Agent service** | Python/FastAPI. Minimal explicit tool-use loop (no heavy multi-agent framework — single agent, small tool set; multi-agent adds coordination failure modes no use case requires). Pydantic schemas are the contract for every tool input/output — contracts are the source of truth, not the implementation. Correlation-ID middleware. OpenTelemetry instrumentation (vendor-neutral; §7). `/health` (process alive) and `/ready` (OpenEMR FHIR, LLM provider, trace backend reachable — real checks, not unconditional 200s). | new code, `agent/` |
| **Tools (read-only)** | `get_patient_snapshot` — parallel fan-out fetching demographics, active meds, problems, allergies, recent labs, last encounter in one step (UC-1). Targeted tools: `search_observations`, `get_medication_history`, `get_encounters_since`, `search_documents`, `get_immunizations` (UC-2/3/4). All are FHIR API calls with the user's token; none writes. | `src/Services/FHIR/Fhir*Service.php` (Patient, MedicationRequest, Condition, AllergyIntolerance, Observation, Encounter, DocumentReference) |
| **Verification layer** | See §5. | `src/ClinicalDecisionRules/Interface/`, `src/Services/DrugService.php` |
| **Citation resolver** | **Deferred (2026-07-11; T035 — designed and probe-verified, not in v1; see §11.8.)** Module-side endpoint (PHP, inside the co-pilot module) that maps a verified citation's FHIR uuid to its native chart location: `uuid_registry` → source table → row (native id, pid, encounter) → destination page per the §5 routing table. Returns a navigation descriptor (target URL + params + open mode) that the panel executes via the tab-framework JS. Read-only; runs in the user's session, so ACL applies to the lookup like any other read. | `src/Common/Uuid/UuidRegistry.php`, `src/Common/Uuid/UuidMapping.php`, `interface/main/tabs/js/tabs_view_model.js` |
| **Audit + observability** | See §7. | `src/Common/Logging/EventAuditLogger.php`, `AuditConfig.php` |

## 3. Where the Agent Lives

- **UI:** inside the patient chart, because the workflow moment (USER.md §2)
  is "chart already open, minutes before the visit." A separate app would add
  a login and a context switch to a 90-second workflow — disqualifying.
- **Compute:** a separate service, co-deployed with the OpenEMR stack (same
  compose file locally; same VPC in deployment). The same public deployment
  hosts both, satisfying the single-URL submission gate.
- **State:** conversations are keyed by correlation ID and scoped to
  (user, patient, session). v1 holds conversation state in the service
  (single instance); the state interface is Redis-shaped so horizontal
  scaling is a configuration change, not a redesign (§10).

## 4. Authorization & Trust Boundaries

Three trust boundaries, each with an explicit enforcement mechanism:

1. **Browser ↔ OpenEMR:** existing session authentication, untouched. *Two
   audit findings gate the browser panel (AUDIT.md C2, H1) and must be fixed
   before it ships:* the REST/FHIR API reflects an unvalidated `Origin` with
   `Allow-Credentials: true` (`CORSListener.php:57`), and login performs no
   `session_regenerate_id`. Both let a hostile page or fixed session reach PHI
   from the browser — the exact surface the panel adds. Remediation is an
   origin allowlist + session rotation; until then the panel is a same-origin
   module, not a cross-origin app. Two further findings weaken what that
   session *proves* and join the hardening list (AUDIT.md H2, §1 positives
   note): MFA is enforced only by the landing page, never by the session
   guard (`authCheckSession()` re-checks credentials, not MFA completion),
   and the core session cookie ships JS-readable with `cookie_secure` off
   (`SessionConfigurationBuilder::forCore()`) — so any XSS in the EMR yields
   the very session the panel mints SMART tokens from. Both matter more, not
   less, once the panel adds JS surface to the chart.
2. **Agent service ↔ OpenEMR:** OAuth per request. The agent service holds no
   standing credentials — no DB connection, no service account with broad
   read. Every FHIR call presents the requesting user's token, so OpenEMR's
   `AclMain` role checks and OAuth scope enforcement run on every read
   exactly as they would for any API client. Patient scoping uses the FHIR
   controllers' patient-bound query pattern (`puuidBind`). *V1 demo caveat
   (disclosed):* the relay currently presents a **service-account** token, so
   the acting user's authority is enforced at the relay seam (session + CSRF
   + per-request ACL check on the open patient), not re-enforced per FHIR
   read, and audit rows name the service user. Per-user SMART tokens restore
   the boundary as designed — §11.10, T027, PRD_EXCEPTIONS.md E2.
3. **Agent ↔ LLM provider:** BAA (assumed per project constraints) covering
   PHI in prompts; minimum-necessary applies — only the current patient's
   data, only categories relevant to the question.

**Prompt injection posture:** retrieved chart content (notes, documents) is
untrusted input — a document could contain adversarial text. Defenses, in
order of importance: (a) the tool set is read-only and fixed, so injected
instructions cannot escalate into actions; (b) tool results are delimited and
the system prompt instructs the model to treat record content as data;
(c) injected "facts" that don't correspond to real resources fail citation
verification. Residual risk: injection could still bias *emphasis* within a
summary — mitigated only by evals with adversarial documents, and noted
honestly as not fully solved.

**Known limitation — panel scoping.** OpenEMR enforces *what kinds of data a
role can see* (role ACLs, encounter sensitivity levels), not *which patients
a provider may access*: any chart-access role can open any chart. The audit
confirms this in code (AUDIT.md C1): `AclMain::aclCheckCore()`
(`src/Common/Acl/AclMain.php:166`) takes no `pid`, provider, care-team, or
panel parameter — a grep for those terms in AclMain returns nothing. The agent
inherits precisely this boundary. Mitigations: the agent operates only on the
currently-open chart (opening a chart is itself an audited act), and every
agent invocation is separately audit-logged, making misuse detectable
(a detective control, consistent with how deployed EMRs commonly handle
minimum-necessary). Preventive panel scoping — panel + relationship-based
exceptions + break-glass — is roadmap (§11), not v1.

## 5. Verification Strategy

Trust is the product. Layer 1 below runs as a post-generation filter — it
checks the LLM's output after the fact — but a filter can only check what it's
given something to check against. That's why the tool layer (§2) returns
structured resources with stable IDs, and the system prompt requires a
`[ResourceType/id]` citation on every claim: both were co-designed with the
agent loop specifically so this filter would have IDs to verify and tokens to
parse. Bolting verification onto an agent loop built without those affordances
would leave nothing deterministic to check against.

**Layer 1 — Source attribution (hot path, deterministic).**
- Tools return structured FHIR resources; every resource carries its ID.
- The system prompt requires a citation `[ResourceType/id]` on every clinical
  claim — and on **every resource that claim relies on**, not merely the one
  it is nominally about. A claim derived from another resource's value cites
  that resource too. This is what keeps inter-claim dependency deterministic:
  a claim resting on a fabricated source carries the fabricated ID and is
  stripped along with it. Dependency is expressed *as a citation*, because a
  citation is checkable against tool results and a self-reported "depends-on"
  edge is not. Omitting a citation fails closed (the claim is stripped);
  omitting an edge would fail open (the dependent claim would survive). We
  therefore never ask the model to declare its own reasoning structure.
- A post-generation, non-LLM check validates: every cited ID ∈ the set of IDs
  returned by this request's tool calls, keyed by **type + id** (citing a real
  ID under the wrong resource type fails). Cost: milliseconds; cannot itself
  hallucinate.
- Where a claim contains parseable numerics (value/unit/date), they are
  compared against the cited resource's actual fields.
- **Claim vs. scaffolding.** Not every sentence is a clinical claim: greetings,
  coverage statements ("I checked the labs"), section headers, refusals, and
  navigation lines carry no citation and are kept. The boundary is *what the
  sentence is about*, not how it is phrased: scaffolding is a statement about
  **the agent's process**; a claim is a statement about **the patient**. So the
  coverage whitelist admits only a retrieval verb over a *data category* (the
  categories the tools return), never an arbitrary predicate — "I confirmed she
  is allergic to penicillin" is a claim wearing a process costume, and is
  stripped when uncited. Likewise "no *<data category>* on record" is coverage,
  while "no evidence of malignancy" is a diagnostic conclusion. Unrecognized
  phrasing fails closed.
- **Fail closed:** a claim citing an unknown ID, citing an ID under the wrong
  resource type, or carrying no citation at all is stripped. The response is
  annotated — a machine-readable list of stripped claims with reasons, a
  user-visible marker that content was removed, and the `ResourceRef`s that
  *were* available, with deep links. Surviving claims are shown.
- **No fractional floor.** If no claim-bearing sentence survives, the response
  is replaced entirely with "couldn't verify — view source records" plus the
  available refs and deep links: there is nothing left to show. Otherwise the
  survivors are emitted with the removal annotation. There is deliberately no
  "N% stripped ⇒ discard everything" threshold — any such number would discard
  individually-verified claims on a statistical hunch about the response as a
  whole, and no principled value for it exists. Verification counts (claims
  total / passed / stripped, by failure kind) are emitted for observability,
  and the offline eval suite measures how often stripping guts a response.
  Whether that rate justifies a stricter whole-response fail-closed rule is a
  decision deferred to measured data rather than guessed in advance.

**Layer 2 — Domain constraints (hot path, rule-based).**
- **Corrected 2026-07-11, verified by live probe (PRD_EXCEPTIONS.md E1).**
  The original plan — routing medication responses through OpenEMR's own
  drug-interaction checking (`DrugService` / the `rx_show_drug_drug` path) —
  is not achievable: the native check
  (`controllers/C_Prescription.class.php:190-231`) calls the NLM RxNav
  Interaction API, which NLM **retired in January 2024** (live probe:
  HTTP 404); on that failure it renders **"No interactions found"** — a
  silent clean pass, the exact failure mode this layer exists to prevent —
  and it requires an uninstalled RxNorm table (`RXNCONSO`) that it uses only
  to `LIKE`-match the first word of free-text drug names, ignoring the
  stored RxNorm codes.
- **V1's domain-constraint layer, as enforced:** the refusal boundary
  (USER.md §4 — no dosing, treatment, or diagnosis); deterministic
  numeric/date verification of every claim against its cited resource
  (Layer 1 — a response that contradicts the underlying data is stripped);
  medication reconciliation with explicit conflict flags across disagreeing
  source tables, never silently resolved; a deterministic **drug–allergy
  name-level cross-check** (T033), attributed to the data rather than the
  agent's judgment, its no-cross-reactivity limit disclosed in its own
  status vocabulary; and explicit unchecked markers (`unavailable_uncoded`,
  `not_run`) wherever a check could not run — never a silent clean pass.
- Full drug–drug checking requires sourcing an interaction knowledge base
  (the retired API's data came from ONCHigh + DrugBank; RxNorm itself holds
  no interaction pairs) — roadmap (§11.11, T034).
- Data note, superseding AUDIT.md D3 for the current dataset: post-Synthea
  seed, ~91% of `prescriptions` rows and ~99% of `lists` medications carry
  real RxNorm codes — the blocker is the knowledge source, not the coding.
  The uncoded tail keeps the explicit `unavailable_uncoded` flag; requiring
  coded entries remains roadmap (§11.6).

**Layer 3 — Citation presentation (panel-side; show the proof, then link to
it).** The `[ResourceType/id]` token is the machine layer — it exists for the
Layer-1 verifier, not the physician; a raw uuid is unreadable and
un-actionable in a 90-second window. After verification, the panel replaces
each token with a three-tier UI:

> **Status (2026-07-11):** v1 ships tier 1 only — the **chip**, render-only
> and inert. The evidence card and open-in-chart navigation are **deferred**
> (T035): a scope cut, not a PRD gap — the PRD requires claims be traceable
> to specific records, which the chip provides; navigation traces only to
> this document. The probe-verified design below is preserved for
> reactivation. Consequences restated in §11.8.

- **Chip (always visible):** type icon + human identifier + date, rendered
  from the cited resource — `A1c 8.2% · 2026-05-14 · LabCorp`,
  `℞ lisinopril 20mg · started 2025-01`. Answers "where did that come from?"
  with zero clicks; record age is itself clinical signal (a 14-month-old A1c
  reads very differently from last week's).
- **Evidence card (hover/tap; no navigation):** the resource fields the claim
  rests on — value, units, reference range, status, dates — rendered directly
  from the tool result the agent already holds. Because the card renders from
  *data*, not model output, it survives model misstatement: even if the
  summary says "improving" of a worsening trend, the card shows the true
  numbers — a human check on exactly the entailment gap the hot path cannot
  close (below). Derived claims (trends, counts) list every contributing
  resource. **Absence claims** ("no colonoscopy on record") have no resource
  to cite — the chip carries a *query receipt* instead: what was searched,
  the scope, zero results, and the timestamp. Without this, a citation UX
  built only on resource IDs has nothing to show for the data-quality
  boundary cases in §8 ("no allergies **recorded**").
- **Open in chart (button on the card):** navigates to the native record via
  the citation resolver (§2).

Resolver mechanics: `UuidRegistry::getRegistryRecordForUuid()` maps uuid →
source table (`UUID_TABLE_DEFINITIONS`, `src/Common/Uuid/UuidRegistry.php:39`);
a row lookup on that table recovers the native id, owning `pid`, and (where
present) `encounter`; derived resources with no row of their own (e.g.
vitals-derived Observations) indirect through the `uuid_mapping` table
(`src/Common/Uuid/UuidMapping.php`) to their parent first. Routing table —
where each resource type lands and how it opens:

| FHIR resource | Source table | Chart destination | Open mode |
|---|---|---|---|
| Encounter | `form_encounter` | `demographics.php?set_pid&set_encounterid` → encounter tab | tab navigation |
| Condition | `lists` (type=medical_problem) | Medical Problems summary card (`#medical_problem_ps_expand`) | scroll-to + highlight (expand card if collapsed) |
| AllergyIntolerance | `lists` (type=allergy) | Allergies summary card (`#allergy_ps_expand`) | scroll-to + highlight (expand card if collapsed) |
| Observation / DiagnosticReport | `procedure_result` → parent `procedure_order` | `single_order_results.php?orderid=<id>` | dialog |
| DocumentReference | `documents` | `controller.php?document&view&doc_id&patient_id` | dialog |
| MedicationRequest | `prescriptions` | Medications summary card (`#medication_ps_expand`) | scroll-to + highlight (expand card if collapsed) |
| Immunization | `immunizations` | Immunizations summary card (`#immunizations_ps_expand`) | scroll-to + highlight (expand card if collapsed) |

Two OpenEMR constraints shape the open-mode column. First, there are no
bookmarkable deep links: `main.php` rejects foreign query params (single-use
session token) and inner chart pages read pid/encounter from the *session*,
not the URL — so citation links are in-app JS navigations
(`top.RTop.location`, `dlgopen`, `left_nav.loadFrame`), available to the
embedded panel and to nothing outside it. Second, tab navigation mutates the
active pid/encounter session state and replaces the physician's current view;
mid-pre-review that costs more than the citation is worth, so destinations
open as dialogs wherever OpenEMR supports it, and full tab navigation is
reserved for the targets that genuinely need native context (encounters).
Third — settled 2026-07-10, verified by live probe against the actual
Dashboard — **Condition and AllergyIntolerance moved from a dialog
(`add_edit_issue.php`) to the same scroll-to + highlight treatment as
MedicationRequest/Immunization**: both already have their own always-present
summary cards directly on the Dashboard (Allergies, Medical Problems), so
opening an *edit* dialog just to point at a citation is more intrusive than
necessary — a highlight is enough, and it doesn't imply the user should edit
anything. Mechanics for every scroll-to + highlight destination: each
Dashboard card (`templates/patient/card/card_base.html.twig`) is a Bootstrap 4
collapse component — a toggle `<a data-toggle="collapse"
data-target="#<id>">` and a body `<div id="<id>" class="card-text
collapse[ show]">`. Live-probed: many cards default collapsed (Labs, Vitals,
Demographics, etc.), so "expand if collapsed" is a real, not theoretical,
case. Clicking the toggle `<a>` (a real `.click()`, not a jQuery `.collapse()`
plugin call — probed live and confirmed more reliably wired) toggles the
`show` class with Bootstrap's own transition, regardless of collapsed state.
Within the card, individual rows (`templates/patient/card/{allergies,
medical_problems,medication}.html.twig`) carry **no identifying attribute** —
each is a plain `.list-group-item` with only display text. Rather than a core
edit to add one, the resolver returns the resource's display title (already
known from the row lookup) and the panel JS matches the row by text content —
approximate, but a soft failure (worst case: card expands, nothing
highlights) consistent with this feature being a visual aid, not an access
decision. The highlight itself: a green border applied to the matched row,
removed via a CSS `transition` on `opacity` to `0` over 3 seconds — no
blink/pulse, kept deliberately simple.
citation-existence verifies **grounding, not entailment** — the model could
cite a real lab while misstating its direction ("improving" vs. worsening).
Directional/semantic errors beyond parseable numerics are measured by an
**offline LLM-as-judge entailment eval** over a growing test set, not gated
per-request: an in-path judge would roughly double latency and add a second
model to trust. The eval suite (§8) is weighted toward exactly this failure
class because it is the one the hot path cannot fully catch. (Status: the
eval harness reserves this check name and fails loud if a case references it
— the judge itself is roadmap, not yet implemented; §11.2.)

## 6. Speed vs. Completeness

Latency budget (targets pending load testing — stated as design targets, not
measurements): **first token < 3s; complete snapshot < 10s; follow-ups < 5s;
p95 < 15s.** Validation is specified, not vague: load tests at 10 and 50
concurrent users record p50/p95/p99 latency and error rate at each level, and
baseline CPU/memory/latency/throughput profiles of both services are captured
under the same scenarios and checked into the repo as the reference against
which future performance changes are measured.

Design levers:
- **One snapshot tool, parallel fan-out** (UC-1): the default question costs
  one agent step, not five sequential tool calls.
- **Streaming** so reading starts before generation ends. (The service
  exposes buffered SSE — verification completes before claim content is
  emitted; the v1 panel renders a single JSON response, a deliberate demo
  simplification.)
- **Depth on demand** (UC-2): the agent does not exhaustively mine the chart
  up front; follow-ups trigger targeted tools.
- **Model tiering (roadmap, not in v1):** v1 runs a single model; a
  cheaper/faster routing tier is the first cost/latency optimization lever,
  priced in the cost analysis rather than shipped.
- **Uncertainty communicated, not hidden:** partial results ship with an
  explicit statement of what's missing.

The tradeoff, stated: fast-and-explicit over slow-and-complete. The physician
gets a bounded summary quickly plus an honest list of what wasn't checked.

**What the audit changed here (AUDIT.md §2).** The latency floor is *not* the
FHIR queries — that layer is single-query, not N+1 (`BaseService::search()`).
It is OpenEMR's **uncached per-request bootstrap**: `interface/globals.php`
re-loads all ~500 globals from the DB on every request, so a 6-way parallel
fan-out pays that ~100–300ms tax six times with zero reuse. Consequences for
the design: (a) the snapshot tool's parallelism helps wall-clock but not the
per-call fixed cost, so a **cached read model / a bootstrap short-circuit for
token-auth FHIR requests is the highest-leverage optimization** — identified,
not implemented in v1; it stays roadmap, to be revisited against measured
load-test data (T030) rather than built speculatively; (b) labs are the slowest
resource because `procedure_result` has no patient index (`sql/database.sql:10493`,
a 3-table join to reach `pid`), so the snapshot fetches labs on its own timeout
and degrades gracefully if they lag; (c) the snapshot path must **not** request
`_revinclude=provenance` — it builds provenance per record, doubling row work;
(d) encounter content lives in an EAV forms system — the `forms` registry
fans out to 38 `form_*` tables at one query per row (AUDIT.md P3) — so
`get_encounters_since` returns encounter metadata and notes in bounded
batches rather than assembling full form content, which stays behind an
explicit follow-up (UC-2) instead of inflating the interval-diff path.

## 7. Failure Modes, Audit, and Observability

**Governing invariant: the physician always knows what the agent did NOT
check.** A summary that omits a section must be distinguishable from a
summary that verified the section is empty.

| Failure | Behavior |
|---|---|
| One tool fails (e.g., labs) | Answer with remaining data + explicit "labs unavailable — this summary covers meds, problems, allergies" banner. Never silently narrower. |
| LLM provider down/slow | Timeout → non-AI fallback: the panel renders a plain structured snapshot straight from FHIR data (no model needed to show a med list). |
| Verification failure | Fail closed: strip/block + "couldn't verify — view source records" with deep links. |
| Empty/thin record | Stated affirmatively ("no encounters on file since 2023") — absence of data is clinical information (UC-4). |
| Malformed model output | Pydantic-validated at every step; parse failure → one retry → fallback. |

**Audit — two mechanisms, one trail.** The audit's compliance pass sharpens
*why* both are needed (AUDIT.md CR2): OpenEMR's native logging is an **access
log** — it records that a token read a patient, not that an *AI* mediated the
access, what was asked, what was answered, or the verification verdict. The
bridge adds the missing **decision/disclosure log**.
- *Data access:* every tool call is a FHIR API request as the authenticated
  user, so OpenEMR's existing audit machinery logs it natively — no new code.
- *Invocation events:* `EventAuditLogger` is a PHP class, not an API, so the
  agent service cannot call it directly. The co-pilot module — new PHP code
  living inside OpenEMR — exposes a small internal **audit bridge endpoint**;
  the agent service POSTs the invocation record (user, patient, correlation
  ID, verification outcome) and the module writes it via `EventAuditLogger`
  in-process with a custom event type (`ai-clinical-summary`), joining the
  same machinery OpenEMR uses for HIPAA access accounting (disclosure log,
  ATNA export). Routing the whole chat through the module instead was
  rejected: proxying streams through PHP pins Apache workers — the problem
  the separate service exists to avoid.
- *PHI egress is a HIPAA disclosure, and OpenEMR already models it (AUDIT.md
  CR2).* Sending patient data to the LLM is a disclosure to a business
  associate. The bridge therefore also calls OpenEMR's existing §164.528
  accounting hook — `EventAuditLogger::recordDisclosure()` → `extended_log`
  (`EventAuditLogger.php:567`) — recording recipient (the LLM provider),
  patient, and the minimum-necessary payload actually sent. Egress itself is
  gated behind a Business-Associate-Agreement affirmation, following the only
  such precedent in the codebase (`library/MedEx/API.php:3107`, which blocks
  its workflow until a BAA is accepted).
- *If the audit POST fails:* the response is not blocked — the invocation
  remains durably recorded in service logs (joined by correlation ID), which
  together with the EMR audit row are the **sole durable audit record**;
  disclosure accounting is reconstructable from them alone. The observability
  trace is deliberately *not* part of this chain: SaaS trace retention (days
  to weeks by default) is far shorter than HIPAA's six-year accounting
  horizon, and keeping the trace a disposable debugging aid is part of what
  keeps the backend swappable (see Observability below). The bridge failure
  raises an alert. Fail-closed applies to verification; audit delivery is
  fail-open-with-alarm.
- *Retention & disposal (AUDIT.md CR5, CR6):* the platform has no retention
  policy to inherit, so the agent defines its own. Conversation state is
  ephemeral — a TTL of hours, never persisted beyond the session. The
  decision/disclosure rows ride the EMR audit trail and its six-year HIPAA
  accounting horizon, and service logs (PHI-free pointers) are retained to
  the same horizon since together they are the sole durable record. Any
  at-rest storage of prompts or payloads reuses OpenEMR's `CryptoGen`
  primitives — the audit's strongest positive. Provider-side retention is
  contractually zero under the BAA's no-training/no-retention terms.

**Observability (LangSmith, BAA-covered — instrumented for portability):**
every step of every request is traced — tool calls with timings, LLM calls
with token counts and cost, verification outcomes — keyed by the correlation
ID that also appears in service logs and the EMR audit row, so a full trace
reconstructs from logs alone. Service application logs contain pointers
(correlation ID, resource IDs), never PHI content; trace content is masked
to minimum-necessary even under the BAA.

**Portability is a design requirement, not an aspiration.** The vendor is
intentionally replaceable, enforced by three rules: (a) the service emits
**OpenTelemetry** spans — agent code never imports a vendor tracing SDK;
LangSmith is an OTel exporter destination, configured, not coded against
(the one permitted exception is a thin adapter module owning any
vendor-specific eval-run API, so vendor surface stays in one file);
(b) the trace backend appears in `/ready` as "trace backend," a
configured dependency, not a named product; (c) the trace holds no
compliance role — audit and disclosure accounting live entirely in service
logs and the EMR audit trail (above), so trace retention limits and
platform migrations never touch the compliance story. Consequence: moving
to another backend (Braintrust, self-hosted Langfuse) is an exporter-config
change plus one adapter file — the "adapter swap, not a re-architecture"
claim in §9 is engineered here, not asserted.

**Dashboard & alerts:** real-time dashboard of request count, error rate,
p50/p95 latency, tool-call and retry counts, in-flight request depth (the
queue-depth signal; a literal queue arrives with request queueing at the 10K
tier, §10), token cost, and verification pass/fail rate. Alerts: p95 latency
> 15s (degraded UX — investigate tool/LLM latency); error rate > 5% over
10 min (page — check `/ready` dependencies); tool failure rate > 10%
(OpenEMR FHIR layer or auth problem; verify token issuance); cross-patient
access anomaly — one user's invocations spanning an unusual number of
distinct patients in an hour (flag for access review). That last one is not
optional garnish: §4 leans on detection as the compensating control for the
missing panel scoping, and the audit found the platform has no anomaly
detection of its own (AUDIT.md CR4) — so this channel ships its own detector.
Each alert documents its on-call response.

**Runnable API collection:** a Bruno collection, versioned in the repo like
the eval fixtures, covers `/chat`, `/health`, `/ready`, and the module's
audit-bridge endpoint — every core workflow exercisable without reading
source. (The citation-resolver endpoint was deferred with navigation,
2026-07-11 — T035; its request joins the collection if that ships.)

## 8. Evaluation Plan

Eval cases are structured by what they guard against — every case exercises a
boundary, an invariant, or a regression risk (no happy-path-only suite):

- **Invariants:** every clinical claim carries a valid citation (grounding);
  numeric claims match cited resources; failed-tool responses disclose
  coverage; refusal boundary holds (dosing/diagnosis requests refused;
  USER.md §4).
- **Boundaries:** empty patient record; patient with no labs; ambiguous
  questions ("how is she doing?"); questions about data categories the user's
  token can't reach (must refuse/disclose, not fabricate); adversarial
  content embedded in documents (injection); questions about a different
  patient than the open chart (must refuse).
- **Data-quality boundaries (grounded in AUDIT.md §4 — these are real
  properties of the data, not hypotheticals):** *absence-vs-negative* — a
  patient with no allergy row must yield "no allergies **recorded**," never
  "no allergies" (empty string and NULL are used interchangeably across 68+
  `patient_data` columns); *source conflict* — the demo's own patient 2 has
  Lisinopril `active=1` in `prescriptions` but `activity=0` in `lists`, so a
  test asserts the agent flags the conflict rather than silently picking one;
  *status filtering* — discontinued meds / resolved problems (`activity=0`)
  must not surface as current; *stale/zero dates* — `0000-00-00` and NULL
  dates must render as "unknown," never fed to date math.
- **Entailment (offline judge):** cited-but-misstated claims — the failure
  class the hot path can't fully catch — measured as a rate, tracked across
  prompt/model changes. (Roadmap: the harness reserves the check name and
  fails loud if referenced; the judge is not yet implemented — §11.2.)
- **Regressions:** every bug found in development becomes a pinned case.

Ground truth comes from the synthetic patient set (demo/Synthea data only,
per project constraints), where the "right answer" is known by construction.
Evals run in CI on every prompt or tool change. **The suite's source of
truth is the repo, not the platform:** cases live as versioned fixtures
alongside the code — the eval asset accumulates in git and is reviewable in
PRs. LangSmith is the runner and results-history viewer; its datasets are
synced *from* the fixtures, never authored in the platform. This keeps the
most accumulative (and otherwise most vendor-sticky) asset portable:
switching eval platforms means re-pointing the runner, not migrating the
suite (§7 portability rules).

## 9. Model, Framework, and Stack Choices

| Decision | Choice | Rejected | Why |
|---|---|---|---|
| LLM | Claude (BAA-compatible; strong tool use). V1 runs a single model; tiering (fast routing / stronger synthesis) is the first cost lever, priced in the cost analysis (§6) | Self-hosted open source | Self-hosting eliminates the BAA question but its quality/ops cost isn't justified in a one-week sprint with BAAs assumed; revisit at scale. |
| Agent framework | Minimal explicit tool-use loop | LangGraph-scale orchestration; multi-agent | Single agent, ~6 tools: a loop I can fully explain beats a framework I'd debug under deadline. No use case requires multi-agent coordination. |
| LLM client | Official Anthropic Python SDK, behind an `LLMClient` port | Raw HTTP against the Messages API; the SDK's built-in `tool_runner` | Hand-rolling the wire format buys nothing and owns the drift. The SDK lives in one adapter implementing the port — the same containment used for OTel (§7), so the loop imports no vendor SDK and the whole suite runs against a scripted fake with no network. The SDK's `tool_runner` would drive the loop for us, but §4's patient binding must read as one obvious line of our own code, not a hook overriding a runner's pending tool call; the retry-once-then-fallback and step-cap rules are likewise the control flow we want pinned by tests. |
| Generation determinism | None — steer with the prompt | `temperature=0` | Sampling parameters (`temperature`, `top_p`, `top_k`) are rejected by current Claude models, and `temperature=0` never guaranteed identical outputs anyway. This is why §5's verification is deterministic and the *generator* is not: we do not depend on reproducible generation, we depend on a deterministic, non-LLM check over whatever the generator produced. |
| Contracts | Pydantic on every tool I/O | Untyped dicts | Contracts as source of truth; malformed data fails at the boundary, not mid-conversation. |
| Service | FastAPI | PHP in-process | Ecosystem, non-blocking long calls, independent scaling, clean trust boundary (§ Summary). |
| Observability | LangSmith, behind portable instrumentation (OTel spans, repo-held eval fixtures — §7, §8) | Braintrust; self-hosted Langfuse | Criterion: PHI-bearing traces must be BAA-covered or in-boundary. Both LangSmith and Braintrust gate the BAA behind Enterprise contracts, so the criterion ties and tiebreakers decide: one platform for tracing + evals + dashboards, zero ops during the sprint, vendor maturity. Braintrust is the strongest challenger — best-in-class eval ergonomics and a hybrid data plane that keeps traces in-boundary — and is the likely destination if we later switch. The design makes that switch deliberately cheap: agent code emits OTel and never imports a vendor SDK, eval cases live in git, and the trace carries no compliance role — so changing backends is an exporter-config change plus one adapter file, not a re-architecture. Self-hosted Langfuse remains the fallback for a deployment without any BAA. |
| Data access | FHIR API with user token | Direct SQL / internal PHP services | Inherits ACL, scopes, and audit; SQL would bypass every enforcement layer and move authorization into my code. Cost: HTTP-hop latency, paid for by the parallel snapshot tool. |

## 10. Cost & Scale Outlook

Per-interaction estimate: snapshot ≈ 5–15k input tokens + ~1k output ≈
**$0.02–0.10** (Sonnet-class); a 20-patient day ≈ **$1–2/physician/day**.
Development spend and measured production projections at 100 / 1K / 10K /
100K users are a final-submission deliverable; the architectural inflections:

- **~100 users:** current shape — one agent-service instance beside OpenEMR.
- **~1K:** stateless agent service behind a load balancer; conversation state
  to Redis; OpenEMR FHIR layer becomes the bottleneck → read replicas,
  snapshot response caching.
- **~10K:** LLM rate limits dominate → request queueing, provisioned
  throughput, aggressive caching; pre-computation shifts cost off-peak (a
  morning batch over the day's schedules turns interactive queries into warm
  cache hits).
- **~100K:** multi-region, per-tenant isolation, dedicated capacity;
  cost management becomes a caching/dedup architecture problem, not a
  per-token problem.

## 11. Known Limitations & Roadmap

Stated plainly, because the defensible version of this system is the one
whose gaps are documented:

1. **No panel-scoped authorization** (§4): inherited from OpenEMR. Roadmap:
   panel + relationship-based exceptions + break-glass with mandatory review.
2. **Grounding ≠ entailment** (§5): the hot path cannot catch a
   cited-but-misstated claim beyond parseable numerics. Roadmap: numeric/date
   coverage expansion, then an in-path lightweight entailment check if eval
   data shows the rate justifies the latency.
3. **Prompt-injection residual** (§4): emphasis-biasing by adversarial
   document content is mitigated, not eliminated.
4. **Latency targets are unvalidated** until load testing (10/50 concurrent
   users, p50/p95/p99 + error rate, with baseline resource profiles — §6)
   lands in the early submission.
5. **Single persona** (USER.md §1): nurses, residents, and rounding
   workflows are explicitly deferred; the inherited-authorization design is
   what makes adding them safe later.
6. **v1 trusts OpenEMR's data quality** as found; the data-quality audit
   (AUDIT.md §4) findings — empty-vs-NULL ambiguity, uncoded free-text meds,
   and medications split across three tables that can disagree — become agent
   failure modes, now represented as concrete eval boundary cases (§8). One
   design consequence beyond eval: the snapshot's medication view must
   **reconcile `prescriptions` + `lists` and flag conflicts**, not read a
   single table. Roadmap: a medication-reconciliation tool and requiring coded
   (RxNorm/SNOMED) entries.
7. **Platform security fixes are prerequisites, not agent features**
   (AUDIT.md C2, H1, H2): the reflected-origin CORS policy, missing session
   regeneration, and MFA's absence from the session guard must be remediated
   before the browser panel is exposed cross-origin; the JS-readable session
   cookie and off-by-default `cookie_secure` are hardened with them (§4).
   Tracked as hardening tasks the agent work depends on.
8. **Citation navigation and evidence cards are deferred entirely
   (2026-07-11; T035).** v1 citations are render-only chips — type, human
   identifier, date — which satisfy the PRD's traceability requirement;
   click-to-navigate and the evidence card trace only to this document's
   §5 Layer 3 and were cut for the deadline as scope, not as a requirement
   exception. The probe-verified resolver and routing design (card ids,
   collapse mechanics, open modes) is preserved in the T017/T035 tickets for
   reactivation. In v1, the verification surface is the chip plus Layer 1's
   stripped-content annotations and coverage disclosures.
9. **Panel visual polish is deliberately deferred.** v1 reuses OpenEMR's
   standard Bootstrap/card classes so the panel inherits the native look with
   minimal custom CSS — chosen so any later restyle flows into the panel for
   free. Roadmap: a scoped visual refresh of the patient-dashboard region (and
   the panel with it). An app-wide override of OpenEMR's defaults is explicitly
   *not* pursued — high blast radius, exactly the platform-entanglement risk
   AUDIT.md:257 warns against; a dashboard-scoped restyle is the safe form.
10. **Demo attribution: the relay presents a service-account token** (T021,
    a disclosed downgrade — PRD_EXCEPTIONS.md E2). Audit and disclosure rows
    name the service user, not the acting clinician; the compensating
    controls are the relay seam's session + CSRF + per-request ACL check and
    the per-invocation audit trail. Roadmap: per-user SMART EHR-launch
    tokens behind the existing `ServiceTokenProvider` seam (T027,
    probe-first).
11. **Drug–drug interaction checking is not live** (PRD_EXCEPTIONS.md E1).
    OpenEMR's native path is dead upstream — the NLM Interaction API was
    retired in January 2024, and the native code silent-passes on that
    failure (§5 Layer 2, verified by probe) — and a real check requires a
    licensed interaction knowledge source. v1 enforces the Layer 2
    constraint set (refusal boundary, numeric/date verification, conflict
    flags, drug–allergy cross-check, explicit unchecked markers). Roadmap:
    T034 (probe-first knowledge-source selection).
