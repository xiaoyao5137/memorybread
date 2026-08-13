import React, { FormEvent, useEffect, useState } from 'react'
import { CheckCircle2, Mail, ShieldCheck, Smartphone, X } from 'lucide-react'
import type { CloudUser } from '../types'
import {
  bindAccountContact,
  sendAccountContactVerificationCode,
  type AccountContactChannel,
} from '../utils/authApi'
import { toUserFacingError } from '../utils/userFacingError'
import './AccountContactBindings.css'

interface AccountContactBindingsProps {
  adminApiBaseUrl: string
  authToken: string
  user: CloudUser
  onUserChange: (user: CloudUser) => void
}

const presentation = {
  email: {
    label: '邮箱',
    action: '绑定邮箱',
    placeholder: 'you@example.com',
    autoComplete: 'email',
    inputMode: 'email' as const,
    Icon: Mail,
  },
  phone: {
    label: '手机号',
    action: '绑定手机号',
    placeholder: '请输入中国大陆手机号',
    autoComplete: 'tel',
    inputMode: 'tel' as const,
    Icon: Smartphone,
  },
}

export const maskBoundEmail = (email: string) => {
  const [local = '', domain = ''] = email.split('@')
  if (!domain) return email
  const visible = local.slice(0, Math.min(2, local.length))
  return `${visible}${local.length > 2 ? '***' : '*'}@${domain}`
}

export const maskBoundPhone = (phone: string) => {
  const compact = phone.replace(/[^+\d]/g, '')
  const national = compact.startsWith('+86') ? compact.slice(3) : compact
  if (national.length !== 11) return phone
  return `+86 ${national.slice(0, 3)}****${national.slice(-4)}`
}

const formatCountdown = (seconds: number) => {
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return `${minutes}:${String(remainder).padStart(2, '0')}`
}

const AccountContactBindings: React.FC<AccountContactBindingsProps> = ({
  adminApiBaseUrl,
  authToken,
  user,
  onUserChange,
}) => {
  const [activeChannel, setActiveChannel] = useState<AccountContactChannel | null>(null)
  const [identifier, setIdentifier] = useState('')
  const [code, setCode] = useState('')
  const [challengeId, setChallengeId] = useState<string | undefined>()
  const [retryUntil, setRetryUntil] = useState<number | null>(null)
  const [expiresUntil, setExpiresUntil] = useState<number | null>(null)
  const [clock, setClock] = useState(() => Date.now())
  const [sending, setSending] = useState(false)
  const [binding, setBinding] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const retrySeconds = retryUntil ? Math.max(0, Math.ceil((retryUntil - clock) / 1000)) : 0
  const validitySeconds = expiresUntil ? Math.max(0, Math.ceil((expiresUntil - clock) / 1000)) : 0

  useEffect(() => {
    if (!retryUntil && !expiresUntil) return undefined
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [expiresUntil, retryUntil])

  const closeEditor = () => {
    setActiveChannel(null)
    setIdentifier('')
    setCode('')
    setChallengeId(undefined)
    setRetryUntil(null)
    setExpiresUntil(null)
    setError(null)
  }

  const openEditor = (channel: AccountContactChannel) => {
    closeEditor()
    setMessage(null)
    setActiveChannel(channel)
  }

  const changeIdentifier = (value: string) => {
    setIdentifier(value)
    setCode('')
    setChallengeId(undefined)
    setRetryUntil(null)
    setExpiresUntil(null)
    setError(null)
  }

  const sendCode = async () => {
    if (!activeChannel || !identifier.trim() || retrySeconds > 0) return
    setSending(true)
    setError(null)
    setMessage(null)
    try {
      const challenge = await sendAccountContactVerificationCode(
        adminApiBaseUrl,
        authToken,
        activeChannel,
        identifier.trim(),
      )
      const now = Date.now()
      setClock(now)
      setChallengeId(challenge.challenge_id)
      setRetryUntil(now + challenge.retry_after_seconds * 1000)
      setExpiresUntil(now + challenge.expires_in_seconds * 1000)
      setMessage(`验证码已发送，${Math.ceil(challenge.expires_in_seconds / 60)} 分钟内有效。`)
    } catch (sendError) {
      setError(toUserFacingError(sendError, '验证码发送失败，请稍后重试'))
    } finally {
      setSending(false)
    }
  }

  const confirmBinding = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!activeChannel) return
    if (activeChannel === 'email' && !challengeId) {
      setError('请先获取邮箱验证码')
      return
    }
    setBinding(true)
    setError(null)
    setMessage(null)
    try {
      const updatedUser = await bindAccountContact(
        adminApiBaseUrl,
        authToken,
        activeChannel,
        identifier.trim(),
        code,
        challengeId,
      )
      const label = presentation[activeChannel].label
      onUserChange(updatedUser)
      closeEditor()
      setMessage(`${label}绑定成功。`)
    } catch (bindingError) {
      setError(toUserFacingError(bindingError, '绑定失败，请稍后重试'))
    } finally {
      setBinding(false)
    }
  }

  const renderCard = (channel: AccountContactChannel, boundValue?: string | null) => {
    const item = presentation[channel]
    const Icon = item.Icon
    const isEditing = activeChannel === channel
    const masked = boundValue
      ? channel === 'email' ? maskBoundEmail(boundValue) : maskBoundPhone(boundValue)
      : null
    return (
      <article className={isEditing ? 'account-contact__card account-contact__card--editing' : 'account-contact__card'}>
        <div className="account-contact__summary">
          <span className="account-contact__icon"><Icon size={19} aria-hidden /></span>
          <div>
            <div className="account-contact__title-row">
              <h4>{item.label}</h4>
              <span className={masked ? 'account-contact__status account-contact__status--bound' : 'account-contact__status'}>
                {masked ? '已绑定' : '未绑定'}
              </span>
            </div>
            <p>{masked || `绑定${item.label}后，可用于验证身份和找回账户。`}</p>
          </div>
          {!masked && !isEditing && (
            <button className="account-contact__open" onClick={() => openEditor(channel)} type="button">
              {item.action}
            </button>
          )}
        </div>

        {isEditing && (
          <form className="account-contact__form" onSubmit={confirmBinding}>
            <label>
              <span>{item.label}</span>
              <input
                autoComplete={item.autoComplete}
                autoFocus
                inputMode={item.inputMode}
                onChange={(event) => changeIdentifier(event.target.value)}
                placeholder={item.placeholder}
                required
                type={channel === 'email' ? 'email' : 'tel'}
                value={identifier}
              />
            </label>
            <label>
              <span>验证码</span>
              <div className="account-contact__code-field">
                <input
                  autoComplete="one-time-code"
                  inputMode="numeric"
                  maxLength={channel === 'email' ? 6 : 8}
                  onChange={(event) => setCode(event.target.value)}
                  placeholder="请输入验证码"
                  required
                  value={code}
                />
                <button disabled={sending || !identifier.trim() || retrySeconds > 0} onClick={sendCode} type="button">
                  {sending ? '发送中' : retrySeconds > 0 ? `${retrySeconds}s` : challengeId || expiresUntil ? '重新发送' : '获取验证码'}
                </button>
              </div>
            </label>
            {expiresUntil && (
              <p className={validitySeconds > 0 ? 'account-contact__hint' : 'account-contact__hint account-contact__hint--expired'} role="status">
                {validitySeconds > 0 ? `验证码将在 ${formatCountdown(validitySeconds)} 后失效。` : '验证码已失效，请重新获取。'}
              </p>
            )}
            {error && <div className="account-contact__error" role="alert">{error}</div>}
            <div className="account-contact__actions">
              <button disabled={binding || sending} onClick={closeEditor} type="button"><X size={15} aria-hidden />取消</button>
              <button disabled={binding || sending || !identifier.trim() || !code.trim()} type="submit">
                <ShieldCheck size={15} aria-hidden />{binding ? '绑定中' : '确认绑定'}
              </button>
            </div>
          </form>
        )}
      </article>
    )
  }

  return (
    <section className="account-contact" aria-labelledby="account-contact-title">
      <header>
        <div>
          <h3 id="account-contact-title">登录与安全</h3>
          <p>每个邮箱和手机号只能绑定一个记忆面包账户。</p>
        </div>
        <ShieldCheck size={20} aria-hidden />
      </header>
      <div className="account-contact__grid">
        {renderCard('email', user.email)}
        {renderCard('phone', user.phone)}
      </div>
      {message && (
        <div className="account-contact__success" role="status">
          <CheckCircle2 size={17} aria-hidden />{message}
        </div>
      )}
    </section>
  )
}

export default AccountContactBindings
