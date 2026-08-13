#!/bin/bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

export HOME="$TEST_ROOT/home"
mkdir -p "$HOME"

# shellcheck source=../start.sh
source "$PROJECT_ROOT/start.sh"

if [ "$CREATION_STARTUP_RETRIES" -lt 120 ]; then
    echo "Creation Service cold-start wait is too short: $CREATION_STARTUP_RETRIES" >&2
    exit 1
fi

ATTEMPTS=0
curl() {
    ATTEMPTS=$((ATTEMPTS + 1))
    [ "$ATTEMPTS" -ge 31 ]
}
is_running() {
    return 0
}
sleep() {
    return 0
}

wait_for_managed_http "http://127.0.0.1:8001/health" "Creation Service" "$TEST_ROOT/creation.pid" 35 1
if [ "$ATTEMPTS" -ne 31 ]; then
    echo "managed health wait stopped at an unexpected attempt: $ATTEMPTS" >&2
    exit 1
fi

ATTEMPTS=0
SLEEPS=0
curl() {
    ATTEMPTS=$((ATTEMPTS + 1))
    return 1
}
is_running() {
    return 1
}
sleep() {
    SLEEPS=$((SLEEPS + 1))
}

if wait_for_managed_http "http://127.0.0.1:8001/health" "Creation Service" "$TEST_ROOT/creation.pid" 35 1; then
    echo "managed health wait succeeded after the process exited" >&2
    exit 1
fi
if [ "$ATTEMPTS" -ne 1 ] || [ "$SLEEPS" -ne 0 ]; then
    echo "managed health wait did not fail fast after process exit" >&2
    exit 1
fi

echo "managed HTTP wait checks passed"
