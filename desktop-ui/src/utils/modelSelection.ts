import type { CloudBalance } from '../types'
import type { CreationModelConfig } from '../store/useAppStore'

export const LOCAL_CREATION_MODEL_ID = 'mbcd-std-v1'
export const REMOTE_CREATION_MODEL_ID = 'mbcd-plus-v1'

export const CREATION_MODEL_DEFS = [
  {
    id: REMOTE_CREATION_MODEL_ID,
    name: 'MBCD Plus v1.0',
    shortName: 'Plus',
    description: '云端高质量创作与咨询',
    remote: true,
  },
  {
    id: LOCAL_CREATION_MODEL_ID,
    name: 'MBEM v1.0',
    shortName: 'MBEM',
    description: '本地理解、提炼、创作与咨询',
    remote: false,
  },
] as const

export type CreationModelId = typeof CREATION_MODEL_DEFS[number]['id']

export function decimalIsPositive(value?: string | number | null): boolean {
  if (value == null) return false
  const normalized = typeof value === 'number' ? value : Number(String(value).trim())
  return Number.isFinite(normalized) && normalized > 0
}

export function hasUsableCloudBalance(balance: CloudBalance | null): boolean {
  return decimalIsPositive(balance?.available)
}

export function canUseRemoteCreationModel(currentUser: unknown, balance: CloudBalance | null): boolean {
  return Boolean(currentUser) && hasUsableCloudBalance(balance)
}

export function getModelDisplayName(modelId?: string | null, configs: CreationModelConfig[] = []): string {
  if (!modelId) return 'MBEM v1.0'
  if (modelId.startsWith('user-')) return configs.find(c => c.id === modelId)?.name || '自定义模型'
  if (modelId === REMOTE_CREATION_MODEL_ID || modelId.includes('plus') || modelId.includes('opus')) {
    return 'MBCD Plus v1.0'
  }
  return 'MBEM v1.0'
}

export function getEffectiveCreationModelId(
  configs: CreationModelConfig[],
  remoteAllowed: boolean,
): string {
  const active = configs.find(config => config.enabled)?.id
  if (active?.startsWith('user-')) return active
  if (active === REMOTE_CREATION_MODEL_ID && remoteAllowed) return REMOTE_CREATION_MODEL_ID
  return LOCAL_CREATION_MODEL_ID
}
