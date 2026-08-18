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
MANAGED_PID=4321
printf '%s\n' "$MANAGED_PID" > "$TEST_ROOT/creation.pid"
curl() {
    ATTEMPTS=$((ATTEMPTS + 1))
    [ "$ATTEMPTS" -ge 31 ]
}
is_running() {
    return 0
}
lsof() {
    printf '%s\n' "$MANAGED_PID"
}
sleep() {
    return 0
}

wait_for_managed_http "http://127.0.0.1:8001/health" "Creation Service" "$TEST_ROOT/creation.pid" 8001 35 1
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

if wait_for_managed_http "http://127.0.0.1:8001/health" "Creation Service" "$TEST_ROOT/creation.pid" 8001 35 1; then
    echo "managed health wait succeeded after the process exited" >&2
    exit 1
fi
if [ "$ATTEMPTS" -ne 0 ] || [ "$SLEEPS" -ne 0 ]; then
    echo "managed health wait did not fail before HTTP after process exit" >&2
    exit 1
fi

ATTEMPTS=0
SLEEPS=0
curl() {
    ATTEMPTS=$((ATTEMPTS + 1))
    return 0
}
is_running() {
    return 0
}
lsof() {
    printf '%s\n' 9876
}
sleep() {
    SLEEPS=$((SLEEPS + 1))
}

if wait_for_managed_http "http://127.0.0.1:8001/health" "Creation Service" "$TEST_ROOT/creation.pid" 8001 3 1; then
    echo "managed health wait accepted a response from an unrelated listener" >&2
    exit 1
fi
if [ "$ATTEMPTS" -ne 0 ] || [ "$SLEEPS" -ne 3 ]; then
    echo "listener ownership mismatch was not handled as expected" >&2
    exit 1
fi

printf '%s\n' "$MANAGED_PID" > "$TEST_ROOT/creation.pid"
lsof() {
    printf '%s\n' "$MANAGED_PID"
}
if ! is_managed_http_ok "http://127.0.0.1:8001/health" "$TEST_ROOT/creation.pid" 8001; then
    echo "matching managed listener was rejected" >&2
    exit 1
fi

if [ "$ATTEMPTS" -ne 1 ]; then
    echo "matching managed listener did not reach the health endpoint" >&2
    exit 1
fi


echo "managed HTTP wait checks passed"
