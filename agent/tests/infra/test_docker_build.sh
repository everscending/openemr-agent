#!/usr/bin/env bash
# T020 criterion 1: agent/Dockerfile builds a working image.
#
# Real `docker build` against agent/ (the actual Docker CLI, actual exit
# code) — not a check that the Dockerfile merely exists. Runs with
# --no-cache and removes any previously built image under this tag first,
# so a stale layer can never mask a broken Dockerfile.
#
# Usage: agent/tests/infra/test_docker_build.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
AGENT_DIR="${REPO_ROOT}/agent"
IMAGE_TAG="openemr-copilot-agent:t020-test"

if [ ! -f "${AGENT_DIR}/Dockerfile" ]; then
    echo "FAIL: ${AGENT_DIR}/Dockerfile does not exist yet" >&2
    exit 1
fi

# Remove any previously built image under this tag so a stale image can
# never be mistaken for a successful fresh build.
docker image rm -f "${IMAGE_TAG}" >/dev/null 2>&1 || true

echo "Building ${IMAGE_TAG} from ${AGENT_DIR} (--no-cache, real docker build)..."
if docker build --no-cache -t "${IMAGE_TAG}" "${AGENT_DIR}"; then
    echo "PASS: docker build succeeded for ${IMAGE_TAG}"
    exit 0
else
    status=$?
    echo "FAIL: docker build exited ${status}" >&2
    exit "${status}"
fi
