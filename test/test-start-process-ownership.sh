#!/bin/bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

export MEMORYBREAD_MB_ALL_CHILD=1

# shellcheck source=../start.sh
source "$PROJECT_ROOT/start.sh"

REAL_PROCESS_EXECUTABLE="$(process_executable "$$")"
if [ -z "$REAL_PROCESS_EXECUTABLE" ] || [ ! -x "$REAL_PROCESS_EXECUTABLE" ]; then
    echo "process executable discovery did not return the current executable: $REAL_PROCESS_EXECUTABLE" >&2
    exit 1
fi

process_cwd() {
    case "$1" in
        103)
            printf '%s\n' "$PROJECT_ROOT/ai-sidecar"
            ;;
    esac
}

process_executable() {
    case "$1" in
        101)
            printf '%s\n' "/Applications/记忆面包.app/Contents/Helpers/memory-bread-ai.app/Contents/MacOS/memory-bread-ai"
            ;;
        102)
            printf '%s\n' "/Applications/记忆面包.app/Contents/MacOS/memory-bread-desktop"
            ;;
        105)
            printf '%s\n' "/Applications/记忆面包.app/Contents/MacOS/memory-bread-core"
            ;;
        103|104)
            printf '%s\n' "/tmp/unrelated-service"
            ;;
    esac
}

pid_belongs_to_packaged_app 101
pid_is_desktop_app 102
pid_belongs_to_packaged_app 105
pid_belongs_to_memorybread 103

if pid_belongs_to_memorybread 104; then
    echo "foreign process was incorrectly treated as MemoryBread" >&2
    exit 1
fi

pgrep() {
    printf '%s\n' 101 102 104 105
}

PACKAGED_BACKEND_PIDS=$(find_packaged_backend_pids)
if [ "$PACKAGED_BACKEND_PIDS" != $'101\n105' ]; then
    echo "packaged backend discovery returned unexpected PIDs: $PACKAGED_BACKEND_PIDS" >&2
    exit 1
fi

KILL_LOG="$TEST_ROOT/killed"
LISTENER_PID=101
lsof() {
    printf '%s\n' "$LISTENER_PID"
}
kill() {
    printf '%s\n' "$*" >> "$KILL_LOG"
}
sleep() {
    return 0
}
ps() {
    return 1
}

cleanup_port 8001 "Creation Service"
if ! grep -q '101' "$KILL_LOG"; then
    echo "packaged Creation Service was not cleaned up" >&2
    exit 1
fi

: > "$KILL_LOG"
LISTENER_PID=104
if cleanup_port 8001 "Creation Service"; then
    echo "foreign listener cleanup unexpectedly succeeded" >&2
    exit 1
fi
if [ -s "$KILL_LOG" ]; then
    echo "foreign listener was killed" >&2
    exit 1
fi

echo "start process ownership checks passed"
