# T030 — Load/stress tests + baseline resource profiles

Status: **complete.** Harness built and proven on both transports; both
PRD-mandated deployed levels (10 and 50 concurrent users, PRD.md:338-339)
were run against the live deployment on 2026-07-12 and are recorded in §2.

**The 50-user level took the deployed instance down.** OpenEMR's audit `log`
table filled the 500 MB MySQL volume (`1114 The table 'log' is full`), and the
instance 500'd on every request until manually recovered. That is the headline
result, not a footnote — and it means **the 50-user level must not be re-run
against the deployed instance until the volume is grown / the log rotated**
(§2.1). Notably, the LLM provider never rate-limited us: **zero 429s** at either
level.

Read §2 before anything else.

## Corrections (2026-07-12, orchestrator-directed re-scope)

An earlier version of this document reported the deployed agent
(`copilot-agent-production-5c43.up.railway.app`) as down, and reported the
ticket as `passed` with that as a disclosed gap. Both were wrong, corrected
here for the record:

1. **The agent was never down.** That public domain had been created
   moments earlier with no target port configured, so Railway's edge had
   nowhere to route — the 502s were an edge-routing artifact, not an
   application failure. `railway metrics --service copilot-agent` (see
   section 4) now confirms it directly: the container's CPU sat at ~0% the
   whole time (a crashed/restarting process would show restart churn or
   error-state CPU, not flat idle), and its own deploy logs show a
   continuous, unbroken run with `Uvicorn running` and steady `200 OK`
   traffic before my probes started. The lesson, stated plainly so it isn't
   repeated: **suspect the probe before the code.**
2. **The agent's route was never public in the first place, by design, and
   stays that way.** The domain that produced the 502s has since been
   deleted; the agent is private-network-only again and that URL no longer
   resolves. This ticket does not attempt to reach it.
3. **Given (1) and (2), the ticket is re-scoped**: load is driven through
   the **real user path** — browser → OpenEMR relay → agent — against
   `https://openemr-production-472c.up.railway.app`, OpenEMR's existing
   public URL, instead of a direct, bearer-token call to the agent's
   `/chat`. This needs no token minting and no production shell access
   (both of which blocked the previous attempt), and it profiles *both*
   services under one load generator, which criterion 3 wants anyway.

## 1. The harness — two transports, one measurement pipeline

`agent/src/copilot/loadtest/` — a small, TDD'd Python harness (`httpx`, no
new dependency) with two interchangeable transports feeding the SAME
results parser and abort guard:

- **`runner.py`** (original) — calls the agent's `/chat` directly with a
  T027-minted bearer. Retained and still fully tested; not used for the
  deployed runs below (the agent has no public route), but its local runs
  from the previous pass remain in `docs/perf/raw/local-smoke.ndjson` and
  `local-10users.ndjson` as a second, independent proof the application
  code path itself behaves the same way under both transports.
- **`relay_transport.py`** (this pass) — the real user path. Each virtual
  user gets its own `httpx.AsyncClient` (its own cookie jar): it POSTs real
  OpenEMR login credentials to `interface/main/main_screen.php`, then GETs
  the patient Dashboard (`interface/patient_file/summary/demographics.php`)
  and scrapes the three values the co-pilot panel's own JS reads —
  `window.OE_COPILOT_CSRF`, `window.OE_COPILOT_PATIENT_UUID`,
  `window.OE_COPILOT_RELAY_URL` (`Bootstrap.php:132-135`) — then POSTs
  **form-encoded** turns to the relay
  (`interface/modules/custom_modules/oe-module-clinical-copilot/public/copilot-relay.php`),
  reusing the same session and the response's `conversation_id` on a UC-2
  follow-up. Mirrors `copilot-panel.js`'s `sendToAgent()` (~L264-300)
  exactly — verified line-by-line against that file, then verified live
  with `curl` (login → scrape → POST, real reply, real citations) before a
  single test was written.
- **`results.py`, `abort_guard.py`, `scenarios.py`** — **unchanged, reused
  as-is** by both transports; not touched in this pass. The abort guard
  (windowed hard-error-rate OR 429-dominance, sustained 60s) is armed for
  every run below.
- **`cli.py`** (`uv run loadtest`) / **`relay_cli.py`** (`uv run
  loadtest-relay`) — the executable entry points; the recorded runs below
  invoked the latter.

Locked test commits: `c1be890` (original transport: results, abort guard,
scenarios, runner) and `b01ca28` (relay transport: context scraping,
form-encoding + the UUID-not-pid trap, session/conversation-id reuse at the
transport seam, per-user session isolation, login-failure handling, abort
integration). 503 tests pass; neither locked file has been touched since
its lock commit.

### THE TRAP, confirmed caught

`copilot-relay.php`'s `resolveAccessiblePid()` independently authorizes the
POSTed `patient_id` and rejects a raw pid — the harness must post the
*scraped FHIR uuid*, never `"2"`.
`test_uc1_posts_form_encoded_with_scraped_patient_uuid_not_raw_pid` asserts
this at the transport seam (the actual form body), and the live local run
below is a second, independent confirmation: it returns real per-patient
data, which a rejected/misrouted `patient_id` could not.

## 2. Headline finding — what actually breaks at 50 users

Both PRD-mandated levels (PRD.md:338-339) were run against the **deployed**
system on 2026-07-12, through the real relay path, against the demo patient
that has real clinical data (`pid=21`, 4 active medications — see §2.3).

| Level | Requests | Errors | Error rate | 429s | p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|
| **10 users** | 20 | 0 | **0.0%** | 0 | 13.81s | 17.55s | 19.95s |
| **50 users** | 85 | 38 | **44.7%** | **0** | 7.70s | 18.41s | 19.19s |

**The binding constraint is not the LLM, and not the agent. It is OpenEMR's
audit log exhausting the database volume.**

At 50 concurrent users the deployed system failed as follows:

```
MySQL:   1114  The table 'log' is full
OpenEMR: HTTP 500 on every request (incl. /interface/login/login.php)
```

The 38 errors decompose into **23 × HTTP 500** and **15 × login failure**
(`RelayAuthError` — virtual users that could not even authenticate, with
credentials that worked fine at the 10-user level). Both are the same
cascade: OpenEMR writes an audit row per request; under 50 concurrent users
the `log` table filled the **500 MB** `mysql-volume`, InnoDB could no longer
extend the tablespace, and every subsequent query — including the login
query — failed. **OpenEMR did not recover on its own** (12 consecutive polls
over 3 minutes, all 500) and required manual intervention.

Three consequences worth stating plainly:

1. **Zero LLM rate-limiting.** `rate_limited_count: 0` at both levels — the
   Anthropic provider never throttled us. The pre-run worry (a 429 wall,
   ARCHITECTURE.md §10's ~10K inflection arriving early) **did not
   materialise at this scale**. The abort guard, armed for exactly that, never
   tripped: the 44.7% error rate sat just under its 50% threshold.
2. **The wall is storage/audit-write capacity, not CPU and not the model.**
   This sharpens §6's "bootstrap tax" claim: the *latency* floor is the PHP
   bootstrap (see §4 — OpenEMR pins ~97% CPU while the agent idles at <5%),
   but the *availability* wall under concurrency is the audit log filling the
   DB volume. A co-pilot that audits every access (by design — ARCHITECTURE.md
   §7) multiplies OpenEMR's own audit write volume, and the deployed volume was
   not provisioned for it.
3. **The 50-user p50 (7.70s) is LOWER than the 10-user p50 (13.81s) — this is
   an artifact, not an improvement.** Failed requests (500s, auth failures)
   return fast and drag the median down. Read p95/p99 (18.41s/19.19s) and the
   error rate, never the p50, for this level.

### 2.1 Remediation (not done by this ticket — it measures)

Grow `mysql-volume` beyond 500 MB and/or add rotation/retention on OpenEMR's
`log` table before any production-like concurrency. See T042 / ARCHITECTURE.md
§6 roadmap. **Do not re-run the 50-user level against the deployed instance
until that is done — it will take the demo down again.**

### 2.2 Honest gap — the harness scores transport, not answer quality

`RequestResult` records `status_code`/latency only. It has **no notion of the
relay's `fallback` flag, nor of a T008 verification-stripped reply** — so a run
in which every answer came back degraded would still score "0 errors". The
latency and error-rate numbers above are valid; **"0 errors" is not a statement
about answer quality.** Answer quality was verified *separately*, by probing the
deployed relay by hand (real grounded replies with RxNorm codes and
`MedicationRequest/...` citations). Closing this properly means recording
`fallback` and a stripped-answer signature per request — a known follow-up.

### 2.3 Patient selection is load-bearing

An earlier pass ran both deployed levels against `pid=2`, which on the deployed
DB is an **empty chart** (demographics only; every FHIR bundle ~200 bytes / zero
entries). Those numbers (p50 7.86s, p95 9.90s, 0 errors at 50 users) measured an
empty tool fan-out and were **discarded as unrepresentative** — they violate
criterion 5 ("valid tokens/patient context so the run exercises tools and
verification, not a fast-path"). The deployed and local databases hold
**different patients**; the deployed patient with real data is `pid=21`. Always
confirm the target patient has data before recording a run.

### Reproducing the deployed runs

```bash
export OE_LOAD_USER=admin
export OE_LOAD_PASS=<the real deployed OE_PASS value>   # not admin/pass
cd agent
uv run loadtest-relay \
  --base-url https://openemr-production-472c.up.railway.app \
  --patient-pid 21 \
  --users 10 --ramp-seconds 60 \
  --out ../docs/perf/raw/deployed-relay-10users.ndjson
# 50-user level: --users 50 --ramp-seconds 90
# WARNING: at 50 users this exhausts the 500 MB mysql-volume and takes the
# deployed instance down (see §2). Grow the volume first.
# The abort guard is armed by default (50% sustained-60s hard-error or
# 429-dominance); a tripped run prints "ABORT GUARD TRIPPED: <reason>" to
# stderr — record the wall, do not re-run the level repeatedly.
```

## 3. Recorded runs

### Relay transport (this pass) — the real user path, local stack

| Run | Users | Ramp | Think-time | Requests | Errors | p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|---|
| [local-relay-smoke](raw/local-relay-smoke.ndjson) | 3 | 5s | 2s | 6 | 0 | 7.45s | 9.80s | 10.05s |
| [local-relay-10users](raw/local-relay-10users.ndjson) | 10 | 15s | 5s | 20 | 0 | 7.62s | 10.63s | 11.84s |

Both used `--scenario uc2`, real `admin`/`pass` login (the local dev
stack's actual credentials — no default-password assumption needed here,
unlike the deployed attempt), against demo patient pid=2 (Susan Underwood,
uuid `a23278f4-0274-4db3-a346-88fae3e561ff` — the harness scraped this uuid
itself from the Dashboard each run; it was never hardcoded into a request).
Zero errors, zero login failures, in both runs.

Per-step latency (from the raw NDJSON — more informative than the pooled
percentiles since UC-2's two step types differ sharply in cost):

- `initial-snapshot`: 8.67s-10.12s (smoke, n=3), 8.27s-12.14s (10-user, n=10)
- `followup`: 3.97s-6.22s (smoke, n=3), 3.88s-6.97s (10-user, n=10)

### Direct `/chat` transport (previous pass, retained for comparison)

| Run | Users | Ramp | Think-time | Requests | Errors | p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|---|
| [local-smoke](raw/local-smoke.ndjson) | 3 | 5s | 2s | 6 | 0 | 6.63s | 10.28s | 10.62s |
| [local-10users](raw/local-10users.ndjson) | 10 | 15s | 5s | 20 | 0 | 8.11s | 9.60s | 9.90s |

Computed stats for all four runs (produced by
`copilot.loadtest.results.parse_results`/`stats_to_dict`, i.e. by the
harness itself): `docs/perf/results/*-stats.json`.

**Both transports, both local, agree qualitatively**: snapshot turns
cluster 8-12s, follow-ups 4-7s, both widening under 10 concurrent users
relative to the 3-user smoke — consistent (same application code, same
bootstrap cost, two different front doors).

## 4. Baseline resource profiles

**Local (both services), sampled every ~4s via `docker stats --no-stream`
throughout each 10-user run:**

| Run | Service | Container | CPU (idle → peak) | Memory (range) |
|---|---|---|---|---|
| [local-10users (direct)](baselines/local-10users-docker-stats.csv) | agent | `copilot-1` | 0.2% → 4.7% | 70-76 MiB |
| local-10users (direct) | OpenEMR | `openemr-1` | 0.4% → **97.3%** | 860-905 MiB |
| [local-relay-10users](baselines/local-relay-10users-docker-stats.csv) | agent | `copilot-1` | 0.2% → 1.6% | 74-75 MiB |
| local-relay-10users | OpenEMR | `openemr-1` | 0.5% → **97.2%** | 885 MiB-**1.05 GiB** |

The relay run's OpenEMR memory climbs meaningfully higher (up to 1.05 GiB
vs. 905 MiB for the direct-chat run) — expected, since the relay path adds
10 real concurrent **login sessions** (each with its own PHP session state)
on top of the same per-request FHIR bootstrap the direct-chat run already
paid. The agent container stays flat and low in both cases — confirms the
bottleneck is OpenEMR-side, not the agent, regardless of which front door
drives the load.

**Deployed — idle baseline only, via `railway metrics` (read-only, no
shell), NOT under this ticket's load** (the deployed relay run did not
execute, per section 2):

| Service | CPU (current/avg/max, last 1h) | Memory | HTTP (last 1h) | Latency (p50/p95/p99) |
|---|---|---|---|---|
| [openemr](baselines/deployed-openemr-railway-metrics-idle.txt) | <0.01 / <0.01 / 0.02 vCPU | 188-192 MB | 145 req, 0% error | 105/105/105ms |
| [copilot-agent](baselines/deployed-copilot-agent-railway-metrics-idle.txt) | 0 / <0.01 / <0.01 vCPU | 79-80 MB | 12 req, 100% 5xx (the edge-routing 502s from Correction 1's now-deleted public domain — not application errors) | 27/27/27ms |

This table is what motivated Correction 1 above: the agent's own resource
trace shows a flat, idle-shaped container the whole time, not a crash — the
5xx column here is the edge, not the app. These deployed numbers are
**idle baselines, not load-test baselines** — they say nothing about
concurrency behavior. The deployed *load* results live in §2; the deployed
CPU/memory trace *under* load was not captured, because the 50-user run ended
in a DB-exhaustion outage that took priority over metrics collection (an
honest gap — no number is invented for it). CPU/memory are read-only Railway platform metrics
(`railway metrics --service <name>`, no container shell); latency/error-rate
for a real load run would instead come from the harness's own recorded
results, exactly as in section 3.

## 5. Interpreted against ARCHITECTURE.md targets

ARCHITECTURE.md §6 (~line 374): "first token < 3s; complete snapshot <
10s; follow-ups < 5s; p95 < 15s." Judged against the **deployed** relay runs
(§2) — the numbers PRD.md:338-339 actually asks for — with the local runs
(§3) retained as the mechanism trace:

| Target | Deployed result (10 users, real-data patient) | Met? |
|---|---|---|
| First token < 3s | **Not measurable by this harness.** The relay's `/chat` call is buffered JSON end-to-end (`copilot-panel.js` does a single `fetch().then(response.json())`, never SSE); there is no first-token event to observe from outside. Structural gap, not an omission. | N/A |
| Complete snapshot < 10s | **Missed.** p50 alone is 13.81s — every snapshot turn exceeded the target. | **No** |
| Follow-ups < 5s | **Missed, badly.** No deployed follow-up came in under 5s. | **No** |
| p95 < 15s | **Missed.** 17.55s at 10 users; 18.41s at 50. | **No** |

The deployed system misses **every** latency target it can be measured
against — by roughly 1.4–3.5×. The earlier, rosier numbers (p50 7.86s, p95
9.90s) came from the empty-chart patient and are void (§2.3). Against a
patient with real data, the full tool fan-out plus verification costs ~2× more.

**Dominant cost:** unchanged from the previous pass's finding, now
reinforced by a second, independent transport and a heavier resource trace
— ARCHITECTURE.md §6's OpenEMR **bootstrap tax**. `docker_stats` shows
OpenEMR CPU spiking to 97%+ under 10 concurrent users on **both**
transports (direct-chat and relay), while the agent container never
exceeds ~5% CPU on either. The relay path's added login-session overhead
pushed OpenEMR's memory footprint higher still (up to 1.05 GiB), and its
follow-up latencies were measurably worse than the direct-chat run's
(4/10 over the 5s target vs. a comparable spread previously) — consistent
with each relay-driven follow-up paying both a fresh FHIR-context bootstrap
*and* a real PHP session lookup, where the direct-chat path only pays the
former. Labs' missing patient index (the other named suspect) remains
unisolated from this data — the local demo patient has no labs on file, so no
lab query was exercised.

**Two distinct bottlenecks, now separated by the deployed run.** The local
`docker_stats` trace isolates the *latency* mechanism (bootstrap tax: OpenEMR
pins ~97% CPU while the agent idles below 5%). The deployed 50-user run
exposed a second, harder ceiling the local runs never reached — the *availability*
wall, where OpenEMR's audit-log writes exhaust the database volume and the
instance stops serving entirely (§2). Latency degrades gracefully; availability
does not. Any scale plan (ARCHITECTURE.md §10) has to answer both, and the
audit-volume one is the one that takes the system down.

## 6. Public exposure

The agent has **no public route** and none was created this pass (the
domain from Correction 1 was created accidentally in a prior session and
has since been deleted). OpenEMR's existing public URL
(`https://openemr-production-472c.up.railway.app`) is the only
public-facing surface this ticket's deployed runs touch, and it is already
the deployed demo's normal, intended public entry point — this load-testing
approach adds no new exposure at all, which is the whole reason for the
re-scope.

## 7. Reproducing a local run

```bash
# Local stack up (copilot container included) — see CLAUDE.md.
cd agent
uv run loadtest-relay \
  --base-url https://localhost:9300 \
  --patient-pid 2 \
  --users 10 --ramp-seconds 30 \
  --insecure \
  --out ../docs/perf/raw/<name>.ndjson
```

(`--insecure` skips TLS verification for the local stack's self-signed
cert only — never pass it for a deployed run.) `OE_LOAD_USER`/`OE_LOAD_PASS`
default to `admin`/`pass`, which are the local dev stack's real credentials.
