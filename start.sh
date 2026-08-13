#!/bin/bash
#
# 记忆面包 启动脚本
#
# 按顺序启动三个组件：
# 1. AI Sidecar (Python，含 7072 内部检索服务)
# 2. Model API / RAG API (Python，7071，提供 /api/models + /query)
# 3. Core Engine (Rust)
# 4. Desktop UI (Tauri)
#

set -e  # 遇到错误立即退出

# 添加 Rust 和 Homebrew Node 到 PATH（nohup 启动时不继承用户 PATH）
if [ -d "$HOME/.cargo/bin" ]; then
    export PATH="$HOME/.cargo/bin:$PATH"
fi
if [ -d "/opt/homebrew/bin" ]; then
    export PATH="/opt/homebrew/bin:$PATH"
fi

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 项目根目录
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

check_path_leaks() {
    local leaked_paths=()
    local candidates=(
        "$PROJECT_ROOT/ai-sidecar/~/.workbuddy"
        "$PROJECT_ROOT/ai-sidecar/~/.qdrant"
        "$PROJECT_ROOT/~/.workbuddy"
        "$PROJECT_ROOT/~/.qdrant"
    )

    for path in "${candidates[@]}"; do
        if [ -e "$path" ]; then
            leaked_paths+=("$path")
        fi
    done

    if [ ${#leaked_paths[@]} -gt 0 ]; then
        log_error "检测到仓库内存在未展开的 home 路径残留："
        for path in "${leaked_paths[@]}"; do
            log_error "  - $path"
        done
        log_error "请先清理这些目录，再重新启动，避免模型和向量数据继续写入仓库目录。"
        exit 1
    fi
}

# 日志目录
LOG_DIR="$HOME/.memory-bread/logs"
mkdir -p "$LOG_DIR"
STATE_DIR="$HOME/.memory-bread/state"
mkdir -p "$STATE_DIR"
SUPERVISOR_SHUTDOWN_MARKER="$STATE_DIR/supervisor-shutdown-in-progress"

# PID 文件
SIDECAR_PID_FILE="$LOG_DIR/sidecar.pid"
MODEL_API_PID_FILE="$LOG_DIR/model_api.pid"
CREATION_PID_FILE="$LOG_DIR/creation.pid"
CORE_PID_FILE="$LOG_DIR/core.pid"
UI_PID_FILE="$LOG_DIR/ui.pid"
UI_APP_PID_FILE="$LOG_DIR/ui_app.pid"
OLLAMA_PID_FILE="$LOG_DIR/ollama.pid"

# 日志文件
SIDECAR_LOG="$LOG_DIR/sidecar.log"
MODEL_API_LOG="$LOG_DIR/model_api.log"
CREATION_LOG="$LOG_DIR/creation.log"
CORE_LOG="$LOG_DIR/core.log"
UI_LOG="$LOG_DIR/ui.log"
OLLAMA_LOG="$LOG_DIR/ollama.log"

# 首次初始化完成后，Ollama 由 MemoryBread 自己管理。restart 必须继续使用
# 同一份受管运行时和专属模型目录；否则系统 Ollama.app 会被重新拉起，实时
# 质检会把已经完成的初始化误判为组件缺失。
INITIALIZATION_ROOT="$HOME/.memory-bread/initialization"
MANAGED_OLLAMA_MARKER="$INITIALIZATION_ROOT/processes/ollama.json"
MANAGED_OLLAMA_RUNTIME_ROOT="$INITIALIZATION_ROOT/runtime/ollama"
MANAGED_OLLAMA_MODELS_ROOT="$INITIALIZATION_ROOT/models"

CORE_PORT=7070
MODEL_API_PORT=7071
CREATION_PORT=8001
CREATION_STARTUP_RETRIES=180
UI_PORT=1420
OLLAMA_PORT=11434
DEBUG_MODE=false

# 打印带颜色的消息
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

maybe_delegate_to_workspace_supervisor() {
    local command="${1:-start}"
    local workspace_start="$PROJECT_ROOT/../start.sh"

    case "$command" in
        start|stop|restart|status|logs)
            ;;
        *)
            return 0
            ;;
    esac

    if [ "${MEMORYBREAD_MB_ALL_CHILD:-}" = "1" ] || is_truthy "${MEMORYBREAD_LOCAL_ONLY:-}"; then
        return 0
    fi

    if [ -f "$workspace_start" ] \
        && [ -f "$PROJECT_ROOT/../mb-admin/start.sh" ] \
        && [ -f "$PROJECT_ROOT/../mb-gateway/start.sh" ]; then
        log_info "检测到完整 mb-all 工作区，交由总启动器管理账户、网关与客户端组件"
        exec bash "$workspace_start" "$@"
    fi
}

is_truthy() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on|debug)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

set_debug_mode_from_env() {
    if is_truthy "${MEMORYBREAD_DEBUG_MODE:-}"; then
        DEBUG_MODE=true
    else
        DEBUG_MODE=false
    fi
}

parse_start_options() {
    set_debug_mode_from_env

    while [ "$#" -gt 0 ]; do
        case "$1" in
            debug|--debug|--debug-mode|--debug=true|--debug-mode=true)
                DEBUG_MODE=true
                ;;
            normal|release|nodebug|no-debug|--no-debug|--debug=false|--debug-mode=false)
                DEBUG_MODE=false
                ;;
            *)
                log_error "未知启动参数: $1"
                echo "用法: $0 {start|stop|restart|status|logs} [--debug|--no-debug]"
                exit 1
                ;;
        esac
        shift
    done
}

require_no_extra_args() {
    local command="$1"
    shift
    if [ "$#" -gt 0 ]; then
        log_error "${command} 不支持额外参数: $*"
        echo "用法: $0 {start|stop|restart|status|logs} [--debug|--no-debug]"
        exit 1
    fi
}

# 检查进程是否运行
process_cwd() {
    local pid=$1
    lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1 || true
}

process_executable() {
    local pid=$1
    # macOS 的 `ps -o comm` 受 MAXCOMLEN 限制，会把
    # `target/debug/memory-bread-desktop` 截成 `target/debug/mem`，导致桌面
    # 进程无法被记录和清理。lsof 的首个 txt 映射是进程主可执行文件，且返回
    # 完整绝对路径；这也能统一识别开发二进制与 .app 内的 helper。
    lsof -a -p "$pid" -d txt -Fn 2>/dev/null \
        | sed -n 's/^n//p' \
        | head -n 1 \
        || true
}

pid_belongs_to_project() {
    local pid=$1
    local cwd

    if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
        return 1
    fi

    cwd=$(process_cwd "$pid")
    case "$cwd" in
        "$PROJECT_ROOT"|"$PROJECT_ROOT"/*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

pid_belongs_to_packaged_app() {
    local pid=$1
    local executable

    if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
        return 1
    fi

    executable=$(process_executable "$pid")
    case "$executable" in
        */记忆面包.app/Contents/MacOS/memory-bread-desktop|\
        */记忆面包.app/Contents/MacOS/memory-bread-core|\
        */记忆面包.app/Contents/Helpers/memory-bread-ai.app/Contents/MacOS/memory-bread-ai)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

pid_belongs_to_memorybread() {
    local pid=$1
    pid_belongs_to_project "$pid" || pid_belongs_to_packaged_app "$pid"
}

pid_is_desktop_app() {
    local pid=$1
    local executable

    executable=$(process_executable "$pid")
    case "$executable" in
        "$PROJECT_ROOT"/desktop-ui/src-tauri/target/*/memory-bread-desktop|\
        */记忆面包.app/Contents/MacOS/memory-bread-desktop)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

is_running() {
    local pid_file=$1
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(tr -d '[:space:]' < "$pid_file")
        if [[ "$pid" =~ ^[0-9]+$ ]] && ps -p "$pid" > /dev/null 2>&1 && pid_belongs_to_project "$pid"; then
            return 0
        fi
    fi
    return 1
}

any_file_newer_than() {
    local marker=$1
    shift

    if [ ! -e "$marker" ]; then
        return 0
    fi

    local path
    local newer_file
    for path in "$@"; do
        if [ -f "$path" ]; then
            if [ "$path" -nt "$marker" ]; then
                return 0
            fi
            continue
        fi
        if [ -d "$path" ]; then
            newer_file=$(find "$path" -type f -newer "$marker" -print -quit 2>/dev/null)
            if [ -n "$newer_file" ]; then
                return 0
            fi
        fi
    done

    return 1
}

creation_service_sources_changed() {
    any_file_newer_than \
        "$CREATION_PID_FILE" \
        "$PROJECT_ROOT/ai-sidecar/creation" \
        "$PROJECT_ROOT/ai-sidecar/start_creation_service.py" \
        "$PROJECT_ROOT/ai-sidecar/model_registry_global.py" \
        "$PROJECT_ROOT/ai-sidecar/model_manager.py"
}

core_sources_changed() {
    any_file_newer_than \
        "$CORE_PID_FILE" \
        "$PROJECT_ROOT/core-engine/src" \
        "$PROJECT_ROOT/core-engine/Cargo.toml" \
        "$PROJECT_ROOT/core-engine/Cargo.lock" \
        "$PROJECT_ROOT/shared/ipc-protocol/rust" \
        "$PROJECT_ROOT/shared/db-schema"
}

python_sources_newer_than() {
    local marker=$1
    shift

    if [ ! -e "$marker" ]; then
        return 0
    fi

    local path
    local newer_file
    for path in "$@"; do
        if [ -f "$path" ]; then
            if [ "$path" -nt "$marker" ]; then
                return 0
            fi
            continue
        fi
        if [ -d "$path" ]; then
            newer_file=$(find "$path" -type f -name '*.py' -newer "$marker" -print -quit 2>/dev/null)
            if [ -n "$newer_file" ]; then
                return 0
            fi
        fi
    done

    return 1
}

sidecar_sources_changed() {
    python_sources_newer_than \
        "$SIDECAR_PID_FILE" \
        "$PROJECT_ROOT/ai-sidecar/main.py" \
        "$PROJECT_ROOT/ai-sidecar/background_processor.py" \
        "$PROJECT_ROOT/ai-sidecar/energy_policy.py" \
        "$PROJECT_ROOT/ai-sidecar/inference_queue.py" \
        "$PROJECT_ROOT/ai-sidecar/model_registry.py" \
        "$PROJECT_ROOT/ai-sidecar/scheduled_task_executor.py" \
        "$PROJECT_ROOT/ai-sidecar/creation" \
        "$PROJECT_ROOT/ai-sidecar/knowledge" \
        "$PROJECT_ROOT/ai-sidecar/ocr" \
        "$PROJECT_ROOT/ai-sidecar/asr" \
        "$PROJECT_ROOT/ai-sidecar/vlm"
}

model_api_sources_changed() {
    python_sources_newer_than \
        "$MODEL_API_PID_FILE" \
        "$PROJECT_ROOT/ai-sidecar/model_api_server.py" \
        "$PROJECT_ROOT/ai-sidecar/initialization_manager.py" \
        "$PROJECT_ROOT/ai-sidecar/model_manager.py" \
        "$PROJECT_ROOT/ai-sidecar/model_registry.py" \
        "$PROJECT_ROOT/ai-sidecar/model_registry_global.py" \
        "$PROJECT_ROOT/ai-sidecar/inference_queue.py" \
        "$PROJECT_ROOT/ai-sidecar/rag" \
        "$PROJECT_ROOT/ai-sidecar/knowledge"
}

stop_managed_process() {
    local pid_file=$1
    local label=$2
    local pid

    if ! is_running "$pid_file"; then
        rm -f "$pid_file"
        return 0
    fi

    pid=$(cat "$pid_file")
    log_info "重启已过期的 ${label} (PID: $pid)"
    kill "$pid" 2>/dev/null || true
    sleep 1
    if ps -p "$pid" > /dev/null 2>&1; then
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pid_file"
}

find_sidecar_pids() {
    local pid command
    while read -r pid command; do
        [ -n "$pid" ] || continue
        case "$command" in
            *"Python main.py"*|*".venv/bin/python main.py"*|*"ai-sidecar/main.py"*)
                if pid_belongs_to_project "$pid"; then
                    printf '%s\n' "$pid"
                fi
                ;;
        esac
    done < <(ps -axo pid=,command=)
}

stop_sidecar_pids() {
    local pids="$1"
    [ -n "$pids" ] || return 0

    echo "$pids" | xargs kill 2>/dev/null || true
    sleep 1
    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        if ps -p "$pid" > /dev/null 2>&1 && pid_belongs_to_project "$pid"; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done <<< "$pids"
}

cleanup_duplicate_sidecars() {
    local keep_pid=""
    local duplicates=""
    local pid

    if is_running "$SIDECAR_PID_FILE"; then
        keep_pid=$(tr -d '[:space:]' < "$SIDECAR_PID_FILE")
    fi

    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        if [ -z "$keep_pid" ] || [ "$pid" != "$keep_pid" ]; then
            duplicates+="${duplicates:+$'\n'}$pid"
        fi
    done < <(find_sidecar_pids)

    if [ -n "$duplicates" ]; then
        log_warn "清理未被 PID 文件管理的重复 AI Sidecar: $(echo "$duplicates" | tr '\n' ' ')"
        stop_sidecar_pids "$duplicates"
    fi
}

is_ollama_ready() {
    curl -fsS "http://localhost:${OLLAMA_PORT}/api/tags" > /dev/null 2>&1
}

resolve_managed_ollama_runtime() {
    [ -f "$MANAGED_OLLAMA_MARKER" ] || return 1

    python3 - \
        "$MANAGED_OLLAMA_MARKER" \
        "$MANAGED_OLLAMA_RUNTIME_ROOT" \
        "$MANAGED_OLLAMA_MODELS_ROOT" <<'PY'
import json
import os
import sys
from pathlib import Path

marker_path = Path(sys.argv[1])
runtime_root = Path(sys.argv[2]).resolve()
expected_models_root = Path(sys.argv[3]).resolve()

try:
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    executable = Path(marker["executable"]).resolve()
    models_root = Path(marker["models_root"]).resolve()
except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)

if runtime_root not in executable.parents:
    raise SystemExit(1)
if models_root != expected_models_root:
    raise SystemExit(1)
if not executable.is_file() or not os.access(str(executable), os.X_OK):
    raise SystemExit(1)

print(executable)
print(models_root)
PY
}

# 清扫托管运行时的孤儿/多余 llama-server，保证全局最多 1 个 llama-server。
# 宿主 ollama serve 被终止后子 runner 会 reparent 到 launchd 继续驻留内存，
# 必须在启动新 serve 前与停止服务后各清扫一次。
sweep_managed_runtime_processes() {
    local guard_script="$PROJECT_ROOT/ai-sidecar/runtime_process_guard.py"
    if [ -f "$guard_script" ] && command -v python3 &> /dev/null; then
        python3 "$guard_script" >> "$LOG_DIR/runtime_guard.log" 2>&1 || true
    fi
}

ensure_ollama_running() {
    local managed_config=""
    local ollama_executable=""
    local ollama_models_root=""

    managed_config=$(resolve_managed_ollama_runtime 2>/dev/null || true)
    if [ -n "$managed_config" ]; then
        ollama_executable=$(printf '%s\n' "$managed_config" | sed -n '1p')
        ollama_models_root=$(printf '%s\n' "$managed_config" | sed -n '2p')
    fi

    if is_ollama_ready; then
        log_info "Ollama 已在运行，复用现有服务"
        return 0
    fi

    if is_running "$OLLAMA_PID_FILE"; then
        local pid=$(cat "$OLLAMA_PID_FILE")
        log_warn "检测到 Ollama 进程存在但未就绪，先清理旧进程 (PID: $pid)"
        kill "$pid" 2>/dev/null || true
        sleep 1
        if ps -p "$pid" > /dev/null 2>&1; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$OLLAMA_PID_FILE"
    fi

    cleanup_port "$OLLAMA_PORT" "Ollama"

    # 启动前清扫历史遗留的孤儿 llama-server，确保新 serve 是唯一宿主。
    sweep_managed_runtime_processes

    if [ -n "$ollama_executable" ]; then
        log_info "启动 MemoryBread 托管 Ollama 服务..."
        OLLAMA_HOST="127.0.0.1:${OLLAMA_PORT}" \
        OLLAMA_MODELS="$ollama_models_root" \
        OLLAMA_NO_CLOUD=1 \
        OLLAMA_NOHISTORY=1 \
        OLLAMA_MAX_LOADED_MODELS=1 \
            nohup "$ollama_executable" serve > "$OLLAMA_LOG" 2>&1 &
    else
        if ! command -v ollama &> /dev/null; then
            log_error "未找到可用的 MemoryBread 托管运行时或 ollama 命令，请重新打开应用完成初始化"
            exit 1
        fi
        log_info "启动系统 Ollama 服务（仅用于首次初始化迁移）..."
        nohup ollama serve > "$OLLAMA_LOG" 2>&1 &
    fi
    echo $! > "$OLLAMA_PID_FILE"

    if wait_for_http "http://localhost:${OLLAMA_PORT}/api/tags" "Ollama" 30 1; then
        log_success "Ollama 已启动 (PID: $(cat "$OLLAMA_PID_FILE"))"
    else
        log_error "Ollama 启动失败，请查看日志: $OLLAMA_LOG"
        if ! ps -p "$(cat "$OLLAMA_PID_FILE" 2>/dev/null)" > /dev/null 2>&1; then
            rm -f "$OLLAMA_PID_FILE"
        fi
        exit 1
    fi
}

cleanup_port() {
    local port=$1
    local label=$2
    local pid
    local listeners
    local project_pids=()
    local foreign_pids=()

    # 只匹配本机监听端口。`lsof -ti :PORT` 还会命中连接到远端同名端口的
    # 客户端进程（例如微信连接远端 8080），不能用于进程清理。
    listeners=$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u || true)
    if [ -z "$listeners" ]; then
        return 0
    fi

    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        if pid_belongs_to_memorybread "$pid"; then
            project_pids+=("$pid")
        else
            foreign_pids+=("$pid")
        fi
    done <<< "$listeners"

    if [ "${#foreign_pids[@]}" -gt 0 ]; then
        log_error "${label} 端口 ${port} 被非本项目进程占用 (PID: ${foreign_pids[*]})，为避免误杀已停止启动"
        return 1
    fi

    if [ "${#project_pids[@]}" -gt 0 ]; then
        log_info "清理占用 ${port} 端口的 MemoryBread 进程（${label}）: ${project_pids[*]}"
        kill "${project_pids[@]}" 2>/dev/null || true
        sleep 1
        for pid in "${project_pids[@]}"; do
            if ps -p "$pid" > /dev/null 2>&1 && pid_belongs_to_memorybread "$pid"; then
                kill -9 "$pid" 2>/dev/null || true
            fi
        done
    fi
}

find_packaged_backend_pids() {
    local pid
    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        if pid_belongs_to_packaged_app "$pid" && ! pid_is_desktop_app "$pid"; then
            printf '%s\n' "$pid"
        fi
    done < <(pgrep -f "memory-bread-ai|memory-bread-core" || true)
}

cleanup_packaged_backends() {
    local pids

    pids=$(find_packaged_backend_pids)
    if [ -z "$pids" ]; then
        return 0
    fi

    log_info "清理残留的已打包客户端服务: $(echo "$pids" | tr '\n' ' ' | xargs)"
    echo "$pids" | xargs kill 2>/dev/null || true
    sleep 1
    pids=$(find_packaged_backend_pids)
    if [ -n "$pids" ]; then
        echo "$pids" | xargs kill -9 2>/dev/null || true
    fi
}

cleanup_desktop_app() {
    local pids=$(find_desktop_app_pids)
    if [ -n "$pids" ]; then
        log_info "清理残留 Desktop UI 窗口进程: $pids"
        echo "$pids" | xargs kill 2>/dev/null || true
        sleep 1
        pids=$(find_desktop_app_pids)
        if [ -n "$pids" ]; then
            echo "$pids" | xargs kill -9 2>/dev/null || true
        fi
    fi
    rm -f "$UI_APP_PID_FILE"
    cleanup_packaged_backends
}

find_desktop_app_pids() {
    local pid
    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        if pid_is_desktop_app "$pid"; then
            printf '%s\n' "$pid"
        fi
    done < <(pgrep -f "memory-bread-desktop" || true)
}

warn_if_multiple_desktop_apps() {
    local pids=$(find_desktop_app_pids)
    if [ -z "$pids" ]; then
        return 0
    fi

    local count=$(echo "$pids" | wc -l | tr -d '[:space:]')
    if [ "$count" -gt 1 ]; then
        log_warn "检测到 ${count} 个 Desktop UI 历史残留窗口进程，restart 将先自动清理: $(echo "$pids" | tr '\n' ' ' | xargs)"
    fi
}

record_desktop_app_pid() {
    local retries=${1:-20}
    local delay=${2:-1}

    for ((i=1; i<=retries; i++)); do
        local pids=$(find_desktop_app_pids)
        if [ -n "$pids" ]; then
            local pid=$(echo "$pids" | tail -n 1 | tr -d '[:space:]')
            if [ -n "$pid" ]; then
                echo "$pid" > "$UI_APP_PID_FILE"
                return 0
            fi
        fi

        # 启动器已退出时立即返回，避免失败场景仍固定等待三分钟。
        if ! is_running "$UI_PID_FILE"; then
            return 1
        fi
        sleep "$delay"
    done

    return 1
}

wait_for_http() {
    local url=$1
    local label=$2
    local retries=${3:-20}
    local delay=${4:-1}

    for ((i=1; i<=retries; i++)); do
        if curl -fsS "$url" > /dev/null 2>&1; then
            log_success "${label} 健康检查通过"
            return 0
        fi
        sleep "$delay"
    done

    log_warn "${label} 健康检查失败，请查看日志"
    return 1
}

wait_for_managed_http() {
    local url=$1
    local label=$2
    local pid_file=$3
    local retries=${4:-20}
    local delay=${5:-1}

    for ((i=1; i<=retries; i++)); do
        if curl -fsS "$url" > /dev/null 2>&1; then
            log_success "${label} 健康检查通过"
            return 0
        fi
        if ! is_running "$pid_file"; then
            log_warn "${label} 进程已退出，健康检查终止"
            return 1
        fi
        sleep "$delay"
    done

    log_warn "${label} 健康检查超时，请查看日志"
    return 1
}

is_http_ok() {
    local url=$1
    curl -fsS "$url" > /dev/null 2>&1
}

check_core_api_readiness() {
    local failed=0

    wait_for_http "http://localhost:${CORE_PORT}/health" "Core Engine" 20 1 || failed=1
    wait_for_http "http://localhost:${CORE_PORT}/api/creation/history?paged=true&limit=1&offset=0" "Core API /api/creation/history" 10 1 || failed=1
    wait_for_http "http://localhost:${CORE_PORT}/api/bake/captures?limit=1" "Core API /api/bake/captures" 10 1 || failed=1
    wait_for_http "http://localhost:${CORE_PORT}/api/data/sources?limit=1&offset=0" "Core API /api/data/sources" 10 1 || failed=1
    wait_for_http "http://localhost:${CORE_PORT}/api/monitor/overview?range=7d" "Core API /api/monitor/overview" 10 1 || failed=1

    if [ "$failed" -ne 0 ]; then
        log_warn "Core API 自检未全部通过，请优先检查: $CORE_LOG"
        return 1
    fi

    log_success "Core API 关键接口自检通过"
    return 0
}

show_status() {
    local failed=0

    echo ""
    if is_running "$SIDECAR_PID_FILE"; then
        log_success "AI Sidecar: 运行中 (PID: $(cat "$SIDECAR_PID_FILE"))"
    else
        log_error "AI Sidecar: 未运行"
        failed=1
    fi

    if is_running "$MODEL_API_PID_FILE"; then
        local model_api_pid=$(cat "$MODEL_API_PID_FILE")
        if is_http_ok "http://localhost:${MODEL_API_PORT}/health"; then
            log_success "Model API / RAG API: 运行中 (PID: ${model_api_pid}, Port: ${MODEL_API_PORT})"
        else
            log_error "Model API / RAG API: 进程存在但接口未就绪 (PID: ${model_api_pid}, Port: ${MODEL_API_PORT})"
            failed=1
        fi
    else
        log_error "Model API / RAG API: 未运行"
        failed=1
    fi

    if is_ollama_ready; then
        if is_running "$OLLAMA_PID_FILE"; then
            log_success "Ollama: 运行中 (PID: $(cat "$OLLAMA_PID_FILE"), Port: ${OLLAMA_PORT})"
        else
            log_success "Ollama: 运行中 (Port: ${OLLAMA_PORT})"
        fi
    else
        log_error "Ollama: 未运行 (Port: ${OLLAMA_PORT})"
        failed=1
    fi

    if is_running "$CREATION_PID_FILE"; then
        local creation_pid=$(cat "$CREATION_PID_FILE")
        if is_http_ok "http://localhost:${CREATION_PORT}/health"; then
            log_success "Creation Service: 运行中 (PID: ${creation_pid}, Port: ${CREATION_PORT})"
        else
            log_error "Creation Service: 进程存在但接口未就绪 (PID: ${creation_pid}, Port: ${CREATION_PORT})"
            failed=1
        fi
    else
        log_error "Creation Service: 未运行"
        failed=1
    fi

    if is_running "$CORE_PID_FILE"; then
        local core_pid=$(cat "$CORE_PID_FILE")
        if is_http_ok "http://localhost:${CORE_PORT}/health"; then
            log_success "Core Engine: 运行中 (PID: ${core_pid}, Port: ${CORE_PORT})"
        else
            log_error "Core Engine: 进程存在但接口未就绪 (PID: ${core_pid}, Port: ${CORE_PORT})"
            failed=1
        fi
    else
        log_error "Core Engine: 未运行"
        failed=1
    fi

    if is_running "$UI_PID_FILE"; then
        if is_http_ok "http://localhost:${UI_PORT}"; then
            local ui_msg="Desktop UI: 运行中 (启动器 PID: $(cat "$UI_PID_FILE"), Port: ${UI_PORT}"
            if is_running "$UI_APP_PID_FILE"; then
                ui_msg+="，窗口 PID: $(cat "$UI_APP_PID_FILE")"
            fi
            ui_msg+=")"
            log_success "$ui_msg"
        else
            log_error "Desktop UI: 启动器存在但接口未就绪 (PID: $(cat "$UI_PID_FILE"), Port: ${UI_PORT})"
            failed=1
        fi
    else
        log_error "Desktop UI: 未运行"
        failed=1
    fi

    local desktop_pids=$(find_desktop_app_pids)
    if [ -n "$desktop_pids" ]; then
        local desktop_count=$(echo "$desktop_pids" | wc -l | tr -d '[:space:]')
        log_info "Desktop UI 窗口进程数: ${desktop_count} (PID: $(echo "$desktop_pids" | tr '\n' ' ' | xargs))"
    else
        log_info "Desktop UI 窗口进程数: 0"
        failed=1
    fi
    echo ""
    return "$failed"
}

# 停止所有服务
stop_all() {
    local keep_supervisor_marker=${1:-false}
    log_info "停止所有服务..."
    # Desktop 收到退出事件时会自行派生 stop-after-app。由启动器主动 stop/restart
    # 时先放置短生命周期标记，避免旧 Desktop 的延迟清理误杀刚启动的新实例。
    : > "$SUPERVISOR_SHUTDOWN_MARKER"

    # 停止 Desktop UI（包括子进程）
    if is_running "$UI_PID_FILE"; then
        local pid=$(cat "$UI_PID_FILE")
        log_info "停止 Desktop UI (启动器 PID: $pid)"
        # 先尝试优雅关闭
        pkill -P "$pid" 2>/dev/null || true
        kill "$pid" 2>/dev/null || true
        sleep 1
        # 如果还在运行，强制杀掉
        if ps -p "$pid" > /dev/null 2>&1; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$UI_PID_FILE"
    fi

    if is_running "$UI_APP_PID_FILE"; then
        local app_pid=$(cat "$UI_APP_PID_FILE")
        log_info "停止 Desktop UI 窗口进程 (PID: $app_pid)"
        kill "$app_pid" 2>/dev/null || true
        sleep 1
        if ps -p "$app_pid" > /dev/null 2>&1; then
            kill -9 "$app_pid" 2>/dev/null || true
        fi
        rm -f "$UI_APP_PID_FILE"
    fi

    cleanup_port "$UI_PORT" "Desktop UI / Vite"
    cleanup_desktop_app

    # 停止 Core Engine
    if is_running "$CORE_PID_FILE"; then
        local pid=$(cat "$CORE_PID_FILE")
        log_info "停止 Core Engine (PID: $pid)"
        kill "$pid" 2>/dev/null || true
        sleep 1
        if ps -p "$pid" > /dev/null 2>&1; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$CORE_PID_FILE"
    fi

    cleanup_port "$CORE_PORT" "Core Engine"

    # 停止 Creation Service
    if is_running "$CREATION_PID_FILE"; then
        local pid=$(cat "$CREATION_PID_FILE")
        log_info "停止 Creation Service (PID: $pid)"
        kill "$pid" 2>/dev/null || true
        sleep 1
        if ps -p "$pid" > /dev/null 2>&1; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$CREATION_PID_FILE"
    fi

    cleanup_port "$CREATION_PORT" "Creation Service"

    # 停止 AI Sidecar
    if is_running "$SIDECAR_PID_FILE"; then
        local pid=$(cat "$SIDECAR_PID_FILE")
        log_info "停止 AI Sidecar (PID: $pid)"
        kill "$pid" 2>/dev/null || true
        sleep 1
        if ps -p "$pid" > /dev/null 2>&1; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$SIDECAR_PID_FILE"
    fi

    # PID 文件只能记录一个进程；继续清理手工启动或历史启动器遗留的 Sidecar。
    local remaining_sidecars=$(find_sidecar_pids)
    if [ -n "$remaining_sidecars" ]; then
        log_info "停止未登记的 AI Sidecar: $(echo "$remaining_sidecars" | tr '\n' ' ')"
        stop_sidecar_pids "$remaining_sidecars"
    fi
    rm -f /tmp/memory-bread-sidecar.sock

    # 停止 Model API Server
    if is_running "$MODEL_API_PID_FILE"; then
        local pid=$(cat "$MODEL_API_PID_FILE")
        log_info "停止 Model API Server (PID: $pid)"
        kill "$pid" 2>/dev/null || true
        sleep 1
        if ps -p "$pid" > /dev/null 2>&1; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$MODEL_API_PID_FILE"
    fi

    cleanup_port "$MODEL_API_PORT" "Model API / RAG API"

    # 停止由脚本启动的 Ollama
    if is_running "$OLLAMA_PID_FILE"; then
        local pid=$(cat "$OLLAMA_PID_FILE")
        log_info "停止 Ollama (PID: $pid)"
        kill "$pid" 2>/dev/null || true
        sleep 1
        if ps -p "$pid" > /dev/null 2>&1; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$OLLAMA_PID_FILE"
    fi

    # 只杀宿主会留下 llama-server 孤儿；无条件兜底清扫（无 pid 文件时也可能有历史遗留）。
    sweep_managed_runtime_processes

    if [ "$keep_supervisor_marker" != "true" ]; then
        rm -f "$SUPERVISOR_SHUTDOWN_MARKER"
    fi
    log_success "所有服务已停止"
}

# 由菜单栏 App 的“退出”触发：先等待当前窗口进程退出，再清理整套服务。
stop_after_app() {
    local app_pid=$1
    local registered_app_pid=""
    if ! [[ "$app_pid" =~ ^[0-9]+$ ]]; then
        log_error "无效的 Desktop UI PID: $app_pid"
        exit 1
    fi

    if [ -f "$UI_APP_PID_FILE" ]; then
        registered_app_pid=$(tr -d '[:space:]' < "$UI_APP_PID_FILE")
    fi
    if [[ "$registered_app_pid" =~ ^[0-9]+$ ]] && [ "$registered_app_pid" != "$app_pid" ]; then
        log_info "忽略旧 Desktop UI 派生的延迟清理 (旧 PID: ${app_pid}，新 PID: ${registered_app_pid})"
        exit 0
    fi

    if [ -f "$SUPERVISOR_SHUTDOWN_MARKER" ]; then
        log_info "启动器正在停止组件，忽略 Desktop 派生的重复清理"
        exit 0
    fi

    for _ in {1..50}; do
        if ! ps -p "$app_pid" > /dev/null 2>&1; then
            break
        fi
        sleep 0.2
    done

    if [ -f "$SUPERVISOR_SHUTDOWN_MARKER" ]; then
        log_info "启动器已接管组件停止，忽略 Desktop 派生的重复清理"
        exit 0
    fi

    if [ -f "$UI_APP_PID_FILE" ]; then
        registered_app_pid=$(tr -d '[:space:]' < "$UI_APP_PID_FILE")
    fi
    if [[ "$registered_app_pid" =~ ^[0-9]+$ ]] && [ "$registered_app_pid" != "$app_pid" ]; then
        log_info "Desktop UI 已换代，忽略旧实例的延迟清理 (旧 PID: ${app_pid}，新 PID: ${registered_app_pid})"
        exit 0
    fi

    stop_all
}

# 检查依赖
check_dependencies() {
    log_info "检查依赖..."

    # 检查 Python
    if ! command -v python3 &> /dev/null; then
        log_error "未找到 python3，请先安装 Python 3.11+"
        exit 1
    fi

    # 检查 Rust
    if ! command -v cargo &> /dev/null; then
        log_error "未找到 cargo，请先安装 Rust"
        exit 1
    fi

    # 检查 Node.js
    if ! command -v node &> /dev/null; then
        log_error "未找到 node，请先安装 Node.js 18+"
        exit 1
    fi

    log_success "依赖检查通过"
}

clean_stale_tauri_cache() {
    local tauri_root="$PROJECT_ROOT/desktop-ui/src-tauri"
    local tauri_target="$tauri_root/target"
    local stale_marker=""
    local dependency_file
    local dependency_files=(
        "$tauri_target/debug/memory-bread-desktop.d"
        "$tauri_target/debug/libmemory_bread_desktop_lib.d"
    )

    if [ ! -d "$tauri_target" ]; then
        return 0
    fi

    # Cargo 的 .d 文件会记录当前 crate 的绝对源码路径。若仓库迁移后记录中
    # 不再包含当前路径，清理缓存即可，无需保存开发者本机的历史目录。
    for dependency_file in "${dependency_files[@]}"; do
        if [ -f "$dependency_file" ] && ! grep -Fq "$tauri_root" "$dependency_file"; then
            stale_marker="relocated source path"
            break
        fi
    done

    if [ -z "$stale_marker" ] && [ -f "$UI_LOG" ]; then
        if grep -Fq "failed to read plugin permissions" "$UI_LOG"; then
            stale_marker="failed to read plugin permissions"
        fi
    fi

    if [ -n "$stale_marker" ]; then
        log_warn "检测到 Tauri 构建缓存包含旧路径或权限生成残留，正在清理: $stale_marker"
        (cd "$tauri_root" && cargo clean)
    fi
}

# 启动 AI Sidecar
start_sidecar() {
    cleanup_duplicate_sidecars

    if is_running "$SIDECAR_PID_FILE" && is_running "$MODEL_API_PID_FILE"; then
        if sidecar_sources_changed; then
            log_info "检测到 AI Sidecar 源码已更新，将自动加载最新代码"
            stop_managed_process "$SIDECAR_PID_FILE" "AI Sidecar"
        elif model_api_sources_changed; then
            log_info "检测到 Model API 源码已更新，将自动加载最新代码"
            stop_managed_process "$MODEL_API_PID_FILE" "Model API"
        elif ! is_http_ok "http://localhost:${MODEL_API_PORT}/api/initialization/status"; then
            log_warn "现有 Model API 缺少初始化接口，将自动升级运行中的服务"
            stop_managed_process "$MODEL_API_PID_FILE" "Model API"
        else
            log_info "AI Sidecar 与 Model API 已在运行，复用现有进程"
            wait_for_http "http://localhost:${MODEL_API_PORT}/health" "Model API / RAG API" 10 1 || log_warn "现有 Model API 进程健康检查失败，建议执行 ./start.sh restart"
            return 0
        fi
    fi

    if is_running "$SIDECAR_PID_FILE" && ! is_running "$MODEL_API_PID_FILE"; then
        log_warn "检测到 Model API 未运行，先停止现有 AI Sidecar 后整体拉起"
        local pid=$(cat "$SIDECAR_PID_FILE")
        kill "$pid" 2>/dev/null || true
        sleep 1
        if ps -p "$pid" > /dev/null 2>&1; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$SIDECAR_PID_FILE"
    fi

    if ! is_running "$SIDECAR_PID_FILE" && is_running "$MODEL_API_PID_FILE"; then
        log_warn "检测到 AI Sidecar 未运行，先停止现有 Model API 后整体拉起"
        local pid=$(cat "$MODEL_API_PID_FILE")
        kill "$pid" 2>/dev/null || true
        sleep 1
        if ps -p "$pid" > /dev/null 2>&1; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$MODEL_API_PID_FILE"
    fi

    log_info "启动 AI Sidecar..."

    cd "$PROJECT_ROOT/ai-sidecar"

    # 检查虚拟环境
    if [ ! -d ".venv" ]; then
        log_warn "虚拟环境不存在，正在创建..."
        python3 -m venv .venv
        source .venv/bin/activate
        pip install -r requirements.txt
    else
        source .venv/bin/activate
    fi

    cleanup_port "$MODEL_API_PORT" "Model API / RAG API"

    # 启动 Sidecar（后台运行）
    nohup .venv/bin/python main.py > "$SIDECAR_LOG" 2>&1 &
    echo $! > "$SIDECAR_PID_FILE"

    # 单实例锁或启动检查失败时进程会快速退出，不能把失效 PID 当成启动成功。
    sleep 0.2
    if ! is_running "$SIDECAR_PID_FILE"; then
        log_error "AI Sidecar 启动失败或已有未受管实例，请查看日志: $SIDECAR_LOG"
        rm -f "$SIDECAR_PID_FILE"
        exit 1
    fi

    # 启动 Model API / RAG API Server（后台运行）
    nohup .venv/bin/python model_api_server.py > "$MODEL_API_LOG" 2>&1 &
    echo $! > "$MODEL_API_PID_FILE"

    log_success "AI Sidecar 已启动 (PID: $(cat "$SIDECAR_PID_FILE"))"
    log_success "Model API / RAG API 已启动 (PID: $(cat "$MODEL_API_PID_FILE"))"
    log_info "Sidecar 日志文件: $SIDECAR_LOG"
    log_info "Model API / RAG API 日志文件: $MODEL_API_LOG"

    # 等待 Sidecar 与 7071 API 启动
    log_info "等待 AI Sidecar 初始化..."
    sleep 3
    wait_for_http "http://localhost:${MODEL_API_PORT}/health" "Model API / RAG API" 40 2 || {
        log_warn "Model API / RAG API 未就绪，可查看日志: $MODEL_API_LOG"
        if ! ps -p "$(cat "$MODEL_API_PID_FILE" 2>/dev/null)" > /dev/null 2>&1; then
            rm -f "$MODEL_API_PID_FILE"
        fi
    }
    wait_for_http "http://localhost:${MODEL_API_PORT}/api/initialization/status" "Initialization API" 10 1 || {
        log_error "Initialization API 未就绪，请查看日志: $MODEL_API_LOG"
        return 1
    }
}

# 启动 Creation Service
start_creation_service() {
    if is_running "$CREATION_PID_FILE"; then
        if creation_service_sources_changed; then
            log_info "检测到 Creation Service 源码晚于当前进程，将加载最新代码"
            stop_managed_process "$CREATION_PID_FILE" "Creation Service"
        else
            log_info "Creation Service 已在运行且代码未变化，复用现有进程"
            wait_for_http "http://127.0.0.1:${CREATION_PORT}/health" "Creation Service" 10 1 || log_warn "现有 Creation Service 进程健康检查失败，建议执行 ./start.sh restart"
            return 0
        fi
    fi

    log_info "启动 Creation Service..."

    cd "$PROJECT_ROOT/ai-sidecar"

    if [ ! -d ".venv" ]; then
        log_warn "虚拟环境不存在，正在创建..."
        python3 -m venv .venv
        source .venv/bin/activate
        pip install -r requirements.txt
    else
        source .venv/bin/activate
    fi

    cleanup_port "$CREATION_PORT" "Creation Service"

    nohup .venv/bin/python start_creation_service.py > "$CREATION_LOG" 2>&1 < /dev/null &
    echo $! > "$CREATION_PID_FILE"
    disown "$(cat "$CREATION_PID_FILE")" 2>/dev/null || true

    log_success "Creation Service 已启动 (PID: $(cat "$CREATION_PID_FILE"))"
    log_info "Creation Service 日志文件: $CREATION_LOG"
    log_info "等待 Creation Service 初始化（首次加载本地模型时最多需要三分钟）..."

    # Creation Service 导入本地 embedding 与模型注册表；冷启动明显慢于普通
    # HTTP 服务。等待期间同时观察受管进程，真实崩溃时立即失败，仍在初始化时
    # 最多等待三分钟，避免留下“脚本报失败、服务稍后却可用”的假失败状态。
    wait_for_managed_http \
        "http://127.0.0.1:${CREATION_PORT}/health" \
        "Creation Service" \
        "$CREATION_PID_FILE" \
        "$CREATION_STARTUP_RETRIES" \
        1 || {
        log_error "Creation Service 启动失败，请查看日志: $CREATION_LOG"
        if ! ps -p "$(cat "$CREATION_PID_FILE" 2>/dev/null)" > /dev/null 2>&1; then
            rm -f "$CREATION_PID_FILE"
        fi
        exit 1
    }
}

# 启动 Core Engine
start_core() {
    local replace_running_core=false

    if is_running "$CORE_PID_FILE"; then
        if core_sources_changed; then
            log_info "检测到 Core Engine 源码晚于当前进程，将先完成构建再加载最新代码"
            replace_running_core=true
        else
            log_info "Core Engine 已在运行且代码未变化，复用现有进程"
            check_core_api_readiness || log_warn "当前 Core Engine 进程存在接口异常，建议执行 ./start.sh restart 进行完整重启"
            return 0
        fi
    fi

    log_info "启动 Core Engine..."

    cd "$PROJECT_ROOT/core-engine"

    # 构建最新 Core Engine
    log_info "构建最新 Core Engine..."
    cargo build --release

    # 构建失败时 `set -e` 会直接退出，现有 Core 继续服务；仅在新二进制
    # 准备完毕后切换进程，避免 Desktop UI 在编译期间同时失去创作和记录接口。
    if [ "$replace_running_core" = true ]; then
        stop_managed_process "$CORE_PID_FILE" "Core Engine"
    fi

    cleanup_port "$CORE_PORT" "Core Engine"

    # 启动 Core Engine（后台运行）
    nohup ./target/release/memory-bread > "$CORE_LOG" 2>&1 &
    echo $! > "$CORE_PID_FILE"

    log_success "Core Engine 已启动 (PID: $(cat "$CORE_PID_FILE"))"
    log_info "日志文件: $CORE_LOG"

    # 等待 Core Engine 启动
    log_info "等待 Core Engine 初始化..."
    sleep 3

    check_core_api_readiness
}

# 启动 Desktop UI
start_ui() {
    if is_running "$UI_PID_FILE"; then
        log_info "Desktop UI 已在运行，复用现有进程；如需切换调试模式请执行 restart"
        wait_for_http "http://localhost:${UI_PORT}" "Desktop UI / Vite" 10 1 || log_warn "现有 Desktop UI 进程健康检查失败，建议执行 ./start.sh restart"
        return 0
    fi

    log_info "启动 Desktop UI..."
    log_info "Desktop UI 调试模式: ${DEBUG_MODE}"

    cd "$PROJECT_ROOT/desktop-ui"

    # 检查 node_modules
    if [ ! -d "node_modules" ]; then
        log_warn "node_modules 不存在，正在安装依赖..."
        npm install
    fi

    # 确保 Rust 在 PATH 中
    export PATH="$HOME/.cargo/bin:$PATH"

    cleanup_port "$UI_PORT" "Desktop UI / Vite"
    cleanup_desktop_app
    clean_stale_tauri_cache

    # 启动 Tauri 开发服务器（后台运行）
    log_info "启动 Tauri 开发服务器..."
    VITE_MEMORYBREAD_DEBUG_MODE="$DEBUG_MODE" nohup npm run tauri:dev > "$UI_LOG" 2>&1 &
    echo $! > "$UI_PID_FILE"
    disown "$(cat "$UI_PID_FILE")" 2>/dev/null || true

    log_success "Desktop UI 已启动 (启动器 PID: $(cat "$UI_PID_FILE"))"
    log_info "日志文件: $UI_LOG"
    log_info "等待 Desktop UI 初始化（首次清理缓存后可能需要较长时间编译）..."

    if record_desktop_app_pid 180 1; then
        log_success "Desktop UI 窗口进程已记录 (PID: $(cat "$UI_APP_PID_FILE"))"
    else
        if ! is_running "$UI_PID_FILE"; then
            log_error "Desktop UI 启动器已退出，请查看日志: $UI_LOG"
            rm -f "$UI_PID_FILE"
            exit 1
        fi
        log_warn "未能记录 Desktop UI 窗口进程 PID，后续将依赖残留扫描兜底"
    fi

    if curl -fsS "http://localhost:${UI_PORT}" > /dev/null 2>&1; then
        log_success "Desktop UI / Vite 健康检查通过"
    else
        log_warn "Desktop UI / Vite 健康检查失败，请查看日志"
    fi
}

# 主函数
main() {
    maybe_delegate_to_workspace_supervisor "$@"

    local command="${1:-start}"
    if [ "$#" -gt 0 ]; then
        shift
    fi

    echo ""
    echo "╔════════════════════════════════════════╗"
    echo "║     记忆面包 启动脚本 v1.0           ║"
    echo "╚════════════════════════════════════════╝"
    echo ""

    # 解析命令行参数
    case "$command" in
        start)
            parse_start_options "$@"
            check_path_leaks
            check_dependencies
            ensure_ollama_running
            start_sidecar
            start_creation_service
            start_core
            start_ui
            show_status
            ;;
        start-backends)
            require_no_extra_args "$command" "$@"
            check_path_leaks
            check_dependencies
            ensure_ollama_running
            start_sidecar
            start_creation_service
            start_core
            ;;
        stop)
            require_no_extra_args "$command" "$@"
            stop_all
            ;;
        stop-after-app)
            stop_after_app "${1:-}"
            ;;
        restart)
            parse_start_options "$@"
            log_info "执行全组件 restart（AI Sidecar → Core Engine → Desktop UI）..."
            warn_if_multiple_desktop_apps
            # 标记覆盖完整重启窗口，不能在旧进程刚退出时就删除。否则它已经
            # 派生的 stop-after-app 会在新进程启动后执行，再次停掉整套服务。
            # EXIT 兜底保证中途构建或启动失败时不会遗留永久抑制清理的标记。
            trap 'rm -f "$SUPERVISOR_SHUTDOWN_MARKER"' EXIT
            stop_all true
            sleep 2
            check_path_leaks
            check_dependencies
            ensure_ollama_running
            start_sidecar
            start_creation_service
            start_core
            start_ui
            rm -f "$SUPERVISOR_SHUTDOWN_MARKER"
            trap - EXIT
            show_status
            log_info "联调测试前请优先使用 ./start.sh restart，7071 由 model_api_server.py 统一提供 /api/models + /query，避免旧进程状态污染测试结果"
            ;;
        status)
            require_no_extra_args "$command" "$@"
            show_status
            ;;
        logs)
            require_no_extra_args "$command" "$@"
            log_info "查看日志 (Ctrl+C 退出)..."
            tail -f "$SIDECAR_LOG" "$MODEL_API_LOG" "$CREATION_LOG" "$CORE_LOG" "$UI_LOG" 2>/dev/null
            ;;
        *)
            echo "用法: $0 {start|stop|restart|status|logs} [--debug|--no-debug]"
            echo ""
            echo "命令说明:"
            echo "  start [--debug]   - 启动完整工作区；设置 MEMORYBREAD_LOCAL_ONLY=1 时仅启动客户端本地组件"
            echo "  stop              - 停止对应范围的服务"
            echo "  restart [--debug] - 重启对应范围的服务"
            echo "  status            - 查看对应范围的服务状态"
            echo "  logs              - 查看对应范围的实时日志"
            exit 1
            ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    # 捕获 Ctrl+C 信号
    trap 'echo ""; log_info "收到中断信号，正在停止服务..."; stop_all; exit 0' INT TERM

    # 执行主函数
    main "$@"
fi
