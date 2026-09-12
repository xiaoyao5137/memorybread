#!/bin/bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

# shellcheck source=../start.sh
source "$PROJECT_ROOT/start.sh"

# Execute the real command dispatcher and lifecycle functions with process/HTTP
# stubs. No real service, model, PID file or source timestamp is changed.
STOP_LOG="$TEST_ROOT/stopped"
SIDECAR_CHANGED=true
MODEL_API_CHANGED=true
CREATION_CHANGED=true
CORE_CHANGED=true
CREATION_HEALTHY=true
MODEL_API_READY=true

maybe_delegate_to_workspace_supervisor() { return 0; }
acquire_start_lock() { return 0; }
check_path_leaks() { return 0; }
check_dependencies() { return 0; }
ensure_ollama_running() { return 0; }
cleanup_duplicate_sidecars() { return 0; }
is_running() { return 0; }
is_http_ok() { [ "$MODEL_API_READY" = true ]; }
is_managed_http_ok() { [ "$CREATION_HEALTHY" = true ]; }
wait_for_managed_http() { [ "$CREATION_HEALTHY" = true ]; }
wait_for_http() { return 0; }
check_core_api_readiness() { return 0; }
sidecar_sources_changed() { [ "$SIDECAR_CHANGED" = true ]; }
model_api_sources_changed() { [ "$MODEL_API_CHANGED" = true ]; }
creation_service_sources_changed() { [ "$CREATION_CHANGED" = true ]; }
core_sources_changed() { [ "$CORE_CHANGED" = true ]; }
build_core() { return 0; }
start_ui() { return 0; }
show_status() { return 0; }
stop_managed_process() {
    printf '%s\n' "$2" >> "$STOP_LOG"
    # Stop at the replacement boundary, before any real process can be started.
    exit 73
}

assert_lifecycle_result() {
    local command=$1
    local expected_stop=$2
    local status=0
    : > "$STOP_LOG"
    (main "$command" >/dev/null 2>&1) || status=$?
    if [ -z "$expected_stop" ]; then
        if [ "$status" -ne 0 ] || [ -s "$STOP_LOG" ]; then
            echo "$command interrupted healthy in-flight services after source edits" >&2
            exit 1
        fi
    elif [ "$status" -ne 73 ] || [ "$(cat "$STOP_LOG")" != "$expected_stop" ]; then
        echo "$command did not replace the expected service: $expected_stop" >&2
        exit 1
    fi
}

# All source families changed: supervisor polling must preserve every process.
assert_lifecycle_result start-backends ""

# An explicit start still loads changes for each backend family.
assert_lifecycle_result start "AI Sidecar"
SIDECAR_CHANGED=false
assert_lifecycle_result start "Model API"
MODEL_API_CHANGED=false
assert_lifecycle_result start "Creation Service"
CREATION_CHANGED=false
assert_lifecycle_result start "Core Engine"

# Freshness suppression is scoped to the command and never blocks recovery.
MODEL_API_READY=false
assert_lifecycle_result start-backends "Model API"
MODEL_API_READY=true
CREATION_HEALTHY=false
assert_lifecycle_result start-backends "Creation Service"

echo "supervisor in-flight preservation checks passed"
