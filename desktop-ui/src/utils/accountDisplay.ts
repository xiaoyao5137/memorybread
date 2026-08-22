import type { CloudSubscription, CloudUser } from '../types'

const firstNonEmpty = (...values: Array<string | null | undefined>): string | null => {
  for (const value of values) {
    const normalized = value?.trim()
    if (normalized) return normalized
  }
  return null
}

const RUN_MODE_LABELS: Record<string, string> = {
  std: '标准模式',
  std_monthly: '标准模式',
  std_annual: '标准模式',
  'std annual': '标准模式',
  silver: '标准模式',
  silver_monthly: '标准模式',
  silver_annual: '标准模式',
  plus: '增强模式',
  plus_monthly: '增强模式',
  plus_annual: '增强模式',
  'plus annual': '增强模式',
  gold: '增强模式',
  gold_monthly: '增强模式',
  gold_annual: '增强模式',
  pro: '专家模式',
  pro_monthly: '专家模式',
  pro_annual: '专家模式',
  'pro annual': '专家模式',
  platinum: '专家模式',
  platinum_monthly: '专家模式',
  platinum_annual: '专家模式',
  enterprise: '企业模式',
  enterprise_monthly: '企业模式',
  enterprise_annual: '企业模式',
  'enterprise annual': '企业模式',
  普通套餐: '标准模式',
  基础套餐: '标准模式',
  白银: '标准模式',
  白银套餐: '标准模式',
  白银年费套餐: '标准模式',
  黄金: '增强模式',
  黄金套餐: '增强模式',
  黄金年费套餐: '增强模式',
  白金: '专家模式',
  白金套餐: '专家模式',
  白金年费套餐: '专家模式',
  企业月费套餐: '企业模式',
  企业年费套餐: '企业模式',
}

const normalizeRunModeLabel = (value?: string | null): string | null => {
  const normalized = firstNonEmpty(value)
  if (!normalized) return null
  return RUN_MODE_LABELS[normalized] ?? RUN_MODE_LABELS[normalized.toLowerCase()] ?? normalized
}

export const getUserDisplayName = (
  user?: CloudUser | null,
  localNickname = '新鲜出炉的面包',
): string => {
  if (!user) return localNickname
  return (
    firstNonEmpty(
      user.nickname,
      user.display_name,
      user.name,
      user.username,
      user.email,
      user.phone,
    ) ||
    '已连接账户'
  )
}

export const getRunModeLabel = (
  user?: CloudUser | null,
  subscription?: CloudSubscription | null,
): string => {
  if (!user) return '本地模式'
  return (
    normalizeRunModeLabel(subscription?.plan_key) ||
    normalizeRunModeLabel(subscription?.name) ||
    normalizeRunModeLabel(user.membership_plan) ||
    normalizeRunModeLabel(user.plan_name) ||
    normalizeRunModeLabel(user.subscription_plan) ||
    '标准模式'
  )
}
