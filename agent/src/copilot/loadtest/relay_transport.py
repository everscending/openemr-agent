"""OpenEMR public-relay transport for the load-test harness (T030 re-scope,
2026-07-12 — see docs/perf/README.md "Correction 2 / re-scope").

Drives the REAL user path — browser -> OpenEMR relay -> agent — instead of
calling the agent's `/chat` directly with a minted bearer. No token minting,
no production shell access, no new public surface on the agent: each
virtual user logs in with real OpenEMR credentials, GETs the patient
Dashboard once to scrape its per-session CSRF token, the patient's FHIR
uuid, and the relay URL — exactly what
``interface/modules/custom_modules/oe-module-clinical-copilot/public/copilot-panel.js``
(``sendToAgent()``, ~L264-300) does in a real browser — then POSTs
form-encoded turns to the relay, reusing the same session (and hence the
same T040 token-hash-bound conversation) on a follow-up.

Produces the SAME ``copilot.loadtest.results.RequestResult`` objects the
existing ``runner`` module produces, so the existing results parser and
abort guard apply completely unchanged — this module supplies a new
*transport*, not new measurement logic.

Each virtual user owns its own ``httpx.AsyncClient`` (and therefore its own
cookie jar / OpenEMR session) — N concurrent logins is the realistic
scenario a concurrency level models here, matching N real clinicians
opening the same patient's Dashboard at once.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from copilot.loadtest.abort_guard import AbortGuard
from copilot.loadtest.results import RequestResult
from copilot.loadtest.runner import compute_ramp_delays
from copilot.loadtest.scenarios import Scenario

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
Reporter = Callable[[RequestResult], None]

# The Dashboard page echoes these as `window.OE_COPILOT_X = "<json-encoded string>";`
# (Bootstrap.php:132-135) -- match the json_encode()'d string literal, including
# its escapes (e.g. `\/` in the relay URL), and decode with the JSON decoder
# rather than hand-rolled unescaping.
_CSRF_RE = re.compile(r'window\.OE_COPILOT_CSRF\s*=\s*"((?:[^"\\]|\\.)*)"')
_PATIENT_UUID_RE = re.compile(r'window\.OE_COPILOT_PATIENT_UUID\s*=\s*"((?:[^"\\]|\\.)*)"')
_RELAY_URL_RE = re.compile(r'window\.OE_COPILOT_RELAY_URL\s*=\s*"((?:[^"\\]|\\.)*)"')


class RelayAuthError(RuntimeError):
    """No authenticated session with a working co-pilot panel context was
    established for this user/patient — covers a rejected login, an
    unauthorized patient, or any other reason the Dashboard did not carry
    the ``OE_COPILOT_*`` values a real browser session would have."""


@dataclass(frozen=True)
class RelayUser:
    """One virtual user's real OpenEMR login credentials."""

    username: str
    password: str


@dataclass(frozen=True)
class RelayContext:
    csrf_token: str
    patient_uuid: str
    relay_url: str


def _json_unescape(raw_string_literal_body: str) -> str:
    """``raw_string_literal_body`` is the inside of a JSON string literal
    (e.g. ``interface\\/modules\\/...``) as matched by the regexes above;
    decode it with the JSON decoder so escapes (``\\/``, ``\\"``, ...) are
    handled correctly rather than by ad hoc string replacement."""
    return json.loads('"' + raw_string_literal_body + '"')


async def fetch_relay_context(
    client: httpx.AsyncClient,
    base_url: str,
    user: RelayUser,
    patient_pid: int,
) -> RelayContext:
    """Log in, then GET the patient Dashboard and scrape the CSRF token,
    patient FHIR uuid, and relay URL the co-pilot panel itself reads.

    Deliberately does not branch on the login POST's status/redirect shape
    (which varies across OpenEMR versions/deployments) — the single source
    of truth for "did this session authenticate" is whether the Dashboard
    actually rendered the panel context, exactly as it would for a real
    browser. Raises :class:`RelayAuthError` if it did not.
    """
    await client.post(
        f"{base_url}/interface/main/main_screen.php",
        params={"auth": "login", "site": "default"},
        data={
            "new_login_session_management": "1",
            "languageChoice": "1",
            "authUser": user.username,
            "clearPass": user.password,
        },
    )

    response = await client.get(
        f"{base_url}/interface/patient_file/summary/demographics.php",
        params={"set_pid": str(patient_pid)},
    )

    csrf_match = _CSRF_RE.search(response.text)
    uuid_match = _PATIENT_UUID_RE.search(response.text)
    relay_match = _RELAY_URL_RE.search(response.text)
    if not (csrf_match and uuid_match and relay_match):
        raise RelayAuthError(
            f"no authenticated co-pilot panel context for user {user.username!r} "
            f"on patient pid={patient_pid} (Dashboard did not carry OE_COPILOT_* markers)"
        )

    return RelayContext(
        csrf_token=_json_unescape(csrf_match.group(1)),
        patient_uuid=_json_unescape(uuid_match.group(1)),
        relay_url=_json_unescape(relay_match.group(1)),
    )


async def run_relay_virtual_user(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    scenario: Scenario,
    user: RelayUser,
    patient_pid: int,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    report: Reporter | None = None,
    abort_event: "asyncio.Event | None" = None,
    guard_first_step: bool = True,
) -> list[RequestResult]:
    """Run one virtual user (its own session) through ``scenario`` via the
    relay. A login/context failure is recorded as a single ``"login"`` step
    result (status_code=None, error=the exception's class name) rather than
    raised — a bad-credential run must still flow through the same
    results/abort-guard pipeline as any other failure, not crash the level.
    """
    try:
        context = await fetch_relay_context(client, base_url, user, patient_pid)
    except RelayAuthError as exc:
        result = RequestResult(
            scenario=scenario.name,
            step="login",
            status_code=None,
            latency_ms=0.0,
            error=type(exc).__name__,
            timestamp=clock(),
        )
        if report is not None:
            report(result)
        return [result]

    relay_url = (
        context.relay_url
        if context.relay_url.startswith("http")
        else f"{base_url}{context.relay_url}"
    )

    results: list[RequestResult] = []
    conversation_id: str | None = None

    for i, step in enumerate(scenario.steps):
        if abort_event is not None and abort_event.is_set() and (i > 0 or guard_first_step):
            break
        if i > 0:
            await sleep(scenario.think_time_s)
            if abort_event is not None and abort_event.is_set():
                break

        form: dict[str, str] = {
            "csrf_token_form": context.csrf_token,
            "message": step.message,
            "patient_id": context.patient_uuid,
        }
        if step.is_followup and conversation_id is not None:
            form["conversation_id"] = conversation_id

        start = clock()
        try:
            response = await client.post(relay_url, data=form)
            status_code: int | None = response.status_code
            if status_code == 200:
                body = response.json()
                conversation_id = body.get("conversation_id", conversation_id)
                error: str | None = None
            else:
                error = f"http_{status_code}"
        except httpx.HTTPError as exc:
            status_code = None
            error = type(exc).__name__

        latency_ms = (clock() - start) * 1000.0
        result = RequestResult(
            scenario=scenario.name,
            step=step.label,
            status_code=status_code,
            latency_ms=latency_ms,
            error=error,
            timestamp=clock(),
        )
        results.append(result)
        if report is not None:
            report(result)

    return results


@dataclass(frozen=True)
class RelayLevelReport:
    results: list[RequestResult]
    aborted: bool
    abort_reason: str | None = None


async def run_relay_level(
    *,
    base_url: str,
    scenario: Scenario,
    users: list[RelayUser],
    patient_pid: int,
    ramp_seconds: float,
    guard: AbortGuard,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    client_factory: "Callable[[], httpx.AsyncClient] | None" = None,
) -> RelayLevelReport:
    """Ramp ``users`` in over ``ramp_seconds``; each gets its own
    ``httpx.AsyncClient`` (own cookie jar / OpenEMR session) via
    ``client_factory`` (defaults to a plain ``httpx.AsyncClient()``).
    Mirrors ``copilot.loadtest.runner.run_level``'s abort semantics exactly
    (same ``AbortGuard``, same "in-flight work completes, no new work
    starts" rule) — see that module's docstring for the rationale.
    """
    abort_event = asyncio.Event()
    all_results: list[RequestResult] = []
    trip_reason: list[str] = []

    def report(result: RequestResult) -> None:
        all_results.append(result)
        decision = guard.observe(result)
        if decision.tripped and not abort_event.is_set():
            trip_reason.append(decision.reason or "abort guard tripped")
            abort_event.set()

    delays = compute_ramp_delays(len(users), ramp_seconds)
    make_client = client_factory or (lambda: httpx.AsyncClient())

    async def delayed_user(delay: float, user: RelayUser) -> list[RequestResult]:
        if delay > 0:
            await sleep(delay)
            if abort_event.is_set():
                return []
        async with make_client() as client:
            return await run_relay_virtual_user(
                client=client,
                base_url=base_url,
                scenario=scenario,
                user=user,
                patient_pid=patient_pid,
                clock=clock,
                sleep=sleep,
                report=report,
                abort_event=abort_event,
                guard_first_step=False,
            )

    await asyncio.gather(
        *(delayed_user(delay, user) for delay, user in zip(delays, users))
    )

    return RelayLevelReport(
        results=all_results,
        aborted=bool(trip_reason),
        abort_reason=trip_reason[0] if trip_reason else None,
    )
