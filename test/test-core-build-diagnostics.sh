#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_ROOT/start.sh"
TEST_ROOT="$(mktemp -d)"
trap 'test_status=$?; trap - EXIT; rm -rf "$TEST_ROOT"; exit "$test_status"' EXIT
LOG_DIR="$TEST_ROOT/logs"
mkdir -p "$LOG_DIR"

cargo() {
    echo 'compiler stdout diagnostic'
    echo 'compiler stderr diagnostic' >&2
    return 101
}
build_status=0
build_core >/dev/null 2>&1 || build_status=$?
test "$build_status" -eq 101
grep -q 'compiler stdout diagnostic' "$LOG_DIR/core-build.log"
grep -q 'compiler stderr diagnostic' "$LOG_DIR/core-build.log"

cargo() { echo 'successful build'; }
build_core >/dev/null 2>&1
grep -q 'successful build' "$LOG_DIR/core-build.log"
if grep -q 'compiler stderr diagnostic' "$LOG_DIR/core-build.log"; then
    echo 'successful build retained stale compiler errors' >&2
    exit 1
fi
echo 'core build diagnostics checks passed'
