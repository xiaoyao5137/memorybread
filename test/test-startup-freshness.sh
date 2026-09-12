#!/bin/bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

export HOME="$TEST_ROOT/home"
mkdir -p "$HOME"

# shellcheck source=../start.sh
source "$PROJECT_ROOT/start.sh"

MARKER="$TEST_ROOT/process.pid"
SOURCE_FILE="$TEST_ROOT/service.py"
SOURCE_DIR="$TEST_ROOT/service"
mkdir -p "$SOURCE_DIR"

touch "$SOURCE_FILE"
touch "$SOURCE_DIR/module.py"
sleep 1
touch "$MARKER"

if any_file_newer_than "$MARKER" "$SOURCE_FILE" "$SOURCE_DIR"; then
    echo "older source was incorrectly treated as newer" >&2
    exit 1
fi

sleep 1
touch "$SOURCE_FILE"
if ! any_file_newer_than "$MARKER" "$SOURCE_FILE"; then
    echo "newer source file was not detected" >&2
    exit 1
fi

sleep 1
touch "$SOURCE_DIR/module.py"
if ! any_file_newer_than "$MARKER" "$SOURCE_DIR"; then
    echo "newer source inside directory was not detected" >&2
    exit 1
fi

rm -f "$MARKER"
if ! any_file_newer_than "$MARKER" "$SOURCE_FILE"; then
    echo "missing process marker should require a restart" >&2
    exit 1
fi

# These shared runtime helpers are imported by all three Python services. Exercise
# their real watch lists with an isolated source tree and deterministic mtimes.
(
    PROJECT_ROOT="$TEST_ROOT/shared-schema-project"
    SIDECAR_PID_FILE="$TEST_ROOT/schema-sidecar.pid"
    MODEL_API_PID_FILE="$TEST_ROOT/schema-model-api.pid"
    CREATION_PID_FILE="$TEST_ROOT/schema-creation.pid"
    mkdir -p "$PROJECT_ROOT/ai-sidecar/embedding/__pycache__" "$PROJECT_ROOT/ai-sidecar/monitor"
    touch -t 202001010001 "$SIDECAR_PID_FILE" "$MODEL_API_PID_FILE" "$CREATION_PID_FILE"
    touch -t 202001010003 "$PROJECT_ROOT/ai-sidecar/embedding/__pycache__/document_chunks.pyc"
    for helper in model_schema.py inference_queue.py inference_transport.py embedding/document_chunks.py embedding/document_quality.py monitor/llm_tracker.py; do
        touch -t 202001010000 "$PROJECT_ROOT/ai-sidecar/$helper"
        for check in sidecar_sources_changed model_api_sources_changed creation_service_sources_changed; do
            if "$check"; then
                echo "$check treated the unchanged $helper as newer" >&2
                exit 1
            fi
        done
        touch -t 202001010002 "$PROJECT_ROOT/ai-sidecar/$helper"
        for check in sidecar_sources_changed model_api_sources_changed creation_service_sources_changed; do
            if ! "$check"; then
                echo "$check missed the changed $helper" >&2
                exit 1
            fi
        done
        touch -t 202001010000 "$PROJECT_ROOT/ai-sidecar/$helper"
    done
)

echo "startup freshness checks passed"
