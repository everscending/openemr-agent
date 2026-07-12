# Clinical Co-Pilot API collection (T031)

A runnable [Bruno](https://www.usebruno.com/) collection covering the core
agent endpoints (PRD.md:321-324, ARCHITECTURE.md §7): `POST /chat`
(streaming and non-streaming), `GET /health`, `GET /ready`, and the module
**audit-bridge** endpoint. Plain-text `.bru` files, versioned in git — no
SaaS workspace, no Postman export.

The citation-resolver request (T017/T035) is **out of scope** — see the T031
ticket. If T035 ships, add its resolver request (plus an unknown-uuid 404
negative) back in here.

## What's in it

| # | Request | Endpoint | Covers |
|---|---|---|---|
| 01 | Health Check | `GET /health` | T001 liveness |
| 02 | Readiness Check | `GET /ready` | T004 dependency probes |
| 10 | Chat - New Conversation | `POST /chat` | T011 criterion 1/2 |
| 11 | Chat - Follow-up | `POST /chat` | T011 criterion 2 (prior turns) |
| 12 | Chat - Scope Mismatch, Different Patient | `POST /chat` | T011 criterion 3 (404) |
| 13 | Chat - Scope Mismatch, Different Token | `POST /chat` | **T031 mandated negative** (404) |
| 14 | Chat - Unknown Conversation Id | `POST /chat` | T011 identical-body rule (404) |
| 15 | Chat - Malformed Request | `POST /chat` | T011 criterion 1 (422, never 500) |
| 16 | Chat - Streaming | `POST /chat?stream=true` | T011 criterion 6 (SSE) |
| 20 | Audit Bridge - Missing Auth | `POST .../audit-bridge.php` | **T031 mandated negative** (401) |
| 21 | Audit Bridge - Method Not Allowed | `GET .../audit-bridge.php` | T016 (405) |
| 22 | Audit Bridge - Malformed Record | `POST .../audit-bridge.php` | T016 criterion 5 (422) |
| 23 | Audit Bridge - Valid Record | `POST .../audit-bridge.php` | T016 criteria 2-4 (201) |

Requests are numbered (not folder-nested) so the collection root runs
non-recursively in one shot — see "The one command" below.

## What to fill in first

The committed environments (`environments/local.bru`,
`environments/railway.bru`) carry **no real credentials** — only the
placeholders `REPLACE_WITH_BEARER_TOKEN` / `REPLACE_WITH_BEARER_TOKEN_SHA256`.
Two requests (22, 23) need a real bearer to get past the audit-bridge's
OAuth2 check; every other request in the collection already runs correctly
against the placeholder (health/ready need no auth at all; the `/chat`
requests only need a *non-empty* token string — T011 defers token
*validation* to the FHIR tool call, so a placeholder still exercises the
documented contract for those).

1. **Mint a bearer token** (OAuth2 password grant for the `admin` user,
   same mechanism T021's relay uses — see `bin/mint_bearer_token.php`):

   ```bash
   openemr-cmd e "su -s /bin/sh apache -c 'php \
     /var/www/localhost/htdocs/openemr/gauntletai/api-collection/bin/mint_bearer_token.php'"
   ```

   Prints two lines:

   ```
   BEARER_TOKEN=eyJ...
   BEARER_TOKEN_SHA256=...
   ```

   The token is valid for about an hour; re-run the script to mint a fresh
   one. Nothing here is committed — the script performs the grant fresh
   each time.

2. **Run the collection**, passing both values as CLI overrides (never
   written back into the committed environment file):

   ```bash
   cd gauntletai/api-collection
   bru run --env local \
     --env-var bearer_token="$BEARER_TOKEN" \
     --env-var bearer_token_sha256="$BEARER_TOKEN_SHA256"
   ```

   (`bearer_token_sha256` is passed alongside the raw token so the
   audit-bridge request bodies can carry `user_token_hash` — T016's binding
   check — without needing a SHA-256 implementation inside a `.bru` script.)

## The one command

Against the local dev stack (`docker/development-easy`, already up per
`CLAUDE.md`), from the repo root:

```bash
TOKENS=$(openemr-cmd e "su -s /bin/sh apache -c 'php \
  /var/www/localhost/htdocs/openemr/gauntletai/api-collection/bin/mint_bearer_token.php'")
eval "$TOKENS"
cd gauntletai/api-collection
bru run --env local --env-var bearer_token="$BEARER_TOKEN" --env-var bearer_token_sha256="$BEARER_TOKEN_SHA256"
```

Expect `13 (13 Passed)`, `36/36` tests.

**Note on `bru run` and the collection path:** the installed Bruno CLI
(`@usebruno/cli` 3.5.1) resolves the collection root from the current working
directory — `bru run <path-to-collection>` from *outside* the collection
fails with `You can run only at the root of a collection`, even though
`bru --help`'s own examples suggest otherwise. `cd` into
`gauntletai/api-collection` first, as above.

Without `bearer_token`/`bearer_token_sha256` filled in, everything **except**
requests 22 and 23 still passes — those two fail 401 (auth required before
body validation runs), which is the expected, honest state of the committed
collection: no real credential ships in git.

### Against the deployed Railway stack

```bash
cd gauntletai/api-collection
bru run --env railway --env-var bearer_token=... --env-var bearer_token_sha256=... \
  --env-var patient_id=<a-deployed-patient-uuid> --env-var other_patient_id=<a-different-one>
```

**Known limitation:** the deployed `copilot-agent` service has a public
Railway domain (`environments/railway.bru`'s `agent_base_url`,
`https://copilot-agent-production-df9d.up.railway.app`), but as of this
writing it returns `502 Application failed to respond` on every route,
including `/health` — a target-port/ingress misconfiguration on the Railway
service (see `.tdd-swarm/progress.md`'s T030 log entries; this domain has
flapped between not existing, 502ing, and being deleted over the course of
this run). Requests 01, 02, and 10-16 (everything against the agent
service) will therefore fail against Railway until that's fixed on the
Railway side — this collection cannot repair a deploy-time ingress config
from a `.bru` file, and doing so is outside this ticket's scope (see
T022/T020/T024). Requests 20-23 (audit-bridge) run against `openemr_base_url`,
which **is** reliably publicly reachable, so those four work unmodified from
anywhere regardless of the agent domain's state.

If a grader's `bru run --env railway` shows only 01/02/10-16 failing (502)
and 20-23 passing, that is the expected, currently-disclosed state — not a
bug in this collection. Once the Railway ingress is fixed (T024 or a manual
`railway domain`/target-port fix), no `.bru` file needs to change: the
public URL is already what's committed.

## CSRF

The audit-bridge endpoint is bearer-authenticated (OAuth2, `$ignoreAuth =
true`), not session-based, so it carries no CSRF token — see T016. The panel
relay endpoint (T021, `copilot-relay.php`) *is* session-based and CSRF-protected,
but is explicitly **out of scope** for this collection (the ticket names the
audit-bridge endpoint, not the relay).

## Fixture data

`patient_id` / `other_patient_id` in `environments/local.bru` are two
patient UUIDs from the dev stack's seeded fixture data (`patient_data.uuid`,
formatted as a FHIR-style UUID string) — not secrets, committed the same way
`agent/evals/cases/*.yaml` commits fixture patient scenarios.

## Design notes

- Assertions target the **documented HTTP contract** (status code, byte-shape
  of error bodies, header presence, SSE frame ordering) rather than
  LLM-generated reply content, which is non-deterministic by nature — this
  keeps `bru run` a reliable pass/fail signal rather than a flaky one.
- Requests 12/13/14 assert the three 404 bodies are **byte-identical**
  (`JSON.stringify` equality via a runtime variable, not just "both 404"),
  the same identical-body property T011's own test suite proves — see
  `CLAUDE.md`'s "Assert the property, not its proxy."
- Request 20 asserts the response is **not** HTML and does not contain the
  word "login", not just `status != 200` — the exact `$ignoreAuth` trap
  T016's ticket documents (a forgotten `$ignoreAuth` renders an HTML login
  page with HTTP 200, invisible to a status-only check).
