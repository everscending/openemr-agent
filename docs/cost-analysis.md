# T032 — AI Cost Analysis

Status: **complete.** Actual dev spend estimate, measured per-interaction unit
economics, and projected production costs at 100 / 1K / 10K / 100K users, per
PRD's submission requirement ("Actual dev spend and projected production
costs at 100 / 1K / 10K / 100K users. Also consider architectural changes
needed at each level. This is not simply cost-per-token × n users." —
PRD.md:356) and ARCHITECTURE.md §10.

Read §1 before anything else — it states exactly what is measured vs.
estimated, and how, so every number below is reproducible.

## 1. Method — what's measured, what's estimated

| Category | Status | How |
|---|---|---|
| Per-interaction token counts and cost (UC-1, UC-2) | **Measured** | Real `AgentLoop` run against the local dev stack's real OpenEMR FHIR API and the real Anthropic API (`claude-sonnet-5`), captured via a private `InMemorySpanExporter` tracer reading the exact `llm.call` span attributes T014 emits (`llm.input_tokens`, `llm.output_tokens`, `llm.cost_usd`) — §2. |
| Price table | **Measured from source** | `agent/src/copilot/telemetry/pricing.py::DEFAULT_PRICE_TABLE` — `claude-sonnet-5`: $3/Mtok input, $15/Mtok output. Exact `Decimal` arithmetic, `compute_cost()`. |
| Model actually running in v1 | **Measured from source** | `agent/src/copilot/app.py:63` — `DEFAULT_CHAT_MODEL = "claude-sonnet-5"`, single model, no tiering (T043). |
| Actual dev spend (Anthropic console total) | **Not available** | No Anthropic billing-console credential is available in this environment. Disclosed as a hard limitation in §4, not fabricated. |
| Dev-to-date spend | **Estimated** | Documented interaction counts from `docs/perf/README.md`'s load-test runs (the only counted, reproducible interaction volume in the repo) × this ticket's measured per-interaction cost — §4. Known to undercount ad hoc manual/demo testing during T001–T043, which left no counted trace. |
| Production projections at 100/1K/10K/100K | **Derived from stated assumptions** | An explicit assumptions table (§5.1) — interactions/user/day, snapshot:follow-up mix, and a per-tier cache-hit-rate assumption — applied to the measured per-interaction costs. Every number in the projection table is reproducible by re-running the arithmetic in §5.2 with different assumption values. |
| Model-tiering savings | **Derived** | Real measured per-call token counts from §2, re-priced at Claude Haiku 4.5's published per-Mtok rate ($1 input / $5 output — not in this repo's price table, sourced from Anthropic's current public pricing) — §6. |

A reviewer can reproduce §2's measurement by re-running the script described
there against the local `development-easy` stack; §4–§6 are pure arithmetic
over the numbers in §2 and the assumptions in §5.1, both stated in full.

## 2. Per-interaction unit economics — measured

ARCHITECTURE.md:578-579 estimates the per-interaction cost as "snapshot ≈
5–15k input tokens + ~1k output ≈ **$0.02–0.10** (Sonnet-class)". This
section replaces that estimate with a real, measured run.

### 2.1 How it was measured

The local dev stack (`docker/development-easy`) was already running with a
real `ANTHROPIC_API_KEY` configured on the `copilot` container. Rather than
route through `/chat` (which does not return token/cost data over HTTP — only
the trace does), the measurement drove the actual `AgentLoop` in-process:

1. A real, server-side-minted OAuth2 FHIR bearer token was created against
   the local OpenEMR container (same mechanism as `.tdd-swarm/T027-probe.php`
   — `AccessTokenRepository`, scoped to `patient/*.read` + `launch`, bound to
   patient pid=16 "Susan Wilson," a local synthetic patient with 3 active
   medications, 2 conditions, and 1 allergy — a non-trivial chart, not an
   empty one; see the `deployed-vs-local-patient-data` lesson on why patient
   selection matters for a representative run).
2. A private `InMemorySpanExporter`-backed `TracerProvider` was constructed
   (never the container's globally configured LangSmith exporter — this
   measurement run must not pollute production traces).
3. `copilot.agent.loop.AgentLoop` was constructed with the real
   `AnthropicLLMClient(model="claude-sonnet-5")`, the real
   `build_default_registry(FhirClient(...))` tool registry (the same six
   tools `/chat` uses), the real bound `patient_id`, and the private tracer.
4. **UC-1** (`agent/src/copilot/loadtest/scenarios.py`'s own `UC1_SNAPSHOT`
   message): `loop.run("What's going on with this patient right now?")` —
   one fresh conversation.
5. **UC-2** (`UC2_FOLLOWUP`'s second step): a second `AgentLoop.run()` call
   with `history` seeded with UC-1's user question and answer, driving the
   follow-up turn: `"What about their most recent labs?"`.
6. Every `llm.call` span's `llm.input_tokens`, `llm.output_tokens`, and
   `llm.cost_usd` attributes (T014 criterion 2, computed by
   `compute_cost()` against `DEFAULT_PRICE_TABLE`) were read directly from
   the exporter — the exact values the production trace would carry for a
   real user turn.

This is a real Anthropic API call over a real FHIR-backed tool call, not a
scripted fake — the same `AnthropicLLMClient` and `FhirClient` `/chat` uses
in production, exercised directly instead of through the HTTP layer only
because HTTP doesn't expose token/cost data to a caller.

### 2.2 Results

**UC-1 — initial snapshot** (one user turn, one tool call):

| LLM call | Purpose | Input tokens | Output tokens | Cost |
|---|---|---:|---:|---:|
| 1 | Decide to call `get_patient_snapshot` | 2,425 | 134 | $0.009285 |
| 2 | Synthesize final answer (post-tool, with citations) | 4,689 | 960 | $0.028467 |
| **Total** | | **7,114** | **1,094** | **$0.037752** |

Tool call: `get_patient_snapshot` → `ok`. Verification: 11 claims total, 7
passed, 4 stripped (T008/T009 caught and removed unverifiable statements —
working as designed, not a defect).

**UC-2 — follow-up turn** (same conversation, one new user question, one new
tool call — cost of the *incremental* follow-up only, not re-billing UC-1):

| LLM call | Purpose | Input tokens | Output tokens | Cost |
|---|---|---:|---:|---:|
| 1 | Decide to call `search_observations` (labs) | 3,303 | 133 | $0.011904 |
| 2 | Synthesize final answer | 3,543 | 107 | $0.012234 |
| **Total** | | **6,846** | **240** | **$0.024138** |

Tool call: `search_observations` → `ok`. Verification: 1 claim total, 0
passed, 1 stripped — this synthetic patient has no lab `Observation`
resources on file, so the model's one claim about labs had nothing to cite
against and T008 correctly stripped it, leaving the generic "couldn't
verify this against the source records" fallback text. This is the
verifier failing closed on a real boundary case (empty labs), not a bug —
consistent with ARCHITECTURE.md §7's "absence-vs-negative" and
"empty/thin record" invariants.

### 2.3 Refining §10's estimate

| | §10 estimate | Measured (this ticket) |
|---|---|---|
| Snapshot input tokens | 5,000–15,000 | 7,114 (sum of both LLM calls) |
| Snapshot output tokens | ~1,000 | 1,094 |
| Snapshot cost | $0.02–0.10 | **$0.0378** |
| Follow-up cost | not separately estimated | **$0.0241** |

The measured snapshot cost ($0.0378) sits in the lower half of §10's
$0.02–0.10 range — consistent with the estimate, and tighter now that it's
grounded in a real two-call trace (tool-selection turn + synthesis turn)
rather than a single blended guess. The gap between §10's estimate and the
measured value is partly explained by T043 (not yet landed when §10 was
written): v1's default model moved from `claude-opus-4-8` ($15/$75 per
Mtok) to `claude-sonnet-5` ($3/$15 per Mtok) — a ~5× per-token price drop —
which is why the measured cost lands well under the top of the estimated
range even with real (not hypothetical) token counts.

## 3. Model tiering — v1 runs a single model (stated explicitly)

**v1 runs a single model everywhere: `claude-sonnet-5`.** Confirmed directly
from source:

- `agent/src/copilot/app.py:63` — `DEFAULT_CHAT_MODEL = "claude-sonnet-5"`.
- `agent/src/copilot/app.py:219` (`_default_chat_llm`) and `app.py:405-406`
  (`resolved_chat_model`) — the same resolved model string is threaded to
  both the constructed `AnthropicLLMClient` and the value `AgentLoop` uses
  for cost telemetry (T043's fix for exactly this desync risk).
- Every LLM call in §2's measured trace — both the tool-selection turn and
  the synthesis turn, in both UC-1 and UC-2 — ran on `claude-sonnet-5`.
  There is no fast/cheap routing tier in the running code today.

This matches ARCHITECTURE.md §6 ("model tiering (roadmap, not in v1): v1
runs a single model; a cheaper/faster routing tier is the first cost/latency
optimization lever, priced in the cost analysis rather than shipped") and §9
("V1 runs a single model; tiering ... is the first cost lever, priced in the
cost analysis"). §6 of this document prices that lever using §2's real
per-call token counts.

## 4. Actual dev spend — estimated, with the limitation disclosed

**No Anthropic billing-console credential is available in this environment.**
This ticket cannot report a real, reconciled dollar figure from provider
billing, and does not fabricate one. What follows is an estimate derived
from the only counted interaction volume that exists in the repository,
multiplied by this ticket's measured per-interaction cost — shown as
arithmetic, not asserted as the real number.

### 4.1 Counted interaction volume

`docs/perf/README.md` records every load-test run's request count (its own
harness parses `status_code`/latency per request, not cost — §2.2 of that
document — but request counts are exact):

| Run | Requests | Source |
|---|---:|---|
| local-relay-smoke | 6 | docs/perf/README.md §3 |
| local-relay-10users | 20 | docs/perf/README.md §3 |
| local-smoke (direct) | 6 | docs/perf/README.md §3 |
| local-10users (direct) | 20 | docs/perf/README.md §3 |
| deployed 10-user level | 20 | docs/perf/README.md §2 |
| deployed 50-user level (of which 38 errored before/during the OpenEMR audit-log-exhaustion cascade, per §2 — conservatively excluded) | 47 | docs/perf/README.md §2 |
| This ticket's own measurement (§2) | 2 | this document |
| **Total counted interactions that reached the LLM** | **121** | |

All runs used the `uc2` scenario by default (one snapshot turn + one
follow-up turn per virtual user) except this ticket's own 2, which are one
of each already counted above at measured cost. For the 119 load-test
turns, assume the same even snapshot:follow-up split the `uc2` scenario
produces by construction (59–60 of each).

### 4.2 Arithmetic

```
snapshot turns  ≈ 60  × $0.037752  = $2.265
follow-up turns ≈ 59  × $0.024138  = $1.424
this ticket's own 2 measured calls  = $0.062   (already itemized in §2)
                                    ------------
documented-interaction total       ≈ $3.75
```

**This is a lower bound, not the real dev spend.** It counts only
harness-driven load-test traffic and this ticket's own measurement — it
excludes every manual/interactive test during T001–T043's development (panel
clicks in Selenium sessions, ad hoc `/chat` probes, the local-copilot-demo
runbook's verification passes), none of which left a counted trace anywhere
in the repo. The true dev-to-date spend is higher than $3.75; how much
higher cannot be determined without the Anthropic console. State this
plainly to a reviewer rather than inflating the $3.75 figure with an
unsourced multiplier.

## 5. Production projections at 100 / 1K / 10K / 100K users

### 5.1 Assumptions (a grader can change these and recompute §5.2)

| Assumption | Value | Rationale |
|---|---|---|
| Interactions (snapshot-equivalent visits)/physician/day | 20 | ARCHITECTURE.md:579's own baseline ("a 20-patient day") |
| Follow-up rate | 30% of visits get one follow-up question | §6's "depth on demand" design intent — follow-ups are opt-in, not the default path; not separately measured, stated as an assumption |
| Snapshot cost/interaction | $0.037752 | Measured, §2.2 |
| Follow-up cost/interaction | $0.024138 | Measured, §2.2 |
| Snapshot cache-hit rate — 100 users | 0% | Current shape (§10): one instance, no caching layer built |
| Snapshot cache-hit rate — 1K users | 15% | §10's "snapshot response caching" — passive, short-TTL cache on repeat same-day views of the same patient |
| Snapshot cache-hit rate — 10K users | 70% | §10's morning batch pre-compute (§7 below) converting most interactive snapshot calls into cache hits |
| Snapshot cache-hit rate — 100K users | 85% | §10's caching/dedup architecture, matured past the 10K tier |
| Follow-up cache-hit rate — 100 / 1K / 10K users | 0% | Follow-ups are open-ended, per-conversation questions — not batchable the way the default snapshot question is |
| Follow-up cache-hit rate — 100K users | 20% | Semantic dedup of common follow-up questions across a large user base (e.g. "most recent labs" asked verbatim across many similar patients) |
| Days/month | 30.44 | Calendar average, for the monthly total column only |

Every number in §5.2 is `(snapshots/day × (1 − snapshot-cache-hit) ×
snapshot-cost) + (followups/day × (1 − followup-cache-hit) × followup-cost)`,
multiplied by user count and (for the monthly column) 30.44.

### 5.2 Projection table

| Tier | Snapshot cache-hit | Follow-up cache-hit | Cost/user/day | Users | Cost/day (fleet) | Cost/month (fleet) |
|---|---:|---:|---:|---:|---:|---:|
| 100 | 0% | 0% | $0.8999 | 100 | $89.99 | $2,739 |
| 1,000 | 15% | 0% | $0.7866 | 1,000 | $786.61 | $23,944 |
| 10,000 | 70% | 0% | $0.3713 | 10,000 | $3,713.40 | $113,036 |
| 100,000 | 85% | 20% | $0.2291 | 100,000 | $22,911.84 | $697,436 |

**Total fleet cost rises with scale, as it must — but per-user cost falls at
every tier past 100 users.** That non-monotonic-per-user-cost curve is the
argument §5.3 develops; it is the direct rebuttal to a naive
cost-per-token × n model, which would hold per-user cost flat across all
four tiers.

### 5.3 The non-linearity, argued: the ~10K tier

Per-user-day cost drops from **$0.7866** (1K tier) to **$0.3713** (10K
tier) — a 52.8% reduction — even though absolute usage per physician is
assumed unchanged (still 20 snapshot-equivalent visits + 6 follow-ups/day).
The mechanism, per ARCHITECTURE.md:587-590:

> LLM rate limits dominate → request queueing, provisioned throughput,
> aggressive caching; pre-computation shifts cost off-peak (a morning batch
> over the day's schedules turns interactive queries into warm cache hits).

Concretely: at the 10K tier, a nightly/early-morning batch job walks the
day's scheduled patients and pre-generates each patient's snapshot summary
once, off-peak, while the LLM provider is otherwise idle for this workload.
When a physician opens that patient's chart during the day and asks the
default question, the answer is served from the pre-computed cache — **a
$0 marginal LLM call**, not a fresh $0.037752 interactive one. The 70%
snapshot cache-hit assumption in §5.1 reflects a batch that successfully
pre-computes the large majority of that day's actually-viewed patients;
the remaining 30% (patients not on the day's schedule, or whose snapshot
went stale between batch and view — e.g. a same-day chart update) still
pay the interactive cost.

Arithmetically, this is why the curve bends down instead of flattening:

```
100 users, no caching:     20 × 1.00 × $0.037752 = $0.7550/user/day (snapshot component)
10K users, 70% cache hit:  20 × 0.30 × $0.037752 = $0.2265/user/day (snapshot component)
```

The follow-up component ($0.1448/user/day, unaffected — follow-ups are not
batchable) stays flat across both tiers, which is exactly why the *blended*
per-user cost doesn't collapse to zero: the mechanism only converts the
*default-question* traffic to cache hits, not the open-ended one. That
asymmetry is itself the point — it is why this is a caching/dedup
architecture problem (§10's own framing) rather than a uniform discount, and
why a grader flexing the follow-up-rate assumption up in §5.1 will see the
10K-tier savings shrink (more of the traffic shifts to the
never-cached follow-up path).

## 6. Model tiering as the first optimization lever — priced

Per §3, v1 ships a single model. The first optimization lever per
ARCHITECTURE.md §6/§9 is splitting each interaction's two LLM calls onto two
tiers: a **fast/cheap model** for the tool-selection turn (the model's job
here is narrow — decide which of ~6 tools to call, not synthesize prose),
and the **current `claude-sonnet-5`** for the citation-heavy final-answer
synthesis turn, where quality is worth the higher per-token cost.

**Priced against real, measured token counts from §2** (not a hypothetical
split), using Claude Haiku 4.5's published rate ($1.00/Mtok input, $5.00/Mtok
output — not yet in this repo's `DEFAULT_PRICE_TABLE`, which only prices
`claude-opus-4-8` and `claude-sonnet-5`; adding a `claude-haiku-4-5` row
would be the corresponding code change if this lever ships):

| Interaction | Call | Tokens (in/out) | Sonnet 5 cost (current) | Haiku 4.5 cost (tiered) | Savings |
|---|---|---|---:|---:|---:|
| UC-1 | Tool-selection | 2,425 / 134 | $0.009285 | $0.003095 | 66.7% on this call |
| UC-1 | Synthesis (unchanged) | 4,689 / 960 | $0.028467 | — | — |
| **UC-1 total** | | | **$0.037752** | **$0.031562** | **16.4%** |
| UC-2 | Tool-selection | 3,303 / 133 | $0.011904 | $0.003968 | 66.7% on this call |
| UC-2 | Synthesis (unchanged) | 3,543 / 107 | $0.012234 | — | — |
| **UC-2 total** | | | **$0.024138** | **$0.016202** | **32.9%** |

Re-running §5.1's assumptions with the tiered per-interaction costs, at the
100-user tier (no caching, isolating the tiering lever from the caching
lever in §5.3):

```
20 × $0.031562 + 6 × $0.016202 = $0.63124 + $0.09721 = $0.72845/user/day
```

versus $0.89987/user/day untiered — an **19.1% reduction**, available
immediately at the smallest tier, before any caching investment. This is
consistent with the "first lever" framing: it is orthogonal to and
stacks with the caching mechanism in §5.3 (a cache-hit snapshot has $0
marginal cost regardless of which model would have served it; tiering only
helps the cache-miss remainder).

## 7. Architectural changes per tier (mapped to §10's four inflections)

| Tier | ARCHITECTURE.md §10 inflection | What changes | Cost mechanism (this document) |
|---|---|---|---|
| **~100** | "current shape — one agent-service instance beside OpenEMR" | No change; today's deployment | §5.2 baseline: $0.90/user/day, no caching |
| **~1K** | "stateless agent service behind a load balancer; conversation state to Redis; OpenEMR FHIR layer becomes the bottleneck → read replicas, snapshot response caching" | Horizontal scale-out of the agent service; conversation state moves out of process memory; passive snapshot caching begins | §5.1's 15% snapshot cache-hit rate; §5.2 shows cost/user/day already falling to $0.79 |
| **~10K** | "LLM rate limits dominate → request queueing, provisioned throughput, aggressive caching; pre-computation shifts cost off-peak" | Request queue in front of the LLM provider; provisioned-throughput contract with Anthropic; a morning batch pre-compute job | §5.3's mechanism — 70% snapshot cache-hit rate, the tier where per-user cost visibly drops (§5.2: $0.37/user/day) |
| **~100K** | "multi-region, per-tenant isolation, dedicated capacity; cost management becomes a caching/dedup architecture problem, not a per-token problem" | Multi-region deployment; per-tenant isolation boundaries; dedicated model capacity; semantic dedup extends caching to the follow-up path | §5.1's 85%/20% cache-hit rates; §5.2: $0.23/user/day — the lowest per-user cost of any tier, even though fleet spend is largest |

Model tiering (§6) is not tied to a specific inflection above — it is
priced as available at every tier, cheapest to adopt at the smallest scale
since it requires no new infrastructure (just a second `LLMClient`
configuration and a routing decision in the loop), unlike the caching
mechanisms which do require the infrastructure investment described in
their respective tiers.

## 8. Sources

- `ARCHITECTURE.md:576-594` (§10 — the skeleton estimate and four scale
  inflections this document deepens).
- `ARCHITECTURE.md:371-392` (§6 — model tiering as "the first cost/latency
  optimization lever, priced in the cost analysis rather than shipped").
- `ARCHITECTURE.md:563-574` (§9 — model/framework choice table, same
  tiering framing).
- `PRD.md:356` (submission requirement: "Actual dev spend and projected
  production costs at 100 / 1K / 10K / 100K users... not simply
  cost-per-token × n users").
- `agent/src/copilot/telemetry/pricing.py` — `DEFAULT_PRICE_TABLE`,
  `compute_cost()`.
- `agent/src/copilot/agent/loop.py` — `AgentLoop`, `SYSTEM_PROMPT`,
  `_call_llm`'s `llm.cost_usd`/`llm.input_tokens`/`llm.output_tokens` span
  attributes (T014 criterion 2).
- `agent/src/copilot/app.py:63,219,405-406` — `DEFAULT_CHAT_MODEL =
  "claude-sonnet-5"`, `_default_chat_llm`, `resolved_chat_model` (T043).
- `.tdd-swarm/tickets/T043-agent-model-sonnet-env-configurable.md` — the
  ticket that switched the default model from `claude-opus-4-8` to
  `claude-sonnet-5` and added its price-table row.
- `docs/perf/README.md` — T030's load-test request counts, used for the
  dev-spend lower bound in §4.
- `.tdd-swarm/T027-probe.php` — the token-minting pattern this ticket's
  measurement reused to drive a real FHIR-backed `AgentLoop` run locally.
