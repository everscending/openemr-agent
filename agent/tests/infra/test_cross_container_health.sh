#!/usr/bin/env bash
# T020 criterion 3: from INSIDE the openemr container, GET
# http://copilot:<port>/health and http://copilot:<port>/ready both respond
# over the real docker network.
#
# This intentionally never curls localhost from within the copilot
# container itself — the point is cross-container reachability, which is
# what the T021 panel relay will depend on. It execs into the *openemr*
# container (already part of the running dev stack) and curls the
# `copilot` service by its compose service name.
#
# Precondition: the docker/development-easy stack is up (mysql/openemr/etc
# already running) via `openemr-cmd worktree up` or `docker compose up`.
# This script brings the `copilot` service itself up (build + start) if it
# isn't already, then performs the real cross-container HTTP calls.
#
# Usage: agent/tests/infra/test_cross_container_health.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/docker/development-easy/docker-compose.yml"
PROJECT_NAME="development-easy"
COPILOT_PORT="${COPILOT_PORT:-8080}"

compose() {
    docker compose -f "${COMPOSE_FILE}" -p "${PROJECT_NAME}" "$@"
}

if ! docker ps --format '{{.Names}}' | grep -q "^${PROJECT_NAME}-openemr-1$"; then
    echo "FAIL: ${PROJECT_NAME}-openemr-1 is not running. Bring up the dev stack first." >&2
    exit 1
fi

echo "Bringing up the copilot service (build + start)..."
compose up -d --build copilot

echo "Waiting for copilot to answer on the docker network (via the openemr container)..."
attempts=0
max_attempts=30
until compose exec -T openemr curl -sS -m 3 -o /dev/null "http://copilot:${COPILOT_PORT}/health"; do
    attempts=$((attempts + 1))
    if [ "${attempts}" -ge "${max_attempts}" ]; then
        echo "FAIL: copilot:${COPILOT_PORT}/health never became reachable from openemr" >&2
        compose logs copilot >&2 || true
        exit 1
    fi
    sleep 1
done

echo "--- GET http://copilot:${COPILOT_PORT}/health (from inside openemr container) ---"
health_code="$(compose exec -T openemr curl -sS -m 5 -w '%{http_code}' -o /dev/null "http://copilot:${COPILOT_PORT}/health")"
health_body="$(compose exec -T openemr curl -sS -m 5 "http://copilot:${COPILOT_PORT}/health")"

echo "status=${health_code} body=${health_body}"
if [ "${health_code}" != "200" ]; then
    echo "FAIL: /health returned HTTP ${health_code}, expected 200" >&2
    exit 1
fi
if [ "${health_body}" != '{"status":"ok"}' ]; then
    echo "FAIL: /health body was '${health_body}', expected the process-alive body" >&2
    exit 1
fi

echo "--- GET http://copilot:${COPILOT_PORT}/ready (from inside openemr container) ---"
ready_code="$(compose exec -T openemr curl -sS -m 8 -w '%{http_code}' -o /dev/null "http://copilot:${COPILOT_PORT}/ready")"
ready_body="$(compose exec -T openemr curl -sS -m 8 "http://copilot:${COPILOT_PORT}/ready")"
echo "status=${ready_code} body=${ready_body}"

# /ready is allowed to report 200 (all deps ok) or 503 (degraded, e.g. no
# trace backend configured for the demo) — both are a real readiness JSON
# response over the network. Anything else (connection failure, 000, 5xx
# other than a well-formed body) is a real failure.
if [ "${ready_code}" != "200" ] && [ "${ready_code}" != "503" ]; then
    echo "FAIL: /ready returned unexpected HTTP status ${ready_code}" >&2
    exit 1
fi

for key in openemr_fhir llm_provider trace_backend; do
    if ! printf '%s' "${ready_body}" | grep -q "\"${key}\""; then
        echo "FAIL: /ready body missing dependency key '${key}'" >&2
        exit 1
    fi
done

echo "PASS: /health and /ready both reachable and well-formed across the docker network"
