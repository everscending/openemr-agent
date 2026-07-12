# T030 — Load/stress tests + baseline resource profiles

Status: **harness complete (both transports) and proven end-to-end locally;
the two PRD-mandated deployed measurement levels (10 and 50 concurrent
users) still could not be obtained** — for a different, narrower reason
than originally reported. Read "Corrections" and "What is still missing,
and why" before anything else.

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

## 2. What is still missing, and why

**The two PRD-mandated levels (10 and 50 concurrent users against the
deployed instance, PRD.md:338-339) were still not obtained** — but the
blocker is now narrower and different in kind from before:

**The deployed OpenEMR's real admin credentials are not `admin`/`pass`.**
A single-user auth probe against
`https://openemr-production-472c.up.railway.app` with the documented
default (`OE_LOAD_USER`/`OE_LOAD_PASS`, defaulting to `admin`/`pass` per
this leg's instructions) failed cleanly: the harness's `relay_transport`
correctly detected it (no `OE_COPILOT_*` markers on the post-login
Dashboard fetch), recorded one `RelayAuthError` result rather than
crashing, and the CLI exited non-zero with `LOGIN FAILED for 1/1 virtual
user(s)`. This matches this project's own [[railway-deployment]] memory
note: *"OpenEMR default `admin`/`pass` does not apply here; the admin
password is the generated `OE_PASS` variable."* Per this leg's explicit
instruction — **do not guess or brute-force passwords** — no further
attempt was made. Running a 50-user level against the same wrong
credentials would only reproduce the identical, uninformative failure 50
times over.

This is now a **credentials-only** gap, not a code, access, or availability
gap:

- The relay transport is fully built, tested, and independently proven
  against the local stack (section 3) — logging in, scraping, and
  completing a full multi-turn conversation through the identical relay
  code path the deployed instance runs.
- No production shell access, no file writes, no token minting, and no new
  public surface were needed or attempted this leg.
- The only missing input is the real `OE_PASS` value (or any other valid
  deployed clinician credential) for `OE_LOAD_USER`/`OE_LOAD_PASS`.

### Reproducing the deployed runs (once real credentials are available)

```bash
export OE_LOAD_USER=admin
export OE_LOAD_PASS=<the real deployed OE_PASS value>
cd agent
uv run loadtest-relay \
  --base-url https://openemr-production-472c.up.railway.app \
  --patient-pid 2 \
  --users 10 --ramp-seconds 60 \
  --out ../docs/perf/raw/deployed-relay-10users.ndjson
# then repeat with --users 50 --ramp-seconds 90 for the second mandated level.
# The abort guard is armed by default (50% sustained-60s hard-error or
# 429-dominance); a tripped run prints "ABORT GUARD TRIPPED: <reason>" to
# stderr — record the wall, do not re-run the level repeatedly.
```

No other change is needed — the harness, transport, and CLI are complete
and were exercised end-to-end against the local stack below.

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
concurrency behavior, and are not a substitute for section 2's still-owed
10/50-user runs. CPU/memory are read-only Railway platform metrics
(`railway metrics --service <name>`, no container shell); latency/error-rate
for a real load run would instead come from the harness's own recorded
results, exactly as in section 3.

## 5. Interpreted against ARCHITECTURE.md targets

ARCHITECTURE.md §6 (~line 374): "first token < 3s; complete snapshot <
10s; follow-ups < 5s; p95 < 15s." Using the relay-transport local runs
(section 3), which now supersede the direct-chat runs as the "real path":

| Target | Local relay result | Met? |
|---|---|---|
| First token < 3s | **Not measurable by this harness.** The relay's `/chat` call is buffered JSON end-to-end (`copilot-panel.js` does a single `fetch().then(response.json())`, never SSE); there is no first-token event to observe from outside. Disclosed gap, unchanged from the previous pass. | N/A |
| Complete snapshot < 10s | Smoke: 2 of 3 under 10s (8.67s, 8.83s), one at 10.12s. 10-user: 8 of 10 under 10s; two exceeded it (10.55s, 12.14s), with the next-highest (9.65s) close behind. | Borderline, not cleanly met under concurrency |
| Follow-ups < 5s | Smoke: 1 of 3 under 5s (3.97s; 5.17s and 6.22s exceeded). 10-user: 6 of 10 under 5s; 4 exceeded (5.18s, 5.22s, 5.96s, 6.97s). | Not consistently met, worse under concurrency |
| p95 < 15s | 9.80s (smoke), 10.63s (10-user) — met in both. | Met (locally) |

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
unisolated from this data — pid=2 has no labs on file, so no lab query was
exercised.

**These remain local-stack numbers.** They demonstrate the same mechanism
on a second transport but still cannot stand in for the deployed
p50/p95/p99 and error-rate numbers PRD.md:338-339 requires — section 2
gives the exact, minimal step (real credentials) needed to close that gap.

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
