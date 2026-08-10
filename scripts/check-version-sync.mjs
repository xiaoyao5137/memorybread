import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

const scriptDir = path.dirname(fileURLToPath(import.meta.url))
const desktopDir = path.join(scriptDir, '..', 'desktop-ui')
const packageJson = JSON.parse(fs.readFileSync(path.join(desktopDir, 'package.json'), 'utf8'))
const packageLock = JSON.parse(fs.readFileSync(path.join(desktopDir, 'package-lock.json'), 'utf8'))
const tauriConfig = JSON.parse(fs.readFileSync(path.join(desktopDir, 'src-tauri', 'tauri.conf.json'), 'utf8'))
const cargoToml = fs.readFileSync(path.join(desktopDir, 'src-tauri', 'Cargo.toml'), 'utf8')
const cargoVersion = cargoToml.match(/^\[package\][\s\S]*?^version\s*=\s*"([^"]+)"/m)?.[1]
const buildNumber = tauriConfig.bundle?.macOS?.bundleVersion

const versions = {
  'desktop-ui/package.json': packageJson.version,
  'desktop-ui/package-lock.json': packageLock.version,
  'desktop-ui/package-lock.json packages[""]': packageLock.packages?.['']?.version,
  'desktop-ui/src-tauri/tauri.conf.json': tauriConfig.version,
  'desktop-ui/src-tauri/Cargo.toml': cargoVersion,
}
const uniqueVersions = new Set(Object.values(versions))

if (uniqueVersions.size !== 1 || uniqueVersions.has(undefined)) {
  console.error('记忆面包版本号不一致：')
  for (const [file, version] of Object.entries(versions)) {
    console.error(`- ${file}: ${version ?? '未找到'}`)
  }
  process.exit(1)
}

if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/.test(packageJson.version)) {
  console.error(`记忆面包版本号必须使用不含 build metadata 的 SemVer：${packageJson.version}`)
  process.exit(1)
}

if (!/^\d+$/.test(String(buildNumber)) || Number(buildNumber) < 1) {
  console.error(`macOS bundleVersion 必须是大于 0 的整数：${buildNumber ?? '未配置'}`)
  process.exit(1)
}

console.log(`记忆面包版本号已同步：${packageJson.version} (build ${buildNumber})`)
