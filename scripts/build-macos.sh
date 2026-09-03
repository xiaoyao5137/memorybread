#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DESKTOP_DIR="$PROJECT_ROOT/desktop-ui"
TAURI_DIR="$DESKTOP_DIR/src-tauri"
SIDECAR_DIR="$PROJECT_ROOT/ai-sidecar"
CORE_MANIFEST="$PROJECT_ROOT/core-engine/Cargo.toml"
PACKAGE_ROOT="$TAURI_DIR/target/macos-package"
STAGING_DIR="$TAURI_DIR/binaries"
MODE="${1:-dmg}"
DMG_ICON_TEMP_ROOT=""
DMG_STAGE_ROOT=""
DMG_TEMP_PATH=""
DMG_CUSTOMIZATION_MOUNT=""
DMG_CUSTOMIZATION_DEVICE=""

export PATH="${CARGO_HOME:-$HOME/.cargo}/bin:$PATH"
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-12.0}"
# 禁止 bsdtar/ditto 等工具把扩展属性写成 AppleDouble（._）文件；
# 更新包里混入 ._ 条目会导致客户端 updater 解包失败（failed to unpack `._xxx.app`）。
export COPYFILE_DISABLE=1

fail() {
  echo "[macOS build] $*" >&2
  exit 1
}

cleanup() {
  if [ -n "$DMG_CUSTOMIZATION_DEVICE" ]; then
    hdiutil detach "$DMG_CUSTOMIZATION_DEVICE" >/dev/null 2>&1 || true
  fi
  if [ -n "$DMG_CUSTOMIZATION_MOUNT" ] && [ -d "$DMG_CUSTOMIZATION_MOUNT" ]; then
    rmdir "$DMG_CUSTOMIZATION_MOUNT" >/dev/null 2>&1 || true
  fi
  if [ -n "$DMG_TEMP_PATH" ] && [ -f "$DMG_TEMP_PATH" ]; then
    rm -f "$DMG_TEMP_PATH"
  fi
  if [ -n "$DMG_ICON_TEMP_ROOT" ] && [ -d "$DMG_ICON_TEMP_ROOT" ]; then
    find "$DMG_ICON_TEMP_ROOT" -depth -mindepth 1 -delete >/dev/null 2>&1 || true
    rmdir "$DMG_ICON_TEMP_ROOT" >/dev/null 2>&1 || true
  fi
  if [ -n "$DMG_STAGE_ROOT" ] && [ -d "$DMG_STAGE_ROOT" ]; then
    find "$DMG_STAGE_ROOT" -depth -mindepth 1 -delete >/dev/null 2>&1 || true
    rmdir "$DMG_STAGE_ROOT" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少命令: $1"
}

require_env() {
  [ -n "${!1:-}" ] || fail "App Store 构建缺少环境变量: $1"
}

host_target() {
  case "$(uname -m)" in
    arm64) echo "aarch64-apple-darwin" ;;
    x86_64) echo "x86_64-apple-darwin" ;;
    *) fail "不支持的 Mac 架构: $(uname -m)" ;;
  esac
}

locate_app_bundle() {
  local candidate
  for candidate in \
    "$TAURI_DIR/target/$TARGET/release/bundle/macos/记忆面包.app" \
    "$TAURI_DIR/target/release/bundle/macos/记忆面包.app"; do
    if [ -d "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  find "$TAURI_DIR/target" -path '*/release/bundle/macos/记忆面包.app' -type d -print -quit
}

locate_dmg() {
  local bundle_dir
  local candidate
  for bundle_dir in \
    "$TAURI_DIR/target/$TARGET/release/bundle/dmg" \
    "$TAURI_DIR/target/release/bundle/dmg"; do
    [ -d "$bundle_dir" ] || continue
    candidate="$(find "$bundle_dir" -maxdepth 1 -type f -name "*_${APP_VERSION}_*.dmg" -print -quit)"
    if [ -n "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
}

locate_updater_bundle() {
  local candidate
  for candidate in \
    "$TAURI_DIR/target/$TARGET/release/bundle/macos/记忆面包.app.tar.gz" \
    "$TAURI_DIR/target/release/bundle/macos/记忆面包.app.tar.gz"; do
    if [ -f "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  find "$TAURI_DIR/target/$TARGET" -path '*/release/bundle/macos/*.app.tar.gz' -type f -print -quit
}

finalize_updater_bundle() {
  # Tauri 生成更新包发生在恢复 PyInstaller 符号链接之前，且手动重打包时
  # macOS bsdtar 会为每个带扩展属性的文件追加 AppleDouble（._）条目；
  # 客户端 updater 对顶层 ._ 条目解包会直接失败。
  # 因此基于最终 .app 重新打包并重签，并用门禁拒绝含 ._ 条目的归档。
  local updater_path bundle_dir app_name updater_password
  updater_path="$(locate_updater_bundle)"
  [ -n "$updater_path" ] && [ -f "$updater_path" ] || fail "未找到 Tauri 更新包，无法重新打包"
  bundle_dir="$(dirname "$updater_path")"
  app_name="$(basename "$APP_PATH")"
  [ -d "$bundle_dir/$app_name" ] || fail "更新包目录中未找到 $app_name"

  echo "[macOS build] 基于最终 .app 重新打包更新产物..."
  find "$APP_PATH" -name '._*' -type f -delete
  rm -f "$updater_path" "$updater_path.sig"
  COPYFILE_DISABLE=1 tar -czf "$updater_path" -C "$bundle_dir" "$app_name"

  # 门禁：归档内不得存在任何 ._ AppleDouble 条目（bsdtar 列表会隐藏它们，用 python 核对）
  python3 - "$updater_path" <<'PY' || fail "更新归档包含 ._ AppleDouble 条目，禁止发布"
import os, sys, tarfile
names = tarfile.open(sys.argv[1]).getnames()
bad = [n for n in names if os.path.basename(n.rstrip('/')).startswith('._')]
if bad:
    print('AppleDouble entries:', bad[:3], file=sys.stderr)
    sys.exit(1)
PY

  updater_password="${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:-}"
  if [ -z "$updater_password" ]; then
    updater_password="$(security find-generic-password -s com.memory-bread.release.updater -w 2>/dev/null || true)"
  fi
  [ -n "$updater_password" ] \
    || fail "缺少更新签名密码：请设置 TAURI_SIGNING_PRIVATE_KEY_PASSWORD 或钥匙串条目 com.memory-bread.release.updater"
  (
    cd "$DESKTOP_DIR"
    # env -u 屏蔽 TAURI_SIGNING_PRIVATE_KEY_PATH：tauri CLI 会同时读取该环境变量，
    # 与显式 -k 参数冲突导致签名失败。
    env -u TAURI_SIGNING_PRIVATE_KEY_PATH TAURI_SIGNING_PRIVATE_KEY_PASSWORD="$updater_password" \
      npx tauri signer sign -k "$TAURI_SIGNING_PRIVATE_KEY" "$updater_path" >/dev/null
  )
  [ -f "$updater_path.sig" ] || fail "更新包重签名失败：未生成 .sig"
  echo "[macOS build] 更新产物已基于最终 .app 重新打包并重签（无 ._ 条目）"
}

apply_dmg_file_icon() {
  local dmg_path="$1"
  local temp_root
  local iconset_path
  local icon_png
  local icon_resource

  temp_root="$(mktemp -d "${TMPDIR:-/tmp}/memory-bread-dmg-icon.XXXXXX")"
  DMG_ICON_TEMP_ROOT="$temp_root"
  iconset_path="$temp_root/memorybread.iconset"
  icon_png="$temp_root/memorybread.png"
  icon_resource="$temp_root/memorybread.rsrc"

  # Tauri 会设置 .app 和挂载卷图标，但不会设置 Finder 里的外层 .dmg 文件图标。
  iconutil --convert iconset --output "$iconset_path" "$TAURI_DIR/icons/icon.icns"
  cp "$iconset_path/icon_512x512@2x.png" "$icon_png"
  sips -i "$icon_png" >/dev/null
  DeRez -only icns "$icon_png" > "$icon_resource"
  Rez -a "$icon_resource" -o "$dmg_path"
  SetFile -a C "$dmg_path"

  find "$temp_root" -depth -mindepth 1 -delete
  rmdir "$temp_root"
  DMG_ICON_TEMP_ROOT=""
}

restore_ai_helper_symlinks() {
  local app_path="$1"
  local bundled_helper="$app_path/Contents/Helpers/memory-bread-ai.app"
  local staged_helper="$STAGING_DIR/memory-bread-ai.app"
  local staged_link_count
  local bundled_link_count

  [ -d "$bundled_helper" ] || fail "App Bundle 中未找到 AI helper: $bundled_helper"
  [ -d "$staged_helper" ] || fail "暂存目录中未找到 AI helper: $staged_helper"
  staged_link_count="$(find "$staged_helper" -type l | wc -l | xargs)"
  [ "$staged_link_count" -gt 0 ] || fail "暂存的 AI helper 中未找到 PyInstaller 符号链接"

  # Tauri 的 resources 目标位于 Contents/Helpers。重新复制 PyInstaller 原始
  # .app，避免资源打包阶段把其框架/动态库符号链接展开成普通文件。
  rm -rf "$bundled_helper"
  ditto "$staged_helper" "$bundled_helper"
  bundled_link_count="$(find "$bundled_helper" -type l | wc -l | xargs)"
  [ "$bundled_link_count" -eq "$staged_link_count" ] \
    || fail "AI helper 符号链接恢复不完整：期望 ${staged_link_count}，实际 ${bundled_link_count}"
  echo "[macOS build] AI helper 符号链接已恢复：${bundled_link_count} 个"
}

create_dmg_with_volume_icon() {
  local dmg_path="$1"
  local attach_output

  DMG_TEMP_PATH="$(mktemp "${TMPDIR:-/tmp}/memory-bread-dmg-rw.XXXXXX.dmg")"
  rm "$DMG_TEMP_PATH"
  DMG_CUSTOMIZATION_MOUNT="$(mktemp -d "${TMPDIR:-/tmp}/memory-bread-dmg-customize.XXXXXX")"

  cp "$TAURI_DIR/icons/icon.icns" "$DMG_STAGE_ROOT/.VolumeIcon.icns"
  hdiutil create -volname "记忆面包" -srcfolder "$DMG_STAGE_ROOT" -ov -format UDRW "$DMG_TEMP_PATH" >/dev/null
  attach_output="$(hdiutil attach -readwrite -nobrowse -mountpoint "$DMG_CUSTOMIZATION_MOUNT" "$DMG_TEMP_PATH")"
  DMG_CUSTOMIZATION_DEVICE="$(printf '%s\n' "$attach_output" | awk '$1 ~ /^\/dev\/disk[0-9]+$/ {print $1; exit}')"
  [ -n "$DMG_CUSTOMIZATION_DEVICE" ] || fail "无法确定可写 DMG 的挂载设备"

  # 卷根目录的 FinderInfo 必须在已挂载的可写卷内设置；在 srcfolder 上设置
  # 会在 hdiutil create 时丢失，导致发布校验看不到自定义图标标记。
  SetFile -a C "$DMG_CUSTOMIZATION_MOUNT"
  sync
  hdiutil detach "$DMG_CUSTOMIZATION_DEVICE" >/dev/null
  DMG_CUSTOMIZATION_DEVICE=""
  rmdir "$DMG_CUSTOMIZATION_MOUNT"
  DMG_CUSTOMIZATION_MOUNT=""

  rm -f "$dmg_path"
  hdiutil convert "$DMG_TEMP_PATH" -format UDZO -o "$dmg_path" >/dev/null
  rm "$DMG_TEMP_PATH"
  DMG_TEMP_PATH=""
}

prepare_python_helper() {
  local python_bin="${MEMORY_BREAD_PYTHON_BIN:-$SIDECAR_DIR/.venv/bin/python}"
  [ -x "$python_bin" ] || fail "缺少 Python 发布环境: $python_bin"
  "$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 9) else 1)' \
    || fail "macOS 发布构建必须使用 Python 3.9，当前为 $($python_bin --version 2>&1)"

  # 确保运行时依赖已安装（PyInstaller 会打包当前环境的所有依赖）
  echo "[macOS build] 检查运行时依赖..."
  if ! "$python_bin" -c 'import requests' >/dev/null 2>&1; then
    echo "[macOS build] 安装运行时依赖（requirements.txt）..."
    "$python_bin" -m pip install -r "$SIDECAR_DIR/requirements.txt"
  fi

  if ! "$python_bin" -c 'import PyInstaller' >/dev/null 2>&1; then
    echo "[macOS build] 安装 PyInstaller 构建依赖..."
    "$python_bin" -m pip install -r "$SIDECAR_DIR/requirements-build.txt"
  fi

  local python_arch
  python_arch="$($python_bin -c 'import platform; print(platform.machine())')"
  case "$TARGET" in
    aarch64-apple-darwin)
      [ "$python_arch" = "arm64" ] || fail "当前 Python 架构为 ${python_arch}，不能生成 arm64 helper"
      PYINSTALLER_ARCH="arm64"
      ;;
    x86_64-apple-darwin)
      [ "$python_arch" = "x86_64" ] || fail "当前 Python 架构为 ${python_arch}，不能生成 x86_64 helper"
      PYINSTALLER_ARCH="x86_64"
      ;;
    *) fail "Python sidecar 暂不支持目标: $TARGET" ;;
  esac

  # Xcode 17 的 lipo 不再允许输入和输出使用同一路径，而 PyInstaller 6.21
  # 会尝试原地裁剪 universal2 bootloader。发布环境按目标架构隔离，因此先在
  # 虚拟环境里安全地裁成当前目标，避免 PyInstaller 在组装 EXE 时失败。
  local pyinstaller_bootloader_dir
  local pyinstaller_bootloader
  local thinned_bootloader
  pyinstaller_bootloader_dir="$($python_bin -c 'import os, PyInstaller; print(os.path.join(os.path.dirname(PyInstaller.__file__), "bootloader", "Darwin-64bit"))')"
  for pyinstaller_bootloader in "$pyinstaller_bootloader_dir/run" "$pyinstaller_bootloader_dir/runw"; do
    [ -f "$pyinstaller_bootloader" ] || continue
    if [ "$(lipo -archs "$pyinstaller_bootloader")" = "x86_64 arm64" ]; then
      thinned_bootloader="$(mktemp "${pyinstaller_bootloader}.${PYINSTALLER_ARCH}.XXXXXX")"
      lipo -thin "$PYINSTALLER_ARCH" "$pyinstaller_bootloader" -output "$thinned_bootloader"
      chmod +x "$thinned_bootloader"
      mv "$thinned_bootloader" "$pyinstaller_bootloader"
    fi
  done

  local build_root="$PACKAGE_ROOT/pyinstaller/$TARGET"
  local dist_dir="$build_root/dist"
  local work_dir="$build_root/work"
  local spec_dir="$build_root/spec"
  local frozen_app="$dist_dir/memory-bread-ai.app"
  local frozen_executable="$frozen_app/Contents/MacOS/memory-bread-ai"
  local hidden_args=()
  local signing_args=()
  local signing_identity=""
  local package
  local module

  if [ "$MODE" = "dmg" ]; then
    if [ -n "${APPLE_SIGNING_IDENTITY:-}" ] && [ "$APPLE_SIGNING_IDENTITY" != "-" ]; then
      signing_identity="$APPLE_SIGNING_IDENTITY"
      signing_args+=(--codesign-identity "$signing_identity")
    fi
  else
    signing_identity="$APPLE_APP_SIGNING_IDENTITY"
    signing_args+=(
      --codesign-identity "$signing_identity"
      --osx-entitlements-file "$TAURI_DIR/Entitlements.child.plist"
    )
  fi

  while IFS= read -r module; do
    hidden_args+=(--hidden-import "$module")
  done < <(
    find "$SIDECAR_DIR" -maxdepth 1 -type f -name '*.py' -print \
      | sed -E 's#^.*/##; s#\.py$##' \
      | sort
  )

  for package in asr creation embedding idle_compute image_generation knowledge monitor ocr rag vlm; do
    hidden_args+=(--collect-submodules "$package")
  done
  hidden_args+=(--collect-submodules memory_bread_ipc)

  # Explicitly include rag.llm submodules (PyInstaller sometimes misses them)
  for module in rag.llm rag.llm.base rag.llm.ollama rag.llm.openai_compat rag.llm.cloud; do
    hidden_args+=(--hidden-import "$module")
  done

  # Explicitly include third-party runtime dependencies that use lazy imports
  # (PyInstaller's static analysis can't detect imports inside functions)
  # Use --collect-all to ensure all submodules and data files are included
  for module in requests urllib3 certifi charset_normalizer idna; do
    hidden_args+=(--collect-all "$module")
    hidden_args+=(--copy-metadata "$module")
  done

  if [ "${MEMORY_BREAD_REUSE_PYINSTALLER:-0}" = "1" ] \
    && [ -x "$frozen_executable" ]; then
    echo "[macOS build] 复用已有 PyInstaller 产物（仅用于本地重试）..."
  else
    echo "[macOS build] 冻结 Python AI sidecar（${TARGET}）..."
    # macOS 自带 Bash 3.2 在 `set -u` 下展开空数组会报 unbound variable。
    # 本机 ad-hoc DMG 没有 signing_args，参数展开期间临时关闭 nounset。
    set +u
    "$python_bin" -m PyInstaller \
      --noconfirm \
      --clean \
      --onedir \
      --windowed \
      --name memory-bread-ai \
      --osx-bundle-identifier com.memory-bread.app.ai-helper \
      --target-arch "$PYINSTALLER_ARCH" \
      --distpath "$dist_dir" \
      --workpath "$work_dir" \
      --specpath "$spec_dir" \
      --paths "$SIDECAR_DIR" \
      --paths "$PROJECT_ROOT/shared/ipc-protocol/python" \
      --additional-hooks-dir "$SIDECAR_DIR/pyinstaller-hooks" \
      --add-data "$SIDECAR_DIR/migrations:migrations" \
      --add-data "$SIDECAR_DIR/Modelfile:." \
      "${signing_args[@]}" \
      "${hidden_args[@]}" \
      "$SIDECAR_DIR/packaged_entry.py"
    set -u
  fi

  [ -d "$frozen_app" ] || fail "PyInstaller 未生成 memory-bread-ai.app"
  [ -x "$frozen_executable" ] || fail "PyInstaller helper 缺少主程序"
  "$frozen_executable" --help >/dev/null

  if [ -n "$signing_identity" ]; then
    local signed_file
    local signature_info
    local signed_macho_count=0

    codesign --verify --deep --strict "$frozen_app" \
      || fail "PyInstaller helper 的签名结构无效"
    while IFS= read -r -d '' signed_file; do
      if ! file "$signed_file" | grep -q 'Mach-O'; then
        continue
      fi
      signed_macho_count=$((signed_macho_count + 1))
      signature_info="$(codesign -d --verbose=4 "$signed_file" 2>&1)" \
        || fail "无法读取 helper 内部签名: $signed_file"
      printf '%s\n' "$signature_info" | grep -Fq "Authority=$signing_identity" \
        || fail "helper 内部文件未使用预期证书签名: $signed_file"
      printf '%s\n' "$signature_info" | grep -q '^Timestamp=' \
        || fail "helper 内部文件缺少安全时间戳: $signed_file"
      printf '%s\n' "$signature_info" | grep -Eq 'flags=.*\(.*runtime.*\)|^Runtime Version=' \
        || fail "helper 内部文件未启用 Hardened Runtime: $signed_file"
    done < <(find "$frozen_app/Contents" -type f -print0)
    [ "$signed_macho_count" -gt 0 ] || fail "PyInstaller helper 内未找到可校验的 Mach-O 文件"
    echo "[macOS build] Python AI sidecar 的 Developer ID、时间戳与 Hardened Runtime 校验通过"
  fi

  rm -rf "$STAGING_DIR/memory-bread-ai.app"
  # 使用 ditto 保留符号链接（PyInstaller 使用符号链接优化库布局）
  ditto "$frozen_app" "$STAGING_DIR/memory-bread-ai.app"
}

prepare_core_helper() {
  echo "[macOS build] 构建 Rust core-engine（${TARGET}）..."
  rustup target add "$TARGET" >/dev/null
  cargo build --release --target "$TARGET" --manifest-path "$CORE_MANIFEST"
  local core_binary="$PROJECT_ROOT/core-engine/target/$TARGET/release/memory-bread"
  local browser_bridge_binary="$PROJECT_ROOT/core-engine/target/$TARGET/release/memorybread-browser-bridge"
  [ -x "$core_binary" ] || fail "未生成 core-engine: $core_binary"
  [ -x "$browser_bridge_binary" ] || fail "未生成 Chrome browser bridge: $browser_bridge_binary"
  cp "$core_binary" "$STAGING_DIR/memory-bread-core-$TARGET"
  cp "$browser_bridge_binary" "$STAGING_DIR/memorybread-browser-bridge-$TARGET"
  chmod +x "$STAGING_DIR/memory-bread-core-$TARGET"
  chmod +x "$STAGING_DIR/memorybread-browser-bridge-$TARGET"
}

verify_staged_helpers() {
  local helper
  for helper in \
    "$STAGING_DIR/memory-bread-core-$TARGET" \
    "$STAGING_DIR/memorybread-browser-bridge-$TARGET" \
    "$STAGING_DIR/memory-bread-ai.app/Contents/MacOS/memory-bread-ai"; do
    file "$helper" | grep -q 'Mach-O' || fail "helper 不是 Mach-O: $helper"
    file "$helper" | grep -q "${TARGET%%-*}\|$(uname -m)" || fail "helper 架构与 ${TARGET} 不匹配: $helper"
  done
}

generate_appstore_entitlements() {
  sed "s/__TEAM_ID__/$APPLE_TEAM_ID/g" \
    "$TAURI_DIR/Entitlements.appstore.plist.in" \
    > "$TAURI_DIR/Entitlements.appstore.generated.plist"
  plutil -lint "$TAURI_DIR/Entitlements.appstore.generated.plist" >/dev/null
}

validate_provisioning_profile() {
  [ -f "$APPLE_PROVISIONING_PROFILE" ] || fail "找不到 provisioning profile: $APPLE_PROVISIONING_PROFILE"
  cp "$APPLE_PROVISIONING_PROFILE" "$TAURI_DIR/embedded.provisionprofile"

  local decoded_profile
  decoded_profile="$(mktemp "${TMPDIR:-/tmp}/memory-bread-profile.XXXXXX.plist")"
  trap 'rm -f "$decoded_profile"' RETURN
  security cms -D -i "$TAURI_DIR/embedded.provisionprofile" > "$decoded_profile"
  local profile_app_id
  local profile_team_id
  profile_app_id="$(/usr/libexec/PlistBuddy -c 'Print :Entitlements:application-identifier' "$decoded_profile")"
  profile_team_id="$(/usr/libexec/PlistBuddy -c 'Print :TeamIdentifier:0' "$decoded_profile")"
  [ "$profile_app_id" = "${APPLE_TEAM_ID}.com.memory-bread.app" ] \
    || fail "profile App ID 不匹配，期望 ${APPLE_TEAM_ID}.com.memory-bread.app，实际 $profile_app_id"
  [ "$profile_team_id" = "$APPLE_TEAM_ID" ] \
    || fail "profile Team ID 不匹配，期望 ${APPLE_TEAM_ID}，实际 $profile_team_id"
  rm -f "$decoded_profile"
  trap - RETURN
}

sign_appstore_bundle() {
  local app_path="$1"
  local ai_helper="$app_path/Contents/Helpers/memory-bread-ai.app"

  codesign --force --deep --options runtime --timestamp \
    --entitlements "$TAURI_DIR/Entitlements.child.plist" \
    --sign "$APPLE_APP_SIGNING_IDENTITY" "$ai_helper"
  codesign --force --options runtime --timestamp \
    --entitlements "$TAURI_DIR/Entitlements.child.plist" \
    --sign "$APPLE_APP_SIGNING_IDENTITY" \
    "$app_path/Contents/MacOS/memory-bread-core"

  codesign --force --options runtime --timestamp \
    --entitlements "$TAURI_DIR/Entitlements.appstore.generated.plist" \
    --sign "$APPLE_APP_SIGNING_IDENTITY" "$app_path"
}

[ "$(uname -s)" = "Darwin" ] || fail "macOS 包只能在 Mac 上构建"
case "$MODE" in
  dmg|appstore) ;;
  *) fail "用法: $0 {dmg|appstore}" ;;
esac

require_command cargo
require_command rustup
require_command npm
require_command node
require_command file
require_command plutil
require_command codesign
require_command hdiutil
require_command iconutil
require_command sips
require_command DeRez
require_command Rez
require_command SetFile

export MEMORY_BREAD_BUILD_NUMBER="$(node -p "require('$TAURI_DIR/tauri.conf.json').bundle.macOS.bundleVersion")"
[[ "$MEMORY_BREAD_BUILD_NUMBER" =~ ^[0-9]+$ ]] && [ "$MEMORY_BREAD_BUILD_NUMBER" -gt 0 ] \
  || fail "tauri.conf.json 的 bundleVersion 必须是大于 0 的整数"
APP_VERSION="$(node -p "require('$TAURI_DIR/tauri.conf.json').version")"

TARGET="${MEMORY_BREAD_MACOS_TARGET:-$(host_target)}"
[ "$TARGET" = "$(host_target)" ] \
  || fail "当前 Python sidecar 构建只支持宿主架构 $(host_target)，收到 ${TARGET}"
mkdir -p "$PACKAGE_ROOT" "$STAGING_DIR"

if [ "$MODE" = "appstore" ]; then
  require_env APPLE_TEAM_ID
  require_env APPLE_PROVISIONING_PROFILE
  require_env APPLE_APP_SIGNING_IDENTITY
  require_env APPLE_INSTALLER_SIGNING_IDENTITY
  [[ "$APPLE_TEAM_ID" =~ ^[A-Z0-9]{10}$ ]] \
    || fail "APPLE_TEAM_ID 格式应为 10 位大写字母或数字"

  require_command xcodebuild
  require_command productbuild
  require_command pkgutil
  require_command security
  if ! xcodebuild -version >/dev/null 2>&1; then
    fail "App Store 构建需要完整 Xcode；当前 xcode-select 指向的只是 Command Line Tools"
  fi
  security find-identity -v -p codesigning | grep -Fq "$APPLE_APP_SIGNING_IDENTITY" \
    || fail "钥匙串中找不到 App 签名证书: $APPLE_APP_SIGNING_IDENTITY"
  security find-identity -v | grep -Fq "$APPLE_INSTALLER_SIGNING_IDENTITY" \
    || fail "钥匙串中找不到 Installer 签名证书: $APPLE_INSTALLER_SIGNING_IDENTITY"
fi

prepare_core_helper
prepare_python_helper
verify_staged_helpers

if [ "$MODE" = "dmg" ]; then
  TAURI_CONFIG_ARGS=(--config src-tauri/tauri.direct.conf.json)
  if [ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ] && [ -n "${TAURI_SIGNING_PRIVATE_KEY_PATH:-}" ]; then
    [ -f "$TAURI_SIGNING_PRIVATE_KEY_PATH" ] \
      || fail "找不到 Tauri 更新签名私钥: $TAURI_SIGNING_PRIVATE_KEY_PATH"
    export TAURI_SIGNING_PRIVATE_KEY="$(< "$TAURI_SIGNING_PRIVATE_KEY_PATH")"
  fi
  UPDATER_PRIVATE_KEY_CONFIGURED=0
  if [ -n "${TAURI_SIGNING_PRIVATE_KEY:-}" ] || [ -n "${TAURI_SIGNING_PRIVATE_KEY_PATH:-}" ]; then
    UPDATER_PRIVATE_KEY_CONFIGURED=1
  fi
  if [ "$UPDATER_PRIVATE_KEY_CONFIGURED" -eq 1 ] && [ -z "${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:-}" ]; then
    TAURI_SIGNING_PRIVATE_KEY_PASSWORD="$(security find-generic-password -s com.memory-bread.release.updater -w 2>/dev/null || true)"
    export TAURI_SIGNING_PRIVATE_KEY_PASSWORD
  fi
  if [ "$UPDATER_PRIVATE_KEY_CONFIGURED" -eq 1 ] && [ -z "${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:-}" ]; then
    fail "缺少更新签名密码：请设置 TAURI_SIGNING_PRIVATE_KEY_PASSWORD 或钥匙串条目 com.memory-bread.release.updater"
  fi
  if [ "$UPDATER_PRIVATE_KEY_CONFIGURED" -eq 0 ] && [ -z "${MEMORY_BREAD_UPDATER_PUBLIC_KEY:-}" ]; then
    TAURI_CONFIG_ARGS+=(--config '{"bundle":{"createUpdaterArtifacts":false}}')
    echo "[macOS build] 未配置更新签名密钥；本次仅生成本机测试 DMG，不生成热更新包"
  elif [ "$UPDATER_PRIVATE_KEY_CONFIGURED" -eq 0 ] || [ -z "${MEMORY_BREAD_UPDATER_PUBLIC_KEY:-}" ]; then
    fail "生成热更新包必须同时设置 TAURI_SIGNING_PRIVATE_KEY（或 TAURI_SIGNING_PRIVATE_KEY_PATH）与 MEMORY_BREAD_UPDATER_PUBLIC_KEY"
  else
    UPDATER_PLUGIN_CONFIG="$(node -e 'const pubkey = process.env.MEMORY_BREAD_UPDATER_PUBLIC_KEY; process.stdout.write(JSON.stringify({ plugins: { updater: { pubkey } } }))')"
    TAURI_CONFIG_ARGS+=(--config "$UPDATER_PLUGIN_CONFIG")
  fi
  if [ -z "${APPLE_SIGNING_IDENTITY:-}" ]; then
    export APPLE_SIGNING_IDENTITY="-"
    echo "[macOS build] 未提供 Developer ID，当前 DMG 使用 ad-hoc 签名，仅供本机测试"
  fi
  echo "[macOS build] 构建可站外分发的 App + DMG..."
  (
    cd "$DESKTOP_DIR"
    npm run tauri -- build \
      --bundles app,dmg \
      --target "$TARGET" \
      "${TAURI_CONFIG_ARGS[@]}"
  )

  # Tauri 打包会将 resources 中的符号链接解析为实际文件
  # 需要从原始 PyInstaller 输出恢复符号链接，否则 PyInstaller 运行时会失败
  APP_PATH="$(locate_app_bundle)"
  [ -d "$APP_PATH" ] || fail "未找到生成的 .app"

  echo "[macOS build] 恢复 PyInstaller helper 的符号链接..."
  restore_ai_helper_symlinks "$APP_PATH"

  APP_PATH="$(locate_app_bundle)"
  DMG_PATH="$(locate_dmg)"
  [ -d "$APP_PATH" ] || fail "未找到生成的 .app"
  [ -f "$DMG_PATH" ] || fail "未找到生成的 .dmg"

  # 由于符号链接已恢复，需要重新创建 DMG（Tauri 创建的 DMG 不包含符号链接）。
  # 重建时必须保留 Applications 快捷方式，否则卷里只有 .app，用户会直接双击
  # 在只读挂载卷里运行，导致应用没有安装进 /Applications、启动台看不到图标。
  echo "[macOS build] 重新创建 DMG（包含符号链接）..."
  rm "$DMG_PATH"
  DMG_STAGE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/memory-bread-dmg-stage.XXXXXX")"
  ditto "$APP_PATH" "$DMG_STAGE_ROOT/$(basename "$APP_PATH")"
  ln -s /Applications "$DMG_STAGE_ROOT/Applications"
  create_dmg_with_volume_icon "$DMG_PATH"
  find "$DMG_STAGE_ROOT" -depth -mindepth 1 -delete
  rmdir "$DMG_STAGE_ROOT"
  DMG_STAGE_ROOT=""
  echo "[macOS build] DMG 已重新创建"

  # 重建后的 DMG 丢失了 Tauri 原产物的签名；公证前先补签，
  # 否则 spctl 会报 "no usable signature"（签名必须在公证前，公证后补签会使 ticket 失效）。
  if [ "${APPLE_SIGNING_IDENTITY:-}" != "-" ] && [ -n "${APPLE_SIGNING_IDENTITY:-}" ]; then
    codesign --force --sign "$APPLE_SIGNING_IDENTITY" --timestamp "$DMG_PATH"
  fi

  apply_dmg_file_icon "$DMG_PATH"
  "$SCRIPT_DIR/verify-macos-bundle.sh" "$APP_PATH" dmg "$DMG_PATH"
  echo "[macOS build] App: $APP_PATH"
  echo "[macOS build] DMG: $DMG_PATH"
  if [ "$UPDATER_PRIVATE_KEY_CONFIGURED" -eq 1 ]; then
    finalize_updater_bundle
    UPDATER_PATH="$(locate_updater_bundle)"
    [ -f "$UPDATER_PATH" ] || fail "未找到 Tauri 更新包"
    [ -f "$UPDATER_PATH.sig" ] || fail "未找到 Tauri 更新签名"
    echo "[macOS build] Updater: $UPDATER_PATH"
    echo "[macOS build] Updater SHA-256: $(shasum -a 256 "$UPDATER_PATH" | awk '{print $1}')"
    echo "[macOS build] Updater signature: $UPDATER_PATH.sig"
  fi
  exit 0
fi

generate_appstore_entitlements
validate_provisioning_profile
export APPLE_SIGNING_IDENTITY="$APPLE_APP_SIGNING_IDENTITY"

echo "[macOS build] 构建 Mac App Store App Bundle..."
(
  cd "$DESKTOP_DIR"
  npm run tauri -- build \
    --bundles app \
    --target "$TARGET" \
    --features app-store \
    --config src-tauri/tauri.appstore.conf.json
)

# 恢复 PyInstaller 符号链接（同 DMG 构建）
APP_PATH="$(locate_app_bundle)"
[ -d "$APP_PATH" ] || fail "未找到生成的 App Store .app"

echo "[macOS build] 恢复 PyInstaller helper 的符号链接..."
restore_ai_helper_symlinks "$APP_PATH"

APP_PATH="$(locate_app_bundle)"
[ -d "$APP_PATH" ] || fail "未找到生成的 App Store .app"
sign_appstore_bundle "$APP_PATH"
"$SCRIPT_DIR/verify-macos-bundle.sh" "$APP_PATH" appstore

VERSION="$(node -p "require('$TAURI_DIR/tauri.conf.json').version")"
PKG_DIR="$PACKAGE_ROOT/appstore"
PKG_PATH="$PKG_DIR/记忆面包_${VERSION}_${TARGET%%-*}.pkg"
mkdir -p "$PKG_DIR"
productbuild \
  --sign "$APPLE_INSTALLER_SIGNING_IDENTITY" \
  --component "$APP_PATH" /Applications \
  "$PKG_PATH"
pkgutil --check-signature "$PKG_PATH"

echo "[macOS build] App: $APP_PATH"
echo "[macOS build] App Store PKG: $PKG_PATH"
