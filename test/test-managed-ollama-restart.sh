#!/bin/bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
MANAGED_PID=""

cleanup() {
    local status=$?
    trap - EXIT
    if [ -n "$MANAGED_PID" ] && ps -p "$MANAGED_PID" > /dev/null 2>&1; then
        kill "$MANAGED_PID" 2>/dev/null || true
        wait "$MANAGED_PID" 2>/dev/null || true
    fi
    rm -rf "$TEST_ROOT"
    exit "$status"
}
trap cleanup EXIT

export MANAGED_OLLAMA_TEST_CAPTURE="$TEST_ROOT/managed-env.txt"

# shellcheck source=../start.sh
source "$PROJECT_ROOT/start.sh"

TEST_MEMORY_HOME="$TEST_ROOT/home"
LOG_DIR="$TEST_MEMORY_HOME/.memory-bread/logs"
OLLAMA_PID_FILE="$LOG_DIR/ollama.pid"
OLLAMA_LOG="$LOG_DIR/ollama.log"
INITIALIZATION_ROOT="$TEST_MEMORY_HOME/.memory-bread/initialization"
MANAGED_OLLAMA_MARKER="$INITIALIZATION_ROOT/processes/ollama.json"
MANAGED_OLLAMA_RUNTIME_ROOT="$INITIALIZATION_ROOT/runtime/ollama"
MANAGED_OLLAMA_MODELS_ROOT="$INITIALIZATION_ROOT/models"
mkdir -p "$LOG_DIR"

FAKE_RUNTIME="$MANAGED_OLLAMA_RUNTIME_ROOT/v-test/runtime/ollama"
mkdir -p "$(dirname "$FAKE_RUNTIME")" "$MANAGED_OLLAMA_MODELS_ROOT" "$(dirname "$MANAGED_OLLAMA_MARKER")"
printf '%s\n' \
    '#!/bin/bash' \
    'printf "%s\n%s\n%s\n%s\n" "$OLLAMA_HOST" "$OLLAMA_MODELS" "$OLLAMA_NO_CLOUD" "$OLLAMA_NOHISTORY" > "$MANAGED_OLLAMA_TEST_CAPTURE"' \
    'trap "exit 0" TERM INT' \
    'while true; do sleep 1; done' > "$FAKE_RUNTIME"
chmod +x "$FAKE_RUNTIME"

printf '{"pid":1,"executable":"%s","models_root":"%s","port":11434}\n' \
    "$FAKE_RUNTIME" \
    "$MANAGED_OLLAMA_MODELS_ROOT" > "$MANAGED_OLLAMA_MARKER"

resolved=$(resolve_managed_ollama_runtime)
REAL_FAKE_RUNTIME="$(cd "$(dirname "$FAKE_RUNTIME")" && pwd -P)/ollama"
REAL_MODELS_ROOT="$(cd "$MANAGED_OLLAMA_MODELS_ROOT" && pwd -P)"
[ "$(printf '%s\n' "$resolved" | sed -n '1p')" = "$REAL_FAKE_RUNTIME" ]
[ "$(printf '%s\n' "$resolved" | sed -n '2p')" = "$REAL_MODELS_ROOT" ]

is_ollama_ready() {
    return 1
}

cleanup_port() {
    return 0
}

# This fixture owns only its fake runtime; never sweep the developer's live model.
sweep_managed_runtime_processes() {
    return 0
}

wait_for_http() {
    local _url=$1
    local _label=$2
    local retries=${3:-20}
    local _delay=${4:-1}
    local attempt

    for ((attempt=1; attempt<=retries; attempt++)); do
        if [ -f "$MANAGED_OLLAMA_TEST_CAPTURE" ]; then
            return 0
        fi
        sleep 0.05
    done
    return 1
}

ensure_ollama_running
MANAGED_PID=$(tr -d '[:space:]' < "$OLLAMA_PID_FILE")

[ "$(sed -n '1p' "$MANAGED_OLLAMA_TEST_CAPTURE")" = "127.0.0.1:11434" ]
[ "$(sed -n '2p' "$MANAGED_OLLAMA_TEST_CAPTURE")" = "$REAL_MODELS_ROOT" ]
[ "$(sed -n '3p' "$MANAGED_OLLAMA_TEST_CAPTURE")" = "1" ]
[ "$(sed -n '4p' "$MANAGED_OLLAMA_TEST_CAPTURE")" = "1" ]

"$PROJECT_ROOT/ai-sidecar/.venv/bin/python" - "$MANAGED_OLLAMA_MARKER" "$MANAGED_PID" <<'PY'
import json
import sys

import psutil

marker = json.load(open(sys.argv[1], encoding="utf-8"))
pid = int(sys.argv[2])
assert marker["pid"] == pid
assert abs(float(marker["create_time"]) - psutil.Process(pid).create_time()) <= 0.01
assert marker["port"] == 11434
PY

# 健康进程的 PID 变化时必须刷新初始化器使用的身份登记，不能仅凭端口健康
# 直接返回，否则严格所有权校验会把自己的运行时误判成第三方进程。
RECORDED_REUSE="$TEST_ROOT/recorded-reuse"
is_ollama_ready() { return 0; }
managed_ollama_listener_pid() {
    [ "$1" = "$REAL_FAKE_RUNTIME" ]
    printf '%s\n' "$MANAGED_PID"
}
record_managed_ollama_process() {
    printf '%s\n%s\n%s\n' "$1" "$2" "$3" > "$RECORDED_REUSE"
}
cleanup_port() {
    echo "healthy managed runtime was unexpectedly cleaned" >&2
    return 1
}

ensure_ollama_running

[ "$(sed -n '1p' "$RECORDED_REUSE")" = "$MANAGED_PID" ]
[ "$(sed -n '2p' "$RECORDED_REUSE")" = "$REAL_FAKE_RUNTIME" ]
[ "$(sed -n '3p' "$RECORDED_REUSE")" = "$REAL_MODELS_ROOT" ]
[ "$(tr -d '[:space:]' < "$OLLAMA_PID_FILE")" = "$MANAGED_PID" ]

echo "managed Ollama restart checks passed"
