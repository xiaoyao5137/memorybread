import { describe, expect, it } from 'vitest'
import { recoverableCreationHint, toCreationFailureMessage, toUserFacingError } from '../utils/userFacingError'

describe('toUserFacingError', () => {
  it('保留可操作的中文业务提示', () => {
    expect(toUserFacingError(new Error('验证码不正确或已过期'), '操作失败')).toBe('验证码不正确或已过期')
  })

  it('屏蔽供应商、地址和内部请求细节', () => {
    expect(toUserFacingError(new Error('provider secret missing at https://api.example.com'), '云能力暂时不可用'))
      .toBe('云能力暂时不可用')
    expect(toUserFacingError(new Error('HTTP 502 request_id=req-123'), '请求失败'))
      .toBe('请求失败')
  })

  it('把余额不足转换成可操作提示', () => {
    expect(toUserFacingError(new Error('insufficient wallet balance'), '请求失败'))
      .toBe('可用 Credit 不足，请充值或切换到本地能力')
  })

  it('保留可操作的中文连接提示，屏蔽浏览器底层网络错误', () => {
    expect(toUserFacingError(new Error('账户服务暂时无法连接'), '登录失败'))
      .toBe('账户服务暂时无法连接')
    expect(toUserFacingError(new TypeError('Failed to fetch'), '登录失败'))
      .toBe('登录失败')
  })
})

describe('toCreationFailureMessage', () => {
  const withCode = (message: string, errorCode: string) =>
    Object.assign(new Error(message), { errorCode })

  it('带 CREATION_ 错误码时保留含技术词的具体缺口，不被吞成兜底文案', () => {
    const reason = '自动修正 2 次后仍未通过验收：删除无法验证的数值并补回 source_id 的来源依据'
    expect(toCreationFailureMessage(withCode(reason, 'CREATION_DELIVERY_INCOMPLETE'), '生成失败，请稍后重试'))
      .toBe(reason)
    // 同一个原因若没有创作错误码，通用过滤会把它吞成兜底文案。
    expect(toUserFacingError(new Error(reason), '生成失败，请稍后重试'))
      .toBe('生成失败，请稍后重试')
  })

  it('非 CREATION_ 错误码仍走通用敏感词过滤', () => {
    expect(toCreationFailureMessage(withCode('provider secret missing', 'MODEL_TRANSPORT_UNAVAILABLE'), '创作中断'))
      .toBe('创作中断')
  })

  it('浏览器底层网络错误不外泄，长文案有界截断', () => {
    expect(toCreationFailureMessage(withCode('Load failed', 'CREATION_STREAM_INTERRUPTED'), '创作中断'))
      .toBe('创作中断')
    const long = '缺' .repeat(260)
    expect(toCreationFailureMessage(withCode(long, 'CREATION_DELIVERY_INCOMPLETE'), '创作中断'))
      .toBe(`${long.slice(0, 200)}…`)
  })
})

describe('recoverableCreationHint', () => {
  it('仅服务端标为可重试时提示重试继续', () => {
    expect(recoverableCreationHint(Object.assign(new Error('连接中断'), { retryable: true })))
      .toBe('，可重试继续')
    expect(recoverableCreationHint(Object.assign(new Error('未通过验收'), { retryable: false })))
      .toBe('')
    expect(recoverableCreationHint(new Error('未通过验收'))).toBe('')
    expect(recoverableCreationHint(null)).toBe('')
  })
})
