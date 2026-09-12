#!/bin/bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

mkdir -p \
    "$TEST_ROOT/project/core-engine/target/debug" \
    "$TEST_ROOT/project/desktop-ui/src-tauri/target/debug" \
    "$TEST_ROOT/bin"
touch "$TEST_ROOT/project/core-engine/Cargo.toml"
touch "$TEST_ROOT/project/desktop-ui/src-tauri/Cargo.toml"
dd if=/dev/zero of="$TEST_ROOT/project/core-engine/target/debug/cache" bs=1024 count=8 >/dev/null 2>&1
dd if=/dev/zero of="$TEST_ROOT/project/desktop-ui/src-tauri/target/debug/cache" bs=1024 count=8 >/dev/null 2>&1

cat > "$TEST_ROOT/bin/pgrep" <<'EOF'
#!/bin/bash
exit 1
EOF
cat > "$TEST_ROOT/bin/cargo" <<'EOF'
#!/bin/bash
set -e
manifest=""
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--manifest-path" ]; then
        manifest=$2
        break
    fi
    shift
done
rm -rf "$(dirname "$manifest")/target"
EOF
chmod +x "$TEST_ROOT/bin/pgrep" "$TEST_ROOT/bin/cargo"

output=$(PATH="$TEST_ROOT/bin:$PATH" \
    MEMORYBREAD_PROJECT_ROOT="$TEST_ROOT/project" \
    MEMORYBREAD_CARGO_BIN="$TEST_ROOT/bin/cargo" \
    MEMORYBREAD_BUILD_CACHE_LIMIT_KB=1 \
    "$PROJECT_ROOT/scripts/maintain-build-cache.sh" --auto)

if [ -d "$TEST_ROOT/project/core-engine/target" ] || [ -d "$TEST_ROOT/project/desktop-ui/src-tauri/target" ]; then
    echo "oversized build caches were not removed" >&2
    exit 1
fi
if [[ "$output" != *"构建缓存回收完成"* ]]; then
    echo "cleanup result was not reported" >&2
    exit 1
fi

echo "build cache maintenance checks passed"
