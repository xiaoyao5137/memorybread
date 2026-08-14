#!/bin/bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_SCRIPT="$PROJECT_ROOT/scripts/build-macos.sh"
TAURI_TARGET_DIR="$PROJECT_ROOT/desktop-ui/src-tauri/target"
PYTHON39_BIN="${MEMORY_BREAD_PYTHON39_BIN:-/usr/bin/python3}"

fail() {
  echo "[DMG install] $*" >&2
  exit 1
}

host_target() {
  case "$(uname -m)" in
    arm64) echo "aarch64-apple-darwin" ;;
    x86_64) echo "x86_64-apple-darwin" ;;
    *) fail "不支持的 Mac 架构: $(uname -m)" ;;
  esac
}

check_python39_compatibility() {
  [ -x "$PYTHON39_BIN" ] \
    || fail "未找到 Python 3.9：$PYTHON39_BIN（可通过 MEMORY_BREAD_PYTHON39_BIN 指定）"

  local python_version
  python_version="$($PYTHON39_BIN -c 'import platform; print(platform.python_version())')"
  case "$python_version" in
    3.9.*) ;;
    *) fail "Python 兼容性门禁需要 3.9.x，当前是 ${python_version}: $PYTHON39_BIN" ;;
  esac

  echo "[DMG install] 检查 Python 3.9 语法兼容性..."
  "$PYTHON39_BIN" - "$PROJECT_ROOT" <<'PY'
import ast
import pathlib
import sys

project_root = pathlib.Path(sys.argv[1])
source_roots = (
    project_root / "ai-sidecar",
    project_root / "shared" / "ipc-protocol" / "python",
)
excluded_parts = {".pytest_cache", ".venv", "__pycache__", "tests"}
checked = 0
failures = []


def annotation_has_pep604_union(annotation):
    for node in ast.walk(annotation):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "|" in node.value:
            return True
    return False


def iter_annotations(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            yield node.annotation
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            yield node.returns


for source_root in source_roots:
    for source_path in sorted(source_root.rglob("*.py")):
        if excluded_parts.intersection(source_path.parts) or any(
            part.startswith(".venv") for part in source_path.parts
        ):
            continue
        try:
            source = source_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(source_path))
            compile(source, str(source_path), "exec", dont_inherit=True)
            for annotation in iter_annotations(tree):
                if annotation_has_pep604_union(annotation):
                    line = getattr(annotation, "lineno", 1)
                    raise SyntaxError(
                        "Python 3.10+ 的 X | Y 类型联合不允许用于客户端打包代码",
                        (str(source_path), line, 1, ""),
                    )
        except Exception as exc:
            failures.append("{}: {}".format(source_path, exc))
        checked += 1

if failures:
    for failure in failures:
        print("[DMG install] {}".format(failure), file=sys.stderr)
    raise SystemExit(1)

print("[DMG install] Python 3.9 兼容性检查通过（{} 个文件）".format(checked))
PY
}

locate_latest_dmg() {
  local target="$1"
  local dmg_dir="$TAURI_TARGET_DIR/$target/release/bundle/dmg"
  local latest=""
  local candidate

  [ -d "$dmg_dir" ] || return 1
  while IFS= read -r -d '' candidate; do
    if [ -z "$latest" ] || [ "$candidate" -nt "$latest" ]; then
      latest="$candidate"
    fi
  done < <(find "$dmg_dir" -maxdepth 1 -type f -name '*.dmg' -print0)

  [ -n "$latest" ] || return 1
  printf '%s\n' "$latest"
}

[ "$(uname -s)" = "Darwin" ] || fail "DMG 只能在 macOS 上构建和安装"
[ -x "$BUILD_SCRIPT" ] || fail "构建脚本不存在或不可执行: $BUILD_SCRIPT"
command -v open >/dev/null 2>&1 || fail "缺少 macOS open 命令"

TARGET="${MEMORY_BREAD_MACOS_TARGET:-$(host_target)}"
check_python39_compatibility

echo "[DMG install] 开始构建 ${TARGET} DMG..."
"$BUILD_SCRIPT" dmg

DMG_PATH="$(locate_latest_dmg "$TARGET")" \
  || fail "构建完成，但没有在 ${TAURI_TARGET_DIR}/${TARGET}/release/bundle/dmg 找到 DMG"

echo "[DMG install] DMG: $DMG_PATH"
echo "[DMG install] 正在打开安装窗口..."
open "$DMG_PATH"
echo "[DMG install] 已触发安装，请在 Finder 中将“记忆面包”拖到 Applications。"
