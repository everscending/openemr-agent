# T028 — Observability dashboard + trace-backend exporter wiring

Status: **complete**. The trace backend is deploy-time configuration
(`OTEL_EXPORTER_OTLP_ENDPOINT` + friends), never code — the only module that
imports the OTel SDK/exporter is `agent/src/copilot/telemetry/bootstrap.py`
(T014's import guard stays green with this ticket's new dependency, proven in
a clean subprocess). This directory holds the committed, reproducible
dashboard artifact the PRD requires (PRD.md:315-320) and this document.

Read §1 for the env-var contract, §2 for the panel → signal table
(reproduced from the ticket's pre-spec resolution), §3 to point a dashboard
at a running service, and §4 for the real, live proof this pass captured.

## 1. Exporter configuration (criterion 1)

All three variables are standard OpenTelemetry env vars — nothing
project-specific, nothing vendor-specific:

| Variable | Read by | Effect when unset | Effect when set |
|---|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `copilot.telemetry.bootstrap` (exporter) **and** `copilot.readiness` (the `/ready` `trace_backend` probe — the *same* var, so the two can never disagree) | No exporter is attached; spans are created against a real, inert `TracerProvider` and immediately dropped — never a crash, never a network call. `/ready`'s `trace_backend` key reports non-`ok` with an error naming this variable. | `bootstrap.configure_tracing()` constructs a real `OTLPSpanExporter` pointed at this URL and attaches it via a `SimpleSpanProcessor`. `/ready` performs a live reachability GET against it (>=500 response = probe failure; a 404/405 — a collector that doesn't answer GET — still counts as reachable, matching this codebase's existing reachability-checker semantics for `openemr_fhir`/`llm_provider`). |
| `OTEL_EXPORTER_OTLP_HEADERS` | `OTLPSpanExporter` itself (standard OTel behavior — `bootstrap.py` never overrides `headers=`, so the exporter's own env resolution applies) | No extra headers sent. | Parsed as `key1=value1,key2=value2` and attached to every export request (e.g. an API key for a hosted collector). |
| `OTEL_SERVICE_NAME` | `copilot.telemetry.bootstrap` | Defaults to `copilot`. | The `TracerProvider`'s `service.name` resource attribute is this value instead. |

**No vendor SDK import enters `agent/src/copilot/`.** `langsmith` / `braintrust`
/ `langfuse` / `datadog` / `newrelic` / `sentry_sdk` / `opentelemetry.sdk` /
`opentelemetry.exporter` are all banned outside the one bootstrap module
(`copilot.telemetry.import_guard`, walked transitively — not just per-file —
and proven against a planted violation). **Swapping backends is exporter-config
+ nothing else**: point `OTEL_EXPORTER_OTLP_ENDPOINT` at LangSmith's OTLP
ingest URL (the architecture's lean, §9 — BAA-covered) with an API key in
`OTEL_EXPORTER_OTLP_HEADERS`, or at a self-hosted Tempo/Grafana stack as
below, or at any other OTLP-HTTP-compatible collector. No code changes, no
new file, in either direction.

The trace carries no compliance role (ARCHITECTURE.md:455-460) — this is a
debugging aid with SaaS-typical short retention, never the audit trail.
Audit/disclosure accounting lives in the EMR trail and service logs
(T013/T016), untouched by this ticket.

## 2. Panel → signal table (criterion 3)

Reproduced from the ticket's pre-spec resolution. Every signal below is
already emitted by the committed `agent/` surface (T014, amended by T039) —
this ticket added no new instrumentation, only exporter/readiness wiring and
this dashboard. `agent/tests/test_observability_dashboard.py` drives one
`/chat` request and asserts all eight signals are present *together*, for
that one request (not merely in isolated fixtures scattered across other
tickets' tests).

| Panel | Backing signal | Source |
|---|---|---|
| total request count | count of `chat.request` root spans | backend-derived |
| error rate | `chat.request` span status / `error.type` | backend-derived |
| p50/p95 latency | `chat.request` span duration | backend-derived |
| tool-call counts | count of `tool.call` spans (+ `tool_failure_total`) | backend-derived + `/metrics` |
| token cost | `llm.cost_usd` (exact `Decimal` string) | `llm.call` attribute |
| verification pass/fail | `verify.claims_total` / `verify.claims_passed` | `verify.response` attribute |
| in-flight request depth | `chat.request` root-span start/end concurrency | backend-derived (*derived*, not a literal attribute — see `dashboard.json` panel 7) |
| retry counts | `llm_corrective_retry_total` | `/metrics` — landed by T039 |

## 3. The dashboard artifact

`dashboard.json` is a Grafana dashboard-as-code definition (schema v39,
importable via provisioning or the Grafana UI) using a Tempo datasource in
TraceQL mode. It is intentionally the **portable, self-hostable** option —
any OTLP-compatible trace store works the same way; LangSmith's own UI
provides equivalent panels natively with zero extra provisioning once
`OTEL_EXPORTER_OTLP_ENDPOINT`/`OTEL_EXPORTER_OTLP_HEADERS` point at it (§1's
"swap is exporter-config + nothing else" claim, proven by this file needing
no per-backend logic).

Two honest, disclosed gaps in the committed panels (both noted in-panel,
`dashboard.json` panels 7-8):

- **Panel 7 (in-flight depth)** is genuinely backend-derived — no backend
  queried during this ticket computes true span-overlap concurrency without
  an extra recording rule. The panel documents the Little's-Law approximation
  (`rate(requests) × avg(duration)`) rather than fabricate a number. Per the
  ticket's own instruction, a real concurrency gauge would be a **T014
  amendment** (new instrumentation), out of this ticket's scope — flagged
  here, not built.
- **Panels tied to `/metrics`** (`tool_failure_total`, `llm_corrective_retry_total`,
  the audit counters) need either a Prometheus-compatible scrape adapter in
  front of the JSON endpoint, or Grafana's JSON API datasource plugin
  (`GF_INSTALL_PLUGINS=marcusolsson-json-datasource`) — neither is a stock
  Tempo/Grafana capability, so panel 8 documents the requirement rather than
  wiring a plugin dependency into the committed file.

### Pointing the dashboard at a running service

```bash
# 1. Point the agent at your trace backend (any OTLP-HTTP collector):
export OTEL_EXPORTER_OTLP_ENDPOINT="https://<collector-host>/v1/traces"
export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=<key>"   # if the backend needs auth
export OTEL_SERVICE_NAME="copilot"                     # optional, defaults to "copilot"

# 2. Import dashboard.json into your Grafana (UI: Dashboards -> New -> Import,
#    or drop it into a provisioning `dashboards/json/` folder alongside a
#    `datasources/tempo.yaml` pointing `url:` at your Tempo/Tempo-compatible
#    backend — see §4 for a minimal local example of both files).

# 3. Drive a request (a real /chat call, or `uv run pytest` — which uses an
#    in-memory exporter and never touches the network — does NOT populate a
#    real backend; use a live request, e.g. via the dev stack's /chat route
#    or the loadtest harness in agent/src/copilot/loadtest/).
```

## 4. Live proof captured this pass

`dashboard-screenshot.png` is a **real** screenshot: a throwaway local
Tempo + Grafana stack (`grafana/tempo:2.6.1` + `grafana/grafana:11.2.0`,
never committed to this repo) received two real requests driven through
`AgentLoop` with `copilot.telemetry.bootstrap.configure_tracing()` as the
tracer and `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces` —
the exact same code path production traffic uses, with zero
test-network shortcuts (`InMemorySpanExporter` was not involved). Tempo's
`/api/traces/<id>` endpoint confirmed real ingestion with the correct
resource/span shape, e.g.:

```
chat.request  | {'correlation_id': 't028-live-proof-002'}
tool.call     | {'correlation_id': '...', 'tool.name': 'search_observations', 'outcome': 'ok'}
llm.call      | {'correlation_id': '...', 'model': 'claude-opus-4-8',
                 'llm.input_tokens': '1000', 'llm.output_tokens': '500', 'llm.cost_usd': '0.0525'}
verify.response | {'correlation_id': '...', 'verify.claims_total': '1',
                    'verify.claims_passed': '1', 'verify.claims_stripped': '0'}
resource.service.name: 'copilot'   # OTEL_SERVICE_NAME default, confirmed live
```

The screenshot shows `dashboard.json` provisioned into that Grafana against
that Tempo, with panels 1, 3, 4, 5, and 6 showing the two real ingested
traces (trace IDs, timestamps, and `service: copilot` all match the raw
Tempo query above) and panel 2 (error rate) correctly showing "No data" —
neither driven request errored, so the query correctly returns nothing
rather than a false positive.
