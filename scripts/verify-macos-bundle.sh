#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_PATH="${1:-}"
MODE="${2:-dmg}"
DMG_PATH="${3:-}"
EXPECTED_ICON="$PROJECT_ROOT/desktop-ui/src-tauri/icons/icon.icns"
ICONSET_TEMP_ROOT=""
DMG_MOUNT_POINT=""
DMG_DEVICE=""

cleanup() {
  if [ -n "$ICONSET_TEMP_ROOT" ] && [ -d "$ICONSET_TEMP_ROOT" ]; then
    find "$ICONSET_TEMP_ROOT" -depth -mindepth 1 -delete
    rmdir "$ICONSET_TEMP_ROOT" >/dev/null 2>&1 || true
  fi
  if [ -n "$DMG_DEVICE" ]; then
    hdiutil detach "$DMG_DEVICE" >/dev/null 2>&1 || true
  fi
  if [ -n "$DMG_MOUNT_POINT" ] && [ -d "$DMG_MOUNT_POINT" ]; then
    rmdir "$DMG_MOUNT_POINT" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT

fail() {
  echo "[macOS verify] $*" >&2
  exit 1
}

[ -d "$APP_PATH" ] \
  || fail "用法: $0 /path/to/记忆面包.app [dmg|appstore] [/path/to/记忆面包.dmg]"
[ "$MODE" = "dmg" ] || [ "$MODE" = "appstore" ] \
  || fail "校验模式必须是 dmg 或 appstore"

INFO_PLIST="$APP_PATH/Contents/Info.plist"
MAIN_BIN="$APP_PATH/Contents/MacOS/memory-bread-desktop"
CORE_BIN="$APP_PATH/Contents/MacOS/memory-bread-core"
BROWSER_BRIDGE_BIN="$APP_PATH/Contents/MacOS/memorybread-browser-bridge"
AI_APP="$APP_PATH/Contents/Helpers/memory-bread-ai.app"
AI_BIN="$AI_APP/Contents/MacOS/memory-bread-ai"

for path in "$INFO_PLIST" "$MAIN_BIN" "$CORE_BIN" "$BROWSER_BRIDGE_BIN" "$AI_APP" "$AI_BIN"; do
  [ -e "$path" ] || fail "App Bundle 缺少: $path"
done

AI_LINK_COUNT="$(find "$AI_APP" -type l | wc -l | xargs)"
[ "$AI_LINK_COUNT" -gt 0 ] || fail "AI helper 中未找到 PyInstaller 符号链接"
while IFS= read -r link_path; do
  [ -e "$link_path" ] || fail "AI helper 包含失效符号链接: $link_path"
done < <(find "$AI_APP" -type l)

IDENTIFIER="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$INFO_PLIST")"
[ "$IDENTIFIER" = "com.memory-bread.app" ] || fail "Bundle ID 错误: $IDENTIFIER"
MINIMUM_SYSTEM="$(/usr/libexec/PlistBuddy -c 'Print :LSMinimumSystemVersion' "$INFO_PLIST")"
[ "${MINIMUM_SYSTEM%%.*}" -ge 12 ] || fail "最低系统版本必须不低于 macOS 12: $MINIMUM_SYSTEM"
ICON_FILE="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIconFile' "$INFO_PLIST")"
[ "$ICON_FILE" = "icon.icns" ] || fail "App Bundle 图标配置错误: ${ICON_FILE:-<empty>}"
[ -f "$EXPECTED_ICON" ] || fail "缺少品牌图标源文件: $EXPECTED_ICON"
BUNDLED_ICON="$APP_PATH/Contents/Resources/$ICON_FILE"
[ -f "$BUNDLED_ICON" ] || fail "App Bundle 缺少品牌图标: $BUNDLED_ICON"
cmp -s "$EXPECTED_ICON" "$BUNDLED_ICON" \
  || fail "App Bundle 中的图标不是当前记忆面包品牌图标"
ICONSET_TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/memory-bread-verify-icon.XXXXXX")"
ICONSET_PATH="$ICONSET_TEMP_ROOT/memorybread.iconset"
iconutil --convert iconset --output "$ICONSET_PATH" "$BUNDLED_ICON"
[ -f "$ICONSET_PATH/icon_512x512@2x.png" ] \
  || fail "品牌 icon.icns 缺少 1024x1024 表示"
find "$ICONSET_TEMP_ROOT" -depth -mindepth 1 -delete
rmdir "$ICONSET_TEMP_ROOT"
ICONSET_TEMP_ROOT=""

for binary in "$MAIN_BIN" "$CORE_BIN" "$BROWSER_BRIDGE_BIN" "$AI_BIN"; do
  file "$binary" | grep -q 'Mach-O' || fail "不是 Mach-O: $binary"
done

codesign --verify --deep --strict "$APP_PATH"

if [ "$MODE" = "dmg" ] && [ -n "$DMG_PATH" ]; then
  [ -f "$DMG_PATH" ] || fail "DMG 安装包不存在: $DMG_PATH"
  hdiutil verify "$DMG_PATH" >/dev/null

  DMG_ATTRIBUTES="$(GetFileInfo -a "$DMG_PATH")"
  [[ "$DMG_ATTRIBUTES" == *C* ]] \
    || fail "DMG 文件没有 Finder 自定义图标标记"
  xattr -p com.apple.ResourceFork "$DMG_PATH" >/dev/null \
    || fail "DMG 文件缺少品牌图标资源分叉"
  DeRez -only icns "$DMG_PATH" >/dev/null \
    || fail "DMG 文件的资源分叉中缺少 icns 图标"

  DMG_MOUNT_POINT="$(mktemp -d "${TMPDIR:-/tmp}/memory-bread-dmg-verify.XXXXXX")"
  ATTACH_OUTPUT="$(hdiutil attach -readonly -nobrowse -mountpoint "$DMG_MOUNT_POINT" "$DMG_PATH")"
  DMG_DEVICE="$(printf '%s\n' "$ATTACH_OUTPUT" | awk '$1 ~ /^\/dev\/disk[0-9]+$/ {print $1; exit}')"
  [ -n "$DMG_DEVICE" ] || fail "无法确定 DMG 挂载设备"

  VOLUME_ATTRIBUTES="$(GetFileInfo -a "$DMG_MOUNT_POINT")"
  [[ "$VOLUME_ATTRIBUTES" == *C* ]] \
    || fail "DMG 卷没有 Finder 自定义图标标记"
  [ -f "$DMG_MOUNT_POINT/.VolumeIcon.icns" ] \
    || fail "DMG 卷缺少 .VolumeIcon.icns"
  cmp -s "$EXPECTED_ICON" "$DMG_MOUNT_POINT/.VolumeIcon.icns" \
    || fail "DMG 卷图标不是当前记忆面包品牌图标"

  DMG_APP="$DMG_MOUNT_POINT/$(basename "$APP_PATH")"
  [ -d "$DMG_APP" ] || fail "DMG 内缺少 $(basename "$APP_PATH")"
  [ -L "$DMG_MOUNT_POINT/Applications" ] \
    || fail "DMG 内缺少 Applications 拖拽安装快捷方式"
  DMG_APP_ICON="$DMG_APP/Contents/Resources/$ICON_FILE"
  [ -f "$DMG_APP_ICON" ] || fail "DMG 内的 App 缺少品牌图标"
  cmp -s "$EXPECTED_ICON" "$DMG_APP_ICON" \
    || fail "DMG 内的 App 没有使用当前记忆面包品牌图标"

  hdiutil detach "$DMG_DEVICE" >/dev/null
  DMG_DEVICE=""
  rmdir "$DMG_MOUNT_POINT"
  DMG_MOUNT_POINT=""
fi

if [ "$MODE" = "appstore" ]; then
  [ -f "$APP_PATH/Contents/embedded.provisionprofile" ] \
    || fail "App Store 包缺少 embedded.provisionprofile"
  MAIN_ENTITLEMENTS="$(codesign -d --entitlements :- "$APP_PATH" 2>/dev/null)"
  CHILD_ENTITLEMENTS="$(codesign -d --entitlements :- "$CORE_BIN" 2>/dev/null)"
  AI_ENTITLEMENTS="$(codesign -d --entitlements :- "$AI_APP" 2>/dev/null)"
  printf '%s' "$MAIN_ENTITLEMENTS" | grep -q 'com.apple.security.app-sandbox' \
    || fail "主 App 缺少 App Sandbox entitlement"
  printf '%s' "$MAIN_ENTITLEMENTS" | grep -q 'com.apple.security.network.client' \
    || fail "主 App 缺少 network.client entitlement"
  printf '%s' "$MAIN_ENTITLEMENTS" | grep -q 'com.apple.security.network.server' \
    || fail "主 App 缺少 network.server entitlement"
  printf '%s' "$CHILD_ENTITLEMENTS" | grep -q 'com.apple.security.inherit' \
    || fail "helper 缺少 sandbox inherit entitlement"
  printf '%s' "$AI_ENTITLEMENTS" | grep -q 'com.apple.security.inherit' \
    || fail "AI helper 缺少 sandbox inherit entitlement"
fi

echo "[macOS verify] 通过: $APP_PATH ($MODE)"
