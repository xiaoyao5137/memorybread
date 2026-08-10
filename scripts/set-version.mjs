import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

const scriptDir = path.dirname(fileURLToPath(import.meta.url))
const desktopDir = path.join(scriptDir, '..', 'desktop-ui')
const packagePath = path.join(desktopDir, 'package.json')
const packageLockPath = path.join(desktopDir, 'package-lock.json')
const tauriConfigPath = path.join(desktopDir, 'src-tauri', 'tauri.conf.json')
const cargoPath = path.join(desktopDir, 'src-tauri', 'Cargo.toml')
const [version, buildNumber] = process.argv.slice(2)

const semverPattern = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/
if (!semverPattern.test(version || '')) {
  console.error('用法：npm run version:set -- 1.4.2 42（版本必须为不含 build metadata 的 SemVer）')
  process.exit(1)
}
if (!/^\d+$/.test(buildNumber || '') || Number(buildNumber) < 1) {
  console.error('构建号必须是大于 0 的整数')
  process.exit(1)
}

const packageJson = JSON.parse(fs.readFileSync(packagePath, 'utf8'))
const packageLock = JSON.parse(fs.readFileSync(packageLockPath, 'utf8'))
const tauriConfig = JSON.parse(fs.readFileSync(tauriConfigPath, 'utf8'))
const currentBuildNumber = Number(tauriConfig.bundle?.macOS?.bundleVersion || 0)
if (Number(buildNumber) <= currentBuildNumber) {
  console.error(`新构建号必须大于当前构建号 ${currentBuildNumber}`)
  process.exit(1)
}

packageJson.version = version
packageLock.version = version
if (packageLock.packages?.['']) packageLock.packages[''].version = version
tauriConfig.version = version
tauriConfig.bundle ||= {}
tauriConfig.bundle.macOS ||= {}
tauriConfig.bundle.macOS.bundleVersion = String(buildNumber)

const cargoToml = fs.readFileSync(cargoPath, 'utf8')
const nextCargoToml = cargoToml.replace(
  /(^\[package\][\s\S]*?^version\s*=\s*")[^"]+(".*$)/m,
  `$1${version}$2`,
)
if (nextCargoToml === cargoToml) {
  console.error('未在 Cargo.toml 中找到 package.version')
  process.exit(1)
}

fs.writeFileSync(packagePath, `${JSON.stringify(packageJson, null, 2)}\n`)
fs.writeFileSync(packageLockPath, `${JSON.stringify(packageLock, null, 2)}\n`)
fs.writeFileSync(tauriConfigPath, `${JSON.stringify(tauriConfig, null, 2)}\n`)
fs.writeFileSync(cargoPath, nextCargoToml)
console.log(`版本已更新为 ${version} (build ${buildNumber})，请运行 npm run version:check`)
