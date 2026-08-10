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

export PATH="${CARGO_HOME:-$HOME/.cargo}/bin:$PATH"
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-12.0}"

fail() {
  echo "[macOS build] $*" >&2
  exit 1
}

cleanup() {
  if [ -n "$DMG_ICON_TEMP_ROOT" ] && [ -d "$DMG_ICON_TEMP_ROOT" ]; then
    find "$DMG_ICON_TEMP_ROOT" -depth -mindepth 1 -delete >/dev/null 2>&1 || true
    rmdir "$DMG_ICON_TEMP_ROOT" >/dev/null 2>&1 || true
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
  find "$TAURI_DIR/target" -path '*/release/bundle/dmg/*.dmg' -type f -print -quit
}

locate_updater_bundle() {
  find "$TAURI_DIR/target" -path '*/release/bundle/macos/*.app.tar.gz' -type f -print -quit
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

prepare_python_helper() {
  local python_bin="$SIDECAR_DIR/.venv/bin/python"
  [ -x "$python_bin" ] || fail "缺少 ai-sidecar/.venv；请先按项目说明安装 Python 运行依赖"

  if ! "$python_bin" -c 'import PyInstaller' >/dev/null 2>&1; then
    echo "[macOS build] 安装隔离的 PyInstaller 构建依赖..."
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

  local build_root="$PACKAGE_ROOT/pyinstaller/$TARGET"
  local dist_dir="$build_root/dist"
  local work_dir="$build_root/work"
  local spec_dir="$build_root/spec"
  local frozen_app="$dist_dir/memory-bread-ai.app"
  local frozen_executable="$frozen_app/Contents/MacOS/memory-bread-ai"
  local hidden_args=()
  local package
  local module

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

  if [ "${MEMORY_BREAD_REUSE_PYINSTALLER:-0}" = "1" ] \
    && [ -x "$frozen_executable" ]; then
    echo "[macOS build] 复用已有 PyInstaller 产物（仅用于本地重试）..."
  else
    echo "[macOS build] 冻结 Python AI sidecar（${TARGET}）..."
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
      "${hidden_args[@]}" \
      "$SIDECAR_DIR/packaged_entry.py"
  fi

  [ -d "$frozen_app" ] || fail "PyInstaller 未生成 memory-bread-ai.app"
  [ -x "$frozen_executable" ] || fail "PyInstaller helper 缺少主程序"
  "$frozen_executable" --help >/dev/null

  rm -rf "$STAGING_DIR/memory-bread-ai.app"
  cp -R "$frozen_app" "$STAGING_DIR/memory-bread-ai.app"
}

prepare_core_helper() {
  echo "[macOS build] 构建 Rust core-engine（${TARGET}）..."
  rustup target add "$TARGET" >/dev/null
  cargo build --release --target "$TARGET" --manifest-path "$CORE_MANIFEST"
  local core_binary="$PROJECT_ROOT/core-engine/target/$TARGET/release/memory-bread"
  [ -x "$core_binary" ] || fail "未生成 core-engine: $core_binary"
  cp "$core_binary" "$STAGING_DIR/memory-bread-core-$TARGET"
  chmod +x "$STAGING_DIR/memory-bread-core-$TARGET"
}

verify_staged_helpers() {
  local helper
  for helper in \
    "$STAGING_DIR/memory-bread-core-$TARGET" \
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
  APP_PATH="$(locate_app_bundle)"
  DMG_PATH="$(locate_dmg)"
  [ -d "$APP_PATH" ] || fail "未找到生成的 .app"
  [ -f "$DMG_PATH" ] || fail "未找到生成的 .dmg"
  apply_dmg_file_icon "$DMG_PATH"
  "$SCRIPT_DIR/verify-macos-bundle.sh" "$APP_PATH" dmg "$DMG_PATH"
  echo "[macOS build] App: $APP_PATH"
  echo "[macOS build] DMG: $DMG_PATH"
  if [ "$UPDATER_PRIVATE_KEY_CONFIGURED" -eq 1 ]; then
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
