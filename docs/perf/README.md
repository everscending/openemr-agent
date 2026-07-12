# T030 — Load/stress tests + baseline resource profiles

Status: **harness complete and proven; the two PRD-mandated deployed
measurement levels (10 and 50 concurrent users) were NOT obtained this
session.** Read "What is missing, and why" below before anything else — it
is the most important section of this document per this ticket's own hard
rule ("never fabricate a measurement").

## 1. The harness

`agent/src/copilot/loadtest/` — a small, TDD'd Python harness (no Locust/k6
dependency; built on `httpx`, already a project dependency) that drives the
agent's `POST /chat` with realistic scenarios at a target concurrency:

- `scenarios.py` — **UC-1** (`UC1_SNAPSHOT`): a single snapshot turn, no
  prior conversation. **UC-2** (`UC2_FOLLOWUP`): a snapshot turn, then a
  follow-up turn in the same conversation. Documented think-time default is
  **25s** (within the ticket's 20-30s clinician-cadence guidance),
  overridable per run via `--think-time-s` (used for fast local smoke runs).
- `runner.py` — one virtual user runs a scenario's steps in order; a
  follow-up step reuses the **same bearer token** and the
  **`conversation_id` from the prior response** (the T040 dependency this
  ticket calls out — without it, turn 2 404s). `run_level` ramps N virtual
  users in over a configurable window (never slams straight to the target
  concurrency) and wires every result through the abort guard as it arrives.
- `abort_guard.py` — the **mandatory hard-abort guard**. Stops a level from
  dispatching new requests the instant either (a) a windowed hard-error rate
  (non-2xx/network failures, excluding 429) exceeds 50% sustained 60s, or
  (b) 429s alone exceed 50% of the window sustained 60s — tracked and named
  as two *distinct* failure signatures (a 429 wall is "the provider is
  throttling us," not an undifferentiated "error rate"). An
  already-dispatched virtual user's own first (already-committed) request
  is never retroactively cancelled; only new work (a not-yet-ramped-in
  user's first request, or any user's follow-up) is prevented once tripped.
- `results.py` — turns raw per-request records (NDJSON, one object per
  line) into p50/p95/p99 latency and error rate. Raises on empty or
  malformed input rather than silently reporting a clean run.
- `cli.py` (`uv run loadtest` from `agent/`) — the executable entry point
  the recorded runs below actually invoked.

All of the above is red/green TDD'd: `agent/tests/test_loadtest_results.py`,
`test_loadtest_abort_guard.py`, `test_loadtest_scenarios.py`,
`test_loadtest_runner.py` (locked at commit `c1be890`). The abort guard is
tested tripping on a synthetic sustained-hard-error run AND a synthetic
sustained-429 run, and *not* tripping on a healthy run and on a brief burst
that recovers before the sustain window elapses. The results parser is
tested against known-bad fixtures (empty input, malformed NDJSON, wrong-typed
fields) that must raise, not silently read as "0 errors, great numbers."
Token reuse and conversation-id propagation on the UC-2 follow-up are
asserted **at the transport seam** (the actual outgoing request body), not
via a return value.

### Token acquisition

`docs/perf/scripts/mint_loadtest_token.php` mints ONE real, T027-shaped FHIR
bearer (via the actual production `SmartLaunchTokenProvider`, not a
reimplementation) for a given clinician + patient, run inside the target
OpenEMR container. Every virtual user in a run shares this one token —
concurrent virtual users model one clinician with several concurrent
conversations open, which keeps the T040 same-token-on-follow-up property
trivially true (the token variable never changes within a run) without
needing to mint under load. The token is never committed; it is read at
runtime from the `COPILOT_LOADTEST_TOKEN` environment variable.

## 2. What is missing, and why

**The two PRD-mandated levels (10 and 50 concurrent users against the
*deployed* agent, PRD.md:338-339) were not run.** Two independent blockers,
both discovered and both disclosed here rather than worked around:

1. **Minting a deployed-OpenEMR-bound token requires executing PHP inside
   the production OpenEMR container** (there is no public HTTP endpoint for
   server-side per-patient/per-clinician minting — that is precisely what
   T027 built to avoid a browser/consent flow, but it is also why it is not
   reachable from outside without a shell). Two attempts to do this via
   `railway ssh` in this session were both denied by the harness's auto-mode
   permission classifier: first an exploratory env-var read, then a scoped
   attempt to write the *already-code-reviewed* minting script into
   `/tmp` and run it (the same base64-pipe pattern documented in this
   project's own runbook for prior tickets' seeding work) — denied as "a
   write to a live production host outside the deploy pipeline... run this
   step outside auto mode so the user can review." Per the harness's own
   instruction on such a denial, this was not worked around.
2. **Independently, `copilot-agent-production-5c43.up.railway.app` is
   currently down** — `/health`, `/ready`, `/metrics`, and `/chat` all
   return `502 Application failed to respond`. Railway's HTTP-proxy logs
   show `connection refused` from the running deployment instance
   (`d3492c28-...`), while the Railway API still reports that deployment's
   status as a stale `SUCCESS` from `2026-07-11T21:00:59Z`. Deploy logs show
   the last successful `POST /chat` at `2026-07-12T04:48:05Z`; the outage
   began sometime in the ~34 minutes after that. **This is a live-demo
   outage independent of this ticket** and is flagged here as the most
   urgent finding in this document — recovering it (a standard Railway
   redeploy/restart) was not attempted, again because it is a production
   mutation outside this session's sanctioned read-only scope; it needs a
   human to approve it.

Given both, no deployed-agent request of any kind was made in this session
beyond read-only `GET /health` probes (which is how the outage above was
discovered) and structural reachability checks. **No number below claims to
be a deployed 10- or 50-user measurement — that section does not exist in
this document because it does not exist as data.**

What **was** obtained instead, honestly, per the ticket's own permitted
scope ("Build and smoke the harness against the local T020 stack"):

- The harness's own smoke run, against the local T020 stack, with a real
  T027-minted bearer, a real patient, and the real LLM/verification path
  (not a scripted LLM, not a 401 fast path).
- A 10-*local*-virtual-user run against the same local stack, exercising
  real concurrency end-to-end, with `docker stats` sampled throughout for
  both local containers.
- The harness's abort-guard integration path is proven only synthetically
  (`tests/test_loadtest_runner.py`'s `test_run_level_aborts_early_on_a_sustained_429_wall`)
  — it has never fired against a real 429 response, because no run in this
  session hit real sustained errors.

### Recommended next step

Once a human approves either (a) an interactive `railway ssh` session to run
`docs/perf/scripts/mint_loadtest_token.php` against the deployed OpenEMR
(patient uuid + clinician user id analogous to the local run below — see
"Reproducing a run"), or (b) supplies a pre-minted deployed token directly,
**and** the `copilot-agent` service is redeployed/restarted, the exact same
`uv run loadtest` invocations below (with `--base-url
https://copilot-agent-production-5c43.up.railway.app`) produce the mandated
10- and 50-user deployed data. The harness needs no further changes — this
is purely an access/availability gap, not a code gap.

## 3. Recorded runs (local T020 stack only)

| Run | Users | Ramp | Think-time | Requests | Errors | p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|---|
| [local-smoke](raw/local-smoke.ndjson) | 3 | 5s | 2s | 6 | 0 | 6.63s | 10.28s | 10.62s |
| [local-10users](raw/local-10users.ndjson) | 10 | 15s | 5s | 20 | 0 | 8.11s | 9.60s | 9.90s |

Raw per-request records: `docs/perf/raw/*.ndjson`. Computed stats (as
produced by `copilot.loadtest.results.parse_results` /
`stats_to_dict`, i.e. by the harness itself, not hand-computed):
`docs/perf/results/*-stats.json`.

Both runs used `--scenario uc2` (every virtual user does a snapshot turn
then a follow-up turn in the same conversation), a real T027-minted bearer
for OpenEMR user id 5 ("clinician") bound to patient
`a23278f4-0274-4db3-a346-88fae3e561ff` (Susan Underwood), against the local
`development-easy` stack's `copilot` container (port 8380) and `openemr`
container (FHIR API enabled). Zero errors, zero 429s, in both runs — the
local stack has no LLM-provider rate limit to hit and negligible network
latency, so these numbers characterize the harness and the application code
path, not what "10/50 concurrent users against the deployed agent" would
show (real network latency to Railway, and — per ARCHITECTURE.md §10 — the
LLM provider's own throttling, are exactly what the deployed runs exist to
surface and neither is present here).

Per-step latency breakdown (from the raw NDJSON) is the more informative
number than the pooled p50/p95/p99 above, since UC-2's two step types have
very different cost profiles:

- `initial-snapshot` steps: 9.00s-10.71s (local-smoke, n=3), 7.96s-9.97s (local-10users, n=10)
- `followup` steps: 3.99s-4.26s (local-smoke, n=3), 3.67s-8.77s (local-10users, n=10 — 5 of 10 exceeded the 5s follow-up target under concurrency, vs. 0 of 3 at low concurrency)

## 4. Baseline resource profiles

**Local (both services), sampled every ~4s via `docker stats --no-stream`
throughout the local-10users run:**
[`docs/perf/baselines/local-10users-docker-stats.csv`](baselines/local-10users-docker-stats.csv)

| Service | Container | CPU (idle → peak) | Memory (steady) |
|---|---|---|---|
| agent (`copilot`) | `development-easy-copilot-1` | 0.2% → 4.7% | ~70-76 MiB |
| OpenEMR | `development-easy-openemr-1` | 0.4% → **97.3%** | ~860-905 MiB |

**Deployed: not obtained.** The Railway metrics MCP tool
(`mcp__railway__service_metrics` and siblings) was `Unauthorized` in this
session (a separate auth state from the CLI/other Railway MCP server that
*was* authenticated for status/logs/redeploy) — a read-only tooling gap, not
a policy denial, but still no number to report. Deploy/HTTP logs (which
*were* reachable) show the outage but carry no CPU/memory data.

## 5. Interpreted against ARCHITECTURE.md targets

ARCHITECTURE.md §6 (~line 374): "first token < 3s; complete snapshot <
10s; follow-ups < 5s; p95 < 15s."

| Target | Local result | Met? |
|---|---|---|
| First token < 3s | **Not measurable by this harness.** `/chat` is called in its buffered-JSON mode (T011 criterion 6); the harness never opens the SSE stream, so it has no way to observe a first-token timestamp distinct from full-completion. Disclosed gap — the harness would need an SSE client to check this target at all. | N/A |
| Complete snapshot < 10s | Local-smoke: 3 of 3 snapshot steps ranged 9.00s-10.71s — **one of three (10.71s) exceeded 10s.** Local-10users: 7.96s-9.97s, all under, but the max sits within 0.03s of the target. | Borderline / inconsistent, not cleanly met |
| Follow-ups < 5s | Local-smoke: 3.99-4.26s, met (3 of 3). Local-10users (10 concurrent): 3.67-8.77s — **5 of 10 exceeded 5s.** | Met at low concurrency; **not met at 10 concurrent local users** |
| p95 < 15s | 10.28s (smoke), 9.60s (10-user) — met in both local runs. | Met (locally) |

**Dominant cost, where targets were missed:** every miss above tracks the
same suspect the ticket names in advance — ARCHITECTURE.md §6's OpenEMR
**bootstrap tax**. The `docker_stats` capture shows OpenEMR's container CPU
spiking as high as 97.3% during concurrent snapshot processing while the
agent container never exceeds ~5% — the FHIR reads backing each snapshot
each pay a fresh OpenEMR request bootstrap (session/ACL/global assembly),
and that bootstrap is what saturates under concurrency, not the agent's own
LLM-call or verification logic. This is exactly the finding T042 (deferred,
gated on this ticket's data per the ledger) exists to fix. The follow-up
regression under 10 concurrent local users (4.3s → up to 8.9s) is the same
mechanism: a follow-up still re-reads FHIR context, so it still pays the
bootstrap tax, and that tax is what grows under concurrent load — not
verification or the LLM call, which are comparatively CPU-light per the
agent container's flat, low CPU trace. Labs' missing patient index (the
other named suspect) could not be independently isolated from this data —
the demo patient here has no labs on file (visible in the transcript in
section 1's local-smoke output), so no lab query was ever exercised by
these runs.

**These are local-stack numbers only.** They demonstrate the bootstrap-tax
mechanism qualitatively but cannot stand in for the deployed p50/p95/p99 and
error-rate numbers PRD.md:338-339 actually requires — those need the
deployed runs described in section 2.

## 6. Public exposure of the deployed agent (documented per T030's instructions)

`copilot-agent-production-5c43.up.railway.app` was given a public Railway
domain (2026-07-12) specifically so this ticket could load-test it per
PRD.md:338 ("against the deployed agent") — it was previously
private-network-only. This is a deliberate, user-approved demo-scope
decision, not an oversight:

- `POST /chat` still requires a valid FHIR bearer token bound to a real
  patient — unauthenticated/malformed requests get a 422/404, never patient
  data.
- `/health`, `/ready`, `/metrics` are public and PHI-free by design (process
  liveness, dependency reachability booleans/latencies, and OTel counters —
  no patient identifiers, no clinical content).
- This exposure should be reconsidered post-demo (tracked at T024/T036 per
  the project ledger).

## 7. Reproducing a run

Local (safe, no production access needed):

```bash
# 1. Local stack up (copilot container included) — see CLAUDE.md.
# 2. Mint a token inside the openemr container (never commit the output):
docker compose exec openemr sh -c \
  "su -s /bin/sh apache -c 'cd /var/www/localhost/htdocs/openemr && \
   php docs/perf/scripts/mint_loadtest_token.php <clinician_user_id> <patient_uuid>'"

# 3. Run a level:
export COPILOT_LOADTEST_TOKEN=<token from step 2>
cd agent
uv run loadtest \
  --base-url http://localhost:8380 \
  --patient-id <patient_uuid> \
  --users 10 --ramp-seconds 30 \
  --out ../docs/perf/raw/<name>.ndjson
```

Deployed (requires the human-approved steps in section 2 first):

```bash
export COPILOT_LOADTEST_TOKEN=<deployed-bound token>
cd agent
uv run loadtest \
  --base-url https://copilot-agent-production-5c43.up.railway.app \
  --patient-id <deployed patient uuid, e.g. pid=2's uuid> \
  --users 10 --ramp-seconds 60 \
  --out ../docs/perf/raw/deployed-10users.ndjson
# then repeat with --users 50 --ramp-seconds 90 for the second mandated level.
# The abort guard (default 50% sustained-60s hard-error or 429-dominance)
# is armed automatically; a tripped run prints "ABORT GUARD TRIPPED: <reason>"
# to stderr and exits non-zero — record the wall, do not re-run the level
# repeatedly against a live rate-limited deployment.
```
