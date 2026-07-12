"""Tests for the OpenEMR public-relay transport (T030 re-scope, 2026-07-12).

Drives the real user path (browser -> OpenEMR relay -> agent) instead of
the agent's `/chat` directly: login with real credentials, scrape the
per-session CSRF token / patient FHIR uuid / relay URL from the patient
Dashboard (exactly what copilot-panel.js does), then POST form-encoded
turns to the relay, reusing the session and conversation_id on a follow-up.

Two properties asserted at the transport seam (never a return value):

  * the relay POST is genuinely form-encoded (`application/x-www-form-urlencoded`)
    and carries the *scraped* patient uuid, not a raw pid — posting a bare
    pid is exactly the trap the relay's `resolveAccessiblePid` rejects.
  * a UC-2 follow-up reuses the SAME session cookie and the conversation_id
    from the prior response; two different virtual users never share a
    cookie (session isolation — "each virtual user gets its own session").

This module reuses the existing, locked `results`/`abort_guard`/`scenarios`
modules unchanged (only a new transport, no new measurement logic) — see
`copilot.loadtest.runner.compute_ramp_delays`, reused as-is for ramping.
"""

from __future__ import annotations

import asyncio
from typing import Callable

import httpx

from copilot.loadtest.abort_guard import AbortGuard
from copilot.loadtest.relay_transport import (
    RelayAuthError,
    RelayUser,
    fetch_relay_context,
    run_relay_level,
    run_relay_virtual_user,
)
from copilot.loadtest.scenarios import UC1_SNAPSHOT, UC2_FOLLOWUP


class SleepSpy:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _fake_clock() -> Callable[[], float]:
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += 1.0
        return state["t"]

    return clock


_DASHBOARD_HTML_OK = """
<html><body>
<script>
window.OE_COPILOT_PID = "2";
window.OE_COPILOT_CSRF = "csrf-abc-123";
window.OE_COPILOT_PATIENT_UUID = "a23278f4-0274-4db3-a346-88fae3e561ff";
window.OE_COPILOT_RELAY_URL = "\\/interface\\/modules\\/custom_modules\\/oe-module-clinical-copilot\\/public\\/copilot-relay.php";
</script>
</body></html>
"""

_DASHBOARD_HTML_LOGIN_FAILED = """
<html><body>
<script>
w.top.location.href = '/interface/login_screen.php?error=1&site=';
</script>
</body></html>
"""


def _make_ok_handler(captured: list[httpx.Request], cookie_value: str = "sess-1"):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        path = request.url.path
        if path.endswith("/main_screen.php"):
            return httpx.Response(200, headers={"set-cookie": f"OpenEMR={cookie_value}; Path=/"})
        if path.endswith("/demographics.php"):
            return httpx.Response(200, text=_DASHBOARD_HTML_OK)
        if path.endswith("/copilot-relay.php"):
            return httpx.Response(200, json={"reply": "ok", "conversation_id": "conv-relay-1", "fallback": False})
        raise AssertionError(f"unexpected path: {path}")

    return handler


def _make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# fetch_relay_context — scraping
# ---------------------------------------------------------------------------


async def test_fetch_relay_context_scrapes_csrf_uuid_and_relay_url() -> None:
    captured: list[httpx.Request] = []

    async with _make_client(_make_ok_handler(captured)) as client:
        context = await fetch_relay_context(
            client, "http://openemr", RelayUser("admin", "pass"), patient_pid=2
        )

    assert context.csrf_token == "csrf-abc-123"
    assert context.patient_uuid == "a23278f4-0274-4db3-a346-88fae3e561ff"
    # the \/ escapes in the source HTML must be correctly unescaped:
    assert context.relay_url == (
        "/interface/modules/custom_modules/oe-module-clinical-copilot/public/copilot-relay.php"
    )


async def test_fetch_relay_context_raises_relay_auth_error_when_login_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/main_screen.php"):
            return httpx.Response(200)
        if path.endswith("/demographics.php"):
            return httpx.Response(200, text=_DASHBOARD_HTML_LOGIN_FAILED)
        raise AssertionError(f"unexpected path: {path}")

    async with _make_client(handler) as client:
        try:
            await fetch_relay_context(
                client, "http://openemr", RelayUser("admin", "wrong"), patient_pid=2
            )
            raise AssertionError("expected RelayAuthError")
        except RelayAuthError:
            pass


# ---------------------------------------------------------------------------
# run_relay_virtual_user — the two transport-seam properties
# ---------------------------------------------------------------------------


async def test_uc1_posts_form_encoded_with_scraped_patient_uuid_not_raw_pid() -> None:
    captured: list[httpx.Request] = []

    async with _make_client(_make_ok_handler(captured)) as client:
        results = await run_relay_virtual_user(
            client=client,
            base_url="http://openemr",
            scenario=UC1_SNAPSHOT,
            user=RelayUser("admin", "pass"),
            patient_pid=2,
            clock=_fake_clock(),
            sleep=SleepSpy(),
        )

    relay_calls = [r for r in captured if r.url.path.endswith("/copilot-relay.php")]
    assert len(relay_calls) == 1
    relay_request = relay_calls[0]

    # form-encoded, not JSON (the trap: `/chat`'s JSON contract does not apply here).
    assert relay_request.headers["content-type"].startswith(
        "application/x-www-form-urlencoded"
    )

    import urllib.parse

    form = dict(urllib.parse.parse_qsl(relay_request.read().decode()))
    assert form["csrf_token_form"] == "csrf-abc-123"
    # THE TRAP: patient_id must be the scraped UUID, never the raw pid "2".
    assert form["patient_id"] == "a23278f4-0274-4db3-a346-88fae3e561ff"
    assert form["patient_id"] != "2"
    assert "conversation_id" not in form

    assert len(results) == 1
    assert results[0].status_code == 200


async def test_uc2_followup_reuses_conversation_id_and_the_same_session_cookie() -> None:
    captured: list[httpx.Request] = []
    sleep_spy = SleepSpy()

    async with _make_client(_make_ok_handler(captured, cookie_value="sess-XYZ")) as client:
        await run_relay_virtual_user(
            client=client,
            base_url="http://openemr",
            scenario=UC2_FOLLOWUP.with_think_time(12.0),
            user=RelayUser("admin", "pass"),
            patient_pid=2,
            clock=_fake_clock(),
            sleep=sleep_spy,
        )

    relay_calls = [r for r in captured if r.url.path.endswith("/copilot-relay.php")]
    assert len(relay_calls) == 2

    import urllib.parse

    form1 = dict(urllib.parse.parse_qsl(relay_calls[0].read().decode()))
    form2 = dict(urllib.parse.parse_qsl(relay_calls[1].read().decode()))
    assert "conversation_id" not in form1
    assert form2["conversation_id"] == "conv-relay-1"

    # Transport-seam session-identity assertion: both relay calls carry the
    # SAME session cookie the mock login response set (httpx's client-level
    # cookie jar, not a value we constructed ourselves).
    cookie1 = relay_calls[0].headers.get("cookie", "")
    cookie2 = relay_calls[1].headers.get("cookie", "")
    assert "sess-XYZ" in cookie1
    assert cookie1 == cookie2

    assert sleep_spy.calls == [12.0]


async def test_login_failure_is_recorded_as_a_result_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/main_screen.php"):
            return httpx.Response(200)
        if path.endswith("/demographics.php"):
            return httpx.Response(200, text=_DASHBOARD_HTML_LOGIN_FAILED)
        raise AssertionError(f"unexpected path: {path}")

    async with _make_client(handler) as client:
        results = await run_relay_virtual_user(
            client=client,
            base_url="http://openemr",
            scenario=UC1_SNAPSHOT,
            user=RelayUser("admin", "wrong"),
            patient_pid=2,
            clock=_fake_clock(),
            sleep=SleepSpy(),
        )

    assert len(results) == 1
    assert results[0].step == "login"
    assert results[0].status_code is None
    assert results[0].error == "RelayAuthError"


async def test_two_virtual_users_get_different_session_cookies() -> None:
    captured_a: list[httpx.Request] = []
    captured_b: list[httpx.Request] = []

    async with _make_client(_make_ok_handler(captured_a, cookie_value="sess-A")) as client_a:
        await run_relay_virtual_user(
            client=client_a,
            base_url="http://openemr",
            scenario=UC1_SNAPSHOT,
            user=RelayUser("alice", "pass"),
            patient_pid=2,
            clock=_fake_clock(),
            sleep=SleepSpy(),
        )
    async with _make_client(_make_ok_handler(captured_b, cookie_value="sess-B")) as client_b:
        await run_relay_virtual_user(
            client=client_b,
            base_url="http://openemr",
            scenario=UC1_SNAPSHOT,
            user=RelayUser("bob", "pass"),
            patient_pid=2,
            clock=_fake_clock(),
            sleep=SleepSpy(),
        )

    relay_a = [r for r in captured_a if r.url.path.endswith("/copilot-relay.php")][0]
    relay_b = [r for r in captured_b if r.url.path.endswith("/copilot-relay.php")][0]
    assert "sess-A" in relay_a.headers.get("cookie", "")
    assert "sess-B" in relay_b.headers.get("cookie", "")
    assert relay_a.headers.get("cookie") != relay_b.headers.get("cookie")


# ---------------------------------------------------------------------------
# run_relay_level — reuses AbortGuard as-is
# ---------------------------------------------------------------------------


async def test_run_relay_level_healthy_run_completes_every_step_for_every_user() -> None:
    users = [RelayUser(f"user{i}", "pass") for i in range(3)]
    guard = AbortGuard(error_rate_threshold=0.5, dominance_threshold=0.5, sustained_seconds=60.0)

    def factory() -> httpx.AsyncClient:
        return _make_client(_make_ok_handler([]))

    report = await run_relay_level(
        base_url="http://openemr",
        scenario=UC2_FOLLOWUP.with_think_time(0.0),
        users=users,
        patient_pid=2,
        ramp_seconds=0.0,
        guard=guard,
        clock=_fake_clock(),
        sleep=SleepSpy(),
        client_factory=factory,
    )

    assert report.aborted is False
    assert len(report.results) == 6  # 3 users * 2 steps


async def test_run_relay_level_aborts_early_on_sustained_login_failures() -> None:
    users = [RelayUser(f"user{i}", "wrong") for i in range(4)]
    guard = AbortGuard(error_rate_threshold=0.5, dominance_threshold=0.5, sustained_seconds=0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/main_screen.php"):
            return httpx.Response(200)
        if path.endswith("/demographics.php"):
            return httpx.Response(200, text=_DASHBOARD_HTML_LOGIN_FAILED)
        raise AssertionError(f"unexpected path: {path}")

    def factory() -> httpx.AsyncClient:
        return _make_client(handler)

    report = await run_relay_level(
        base_url="http://openemr",
        scenario=UC2_FOLLOWUP.with_think_time(0.0),
        users=users,
        patient_pid=2,
        ramp_seconds=0.0,
        guard=guard,
        clock=_fake_clock(),
        sleep=SleepSpy(),
        client_factory=factory,
    )

    assert report.aborted is True
    assert report.abort_reason is not None
    # every user's login attempt is its own already-committed first "step",
    # so all 4 fire (guard_first_step=False), but no user gets a real chat
    # turn -- every result is the synthetic login-failure result.
    assert len(report.results) == 4
    assert all(r.step == "login" for r in report.results)
