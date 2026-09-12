#!/bin/bash

set -euo pipefail

PROJECT_ROOT="${MEMORYBREAD_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CARGO_BIN="${MEMORYBREAD_CARGO_BIN:-cargo}"
LIMIT_GB="${MEMORYBREAD_BUILD_CACHE_LIMIT_GB:-30}"
MODE="${1:---report}"

if [ "$CARGO_BIN" = "cargo" ] && ! command -v cargo >/dev/null 2>&1 \
    && [ -x "$HOME/.cargo/bin/cargo" ]; then
    CARGO_BIN="$HOME/.cargo/bin/cargo"
fi

target_kilobytes() {
    local total=0
    local target
    local size
    for target in \
        "$PROJECT_ROOT/core-engine/target" \
        "$PROJECT_ROOT/desktop-ui/src-tauri/target"; do
        if [ -d "$target" ]; then
            size=$(du -sk "$target" 2>/dev/null | awk '{print $1}')
            total=$((total + ${size:-0}))
        fi
    done
    printf '%s\n' "$total"
}

format_kilobytes() {
    awk -v value="$1" 'BEGIN { printf "%.1f GiB", value / 1024 / 1024 }'
}

active_build_pid() {
    local pid
    local cwd
    for pid in $(pgrep -x cargo 2>/dev/null || true) $(pgrep -x rustc 2>/dev/null || true); do
        cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
        case "$cwd" in
            "$PROJECT_ROOT"|"$PROJECT_ROOT"/*)
                printf '%s\n' "$pid"
                return 0
                ;;
        esac
    done
    return 1
}

clean_targets() {
    local pid
    pid=$(active_build_pid || true)
    if [ -n "$pid" ]; then
        echo "拒绝清理：MemoryBread 正在进行 Rust 构建（PID $pid）。请等待构建结束后重试。" >&2
        return 2
    fi

    if [ -f "$PROJECT_ROOT/core-engine/Cargo.toml" ]; then
        "$CARGO_BIN" clean --manifest-path "$PROJECT_ROOT/core-engine/Cargo.toml"
    fi
    if [ -f "$PROJECT_ROOT/desktop-ui/src-tauri/Cargo.toml" ]; then
        "$CARGO_BIN" clean --manifest-path "$PROJECT_ROOT/desktop-ui/src-tauri/Cargo.toml"
    fi
}

case "$LIMIT_GB" in
    ''|*[!0-9]*)
        echo "MEMORYBREAD_BUILD_CACHE_LIMIT_GB 必须是非负整数，当前值：$LIMIT_GB" >&2
        exit 2
        ;;
esac

before_kb=$(target_kilobytes)
limit_kb="${MEMORYBREAD_BUILD_CACHE_LIMIT_KB:-$((LIMIT_GB * 1024 * 1024))}"
case "$limit_kb" in
    ''|*[!0-9]*)
        echo "构建缓存阈值必须是非负整数 KB，当前值：$limit_kb" >&2
        exit 2
        ;;
esac

case "$MODE" in
    --report)
        echo "MemoryBread Rust 构建缓存：$(format_kilobytes "$before_kb")"
        ;;
    --auto)
        if [ "$before_kb" -le "$limit_kb" ]; then
            echo "MemoryBread Rust 构建缓存未超过 ${LIMIT_GB} GiB 阈值：$(format_kilobytes "$before_kb")"
            exit 0
        fi
        if active_build_pid >/dev/null; then
            echo "MemoryBread Rust 构建缓存已达 $(format_kilobytes "$before_kb")，但当前有构建任务，跳过本次自动清理。" >&2
            exit 0
        fi
        echo "MemoryBread Rust 构建缓存已达 $(format_kilobytes "$before_kb")，超过 ${LIMIT_GB} GiB 阈值，开始回收。"
        clean_targets
        after_kb=$(target_kilobytes)
        echo "构建缓存回收完成：释放 $(format_kilobytes "$((before_kb - after_kb))")，当前 $(format_kilobytes "$after_kb")。"
        ;;
    --clean)
        echo "开始清理可重建的 MemoryBread Rust 构建缓存：$(format_kilobytes "$before_kb")"
        clean_targets
        after_kb=$(target_kilobytes)
        echo "构建缓存清理完成：释放 $(format_kilobytes "$((before_kb - after_kb))")，当前 $(format_kilobytes "$after_kb")。"
        ;;
    *)
        echo "用法: $0 {--report|--auto|--clean}" >&2
        exit 2
        ;;
esac
