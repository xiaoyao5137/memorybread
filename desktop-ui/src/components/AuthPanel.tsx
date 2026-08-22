import React, { FormEvent, useEffect, useState } from 'react'
import { ArrowLeft, ArrowRight, Building2, KeyRound, LockKeyhole, Mail, Smartphone, UserRound } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'
import type { AccountProfileSection } from '../types'
import { authenticateWithPassword, authenticateWithPhoneCode, confirmPasswordReset, fetchConsoleSummary, logoutSession, sendEmailVerificationCode, sendPasswordResetCode, sendPhoneVerificationCode } from '../utils/authApi'
import { getRunModeLabel, getUserDisplayName } from '../utils/accountDisplay'
import { toUserFacingError } from '../utils/userFacingError'
import AccountProfile from './AccountProfile'
import './AuthPanel.css'

type AuthMode = 'login' | 'register' | 'reset'
type LoginMethod = 'email' | 'phone'

const formatCountdown = (seconds: number): string => {
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
}

interface AuthPanelProps {
  initialProfileSection?: AccountProfileSection
  highlightedAchievementKeys?: string[]
  onInitialProfileSectionHandled?: () => void
}

const AuthPanel: React.FC<AuthPanelProps> = ({
  initialProfileSection,
  highlightedAchievementKeys,
  onInitialProfileSectionHandled,
}) => {
  const {
    apiBaseUrl,
    adminApiBaseUrl,
    authToken,
    authExpiresAt,
    currentUser,
    cloudBalance,
    cloudSubscription,
    localNickname,
    setAuthSession,
    setCloudBalance,
    setCloudSubscription,
    clearAuthSession,
  } = useAppStore()
  const [mode, setMode] = useState<AuthMode>('login')
  const [loginMethod, setLoginMethod] = useState<LoginMethod>('email')
  const [nickname, setNickname] = useState('')
  const [companyName, setCompanyName] = useState('')
  const [email, setEmail] = useState('')
  const [emailCode, setEmailCode] = useState('')
  const [emailChallengeId, setEmailChallengeId] = useState<string | null>(null)
  const [emailCodeSent, setEmailCodeSent] = useState(false)
  const [emailRetryUntil, setEmailRetryUntil] = useState<number | null>(null)
  const [emailExpiresUntil, setEmailExpiresUntil] = useState<number | null>(null)
  const [clock, setClock] = useState(() => Date.now())
  const [phone, setPhone] = useState('')
  const [phoneCode, setPhoneCode] = useState('')
  const [codeSent, setCodeSent] = useState(false)
  const [phoneRetryUntil, setPhoneRetryUntil] = useState<number | null>(null)
  const [phoneExpiresUntil, setPhoneExpiresUntil] = useState<number | null>(null)
  const [password, setPassword] = useState('')
  const [resetChallengeId, setResetChallengeId] = useState<string | null>(null)
  const [resetCode, setResetCode] = useState('')
  const [resetCodeSent, setResetCodeSent] = useState(false)
  const [resetRetryUntil, setResetRetryUntil] = useState<number | null>(null)
  const [resetExpiresUntil, setResetExpiresUntil] = useState<number | null>(null)
  const [authRetryUntil, setAuthRetryUntil] = useState<number | null>(null)
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [balanceError, setBalanceError] = useState<string | null>(null)
  const emailRetrySeconds = emailRetryUntil
    ? Math.max(0, Math.ceil((emailRetryUntil - clock) / 1_000))
    : 0
  const emailValiditySeconds = emailExpiresUntil
    ? Math.max(0, Math.ceil((emailExpiresUntil - clock) / 1_000))
    : 0
  const phoneRetrySeconds = phoneRetryUntil
    ? Math.max(0, Math.ceil((phoneRetryUntil - clock) / 1_000))
    : 0
  const phoneValiditySeconds = phoneExpiresUntil
    ? Math.max(0, Math.ceil((phoneExpiresUntil - clock) / 1_000))
    : 0
  const resetRetrySeconds = resetRetryUntil
    ? Math.max(0, Math.ceil((resetRetryUntil - clock) / 1_000))
    : 0
  const resetValiditySeconds = resetExpiresUntil
    ? Math.max(0, Math.ceil((resetExpiresUntil - clock) / 1_000))
    : 0
  const authRetrySeconds = authRetryUntil
    ? Math.max(0, Math.ceil((authRetryUntil - clock) / 1_000))
    : 0

  useEffect(() => {
    const deadlines = [
      emailRetryUntil,
      emailExpiresUntil,
      phoneRetryUntil,
      phoneExpiresUntil,
      resetRetryUntil,
      resetExpiresUntil,
      authRetryUntil,
    ].filter((deadline): deadline is number => deadline !== null)
    const now = Date.now()
    if (!deadlines.some((deadline) => deadline > now)) return
    setClock(now)
    const timer = window.setInterval(() => {
      const nextNow = Date.now()
      setClock(nextNow)
      if (deadlines.every((deadline) => deadline <= nextNow)) window.clearInterval(timer)
    }, 1_000)
    return () => window.clearInterval(timer)
  }, [
    authRetryUntil,
    emailExpiresUntil,
    emailRetryUntil,
    phoneExpiresUntil,
    phoneRetryUntil,
    resetExpiresUntil,
    resetRetryUntil,
  ])

  const refreshBalance = async () => {
    if (!authToken || !currentUser) return
    setBalanceError(null)
    try {
      const summary = await fetchConsoleSummary(adminApiBaseUrl, authToken)
      setCloudBalance(summary.balance ?? null)
      setCloudSubscription(summary.current_subscription ?? null)
    } catch (err) {
      setCloudBalance(null)
      setCloudSubscription(null)
      setBalanceError(toUserFacingError(err, '账户余额读取失败'))
    }
  }

  useEffect(() => {
    void refreshBalance()
  }, [authToken, currentUser?.id, adminApiBaseUrl, setCloudBalance, setCloudSubscription])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setLoading(true)
    setError(null)
    setNotice(null)
    try {
      if (mode === 'reset') {
        if (!resetChallengeId || !resetCode.trim()) {
          setError('请先获取并填写验证码')
          return
        }
        if (newPassword !== confirmPassword) {
          setError('两次输入的密码不一致')
          return
        }
        const identifier = loginMethod === 'email' ? email : phone
        await confirmPasswordReset(adminApiBaseUrl, {
          challenge_id: resetChallengeId,
          channel: loginMethod,
          identifier,
          code: resetCode.trim(),
          new_password: newPassword,
        })
        handleModeChange('login')
        setLoginMethod('email')
        setEmail(identifier.trim())
        setPassword('')
        setNotice('密码已重置，请使用新密码登录。其他设备上的旧会话已退出。')
        return
      }
      if (loginMethod !== 'email') {
        const session = await authenticateWithPhoneCode(
          adminApiBaseUrl,
          phone,
          phoneCode,
          undefined,
          mode === 'register' ? nickname : undefined,
          mode === 'register' ? companyName : undefined,
        )
        setAuthSession(session)
        return
      }
      if (mode === 'register' && (!emailChallengeId || !emailCode.trim())) {
        setError('请先获取并填写邮箱验证码')
        return
      }
      const session = await authenticateWithPassword(
        adminApiBaseUrl,
        mode,
        email,
        password,
        undefined,
        nickname,
        companyName,
        mode === 'register' ? emailChallengeId ?? undefined : undefined,
        mode === 'register' ? emailCode : undefined,
      )
      setAuthSession(session)
    } catch (err) {
      const retryAfter = (err as { retryAfterSeconds?: number } | null)?.retryAfterSeconds
      if (retryAfter) {
        const now = Date.now()
        setClock(now)
        setAuthRetryUntil(now + retryAfter * 1_000)
      }
      setError(toUserFacingError(err, '登录失败，请检查网络或账户信息'))
    } finally {
      setLoading(false)
    }
  }

  const handleEmailChange = (value: string) => {
    setEmail(value)
    setAuthRetryUntil(null)
    if (emailChallengeId || emailCodeSent) {
      setEmailCode('')
      setEmailChallengeId(null)
      setEmailCodeSent(false)
      setEmailRetryUntil(null)
      setEmailExpiresUntil(null)
      setError(null)
    }
    if (mode === 'reset' && (resetChallengeId || resetCodeSent)) {
      resetPasswordVerification()
      setNotice(null)
    }
  }

  const handlePhoneChange = (value: string) => {
    setPhone(value)
    setAuthRetryUntil(null)
    if (codeSent || phoneRetryUntil || phoneExpiresUntil) {
      resetPhoneVerification()
      setError(null)
    }
    if (mode === 'reset' && (resetChallengeId || resetCodeSent)) {
      resetPasswordVerification()
      setError(null)
      setNotice(null)
    }
  }

  const resetEmailVerification = () => {
    setEmailCode('')
    setEmailChallengeId(null)
    setEmailCodeSent(false)
    setEmailRetryUntil(null)
    setEmailExpiresUntil(null)
  }

  const resetPhoneVerification = () => {
    setPhoneCode('')
    setCodeSent(false)
    setPhoneRetryUntil(null)
    setPhoneExpiresUntil(null)
  }

  const resetPasswordVerification = () => {
    setResetChallengeId(null)
    setResetCode('')
    setResetCodeSent(false)
    setResetRetryUntil(null)
    setResetExpiresUntil(null)
    setNewPassword('')
    setConfirmPassword('')
  }

  const handleModeChange = (nextMode: AuthMode) => {
    setMode(nextMode)
    setError(null)
    setNotice(null)
    setAuthRetryUntil(null)
    if (nextMode !== 'register') resetEmailVerification()
    if (nextMode !== 'reset') resetPasswordVerification()
    resetPhoneVerification()
  }

  const handleLoginMethodChange = (nextMethod: LoginMethod) => {
    setLoginMethod(nextMethod)
    setError(null)
    setNotice(null)
    setAuthRetryUntil(null)
    if (nextMethod !== 'email') resetEmailVerification()
    if (nextMethod !== 'phone') resetPhoneVerification()
    if (mode === 'reset') resetPasswordVerification()
  }

  const handleSendEmailCode = async () => {
    setLoading(true)
    setError(null)
    try {
      const challenge = await sendEmailVerificationCode(adminApiBaseUrl, email)
      setEmailChallengeId(challenge.challenge_id)
      setEmailCode('')
      setEmailCodeSent(true)
      const now = Date.now()
      setClock(now)
      setEmailRetryUntil(now + challenge.retry_after_seconds * 1_000)
      setEmailExpiresUntil(now + challenge.expires_in_seconds * 1_000)
    } catch (err) {
      const retryAfter = (err as { retryAfterSeconds?: number } | null)?.retryAfterSeconds
      if (retryAfter) {
        const now = Date.now()
        setClock(now)
        setEmailRetryUntil(now + retryAfter * 1_000)
      }
      setError(toUserFacingError(err, '验证码发送失败'))
    } finally {
      setLoading(false)
    }
  }

  const handleSendPasswordResetCode = async () => {
    if (resetRetrySeconds > 0) return
    setLoading(true)
    setError(null)
    setNotice(null)
    try {
      const identifier = loginMethod === 'email' ? email : phone
      const challenge = await sendPasswordResetCode(adminApiBaseUrl, loginMethod, identifier)
      setResetChallengeId(challenge.challenge_id)
      setResetCode('')
      setResetCodeSent(true)
      const now = Date.now()
      setClock(now)
      setResetRetryUntil(now + challenge.retry_after_seconds * 1_000)
      setResetExpiresUntil(now + challenge.expires_in_seconds * 1_000)
    } catch (err) {
      const retryAfter = (err as { retryAfterSeconds?: number } | null)?.retryAfterSeconds
      if (retryAfter) {
        const now = Date.now()
        setClock(now)
        setResetRetryUntil(now + retryAfter * 1_000)
      }
      setError(toUserFacingError(err, '验证码发送失败'))
    } finally {
      setLoading(false)
    }
  }

  const handleLogout = async () => {
    if (authToken) await logoutSession(adminApiBaseUrl, authToken)
    clearAuthSession()
  }

  const handleSendPhoneCode = async () => {
    if (phoneRetrySeconds > 0) return
    setLoading(true)
    setError(null)
    try {
      const challenge = await sendPhoneVerificationCode(adminApiBaseUrl, phone)
      setCodeSent(true)
      setPhoneCode('')
      const now = Date.now()
      setClock(now)
      setPhoneRetryUntil(now + challenge.retry_after_seconds * 1_000)
      setPhoneExpiresUntil(now + challenge.expires_in_seconds * 1_000)
    } catch (err) {
      const retryAfter = (err as { retryAfterSeconds?: number } | null)?.retryAfterSeconds
      if (retryAfter) {
        const now = Date.now()
        setClock(now)
        setPhoneRetryUntil(now + retryAfter * 1_000)
      }
      setError(toUserFacingError(err, '验证码发送失败'))
    } finally {
      setLoading(false)
    }
  }

  if (currentUser) {
    const accountLabel = getUserDisplayName(currentUser)
    const runModeLabel = getRunModeLabel(currentUser, cloudSubscription)
    return (
      <AccountProfile
        accountLabel={accountLabel}
        adminApiBaseUrl={adminApiBaseUrl}
        apiBaseUrl={apiBaseUrl}
        authToken={authToken!}
        balanceError={balanceError}
        cloudBalance={cloudBalance}
        highlightedAchievementKeys={highlightedAchievementKeys}
        initialSection={initialProfileSection}
        onInitialSectionHandled={onInitialProfileSectionHandled}
        onUserChange={(user) => setAuthSession({
          access_token: authToken!,
          expires_at: authExpiresAt || new Date(Date.now() + 30 * 86400_000).toISOString(),
          user,
        })}
        onLogout={handleLogout}
        runModeLabel={runModeLabel}
        user={currentUser}
      />
    )
  }

  const signedOutPersonalContent = (
    <div className="auth-panel auth-panel--login auth-panel--embedded">
      <form className="auth-panel__form" onSubmit={submit}>
        <div className="auth-panel__form-head">
          <span className="auth-panel__form-icon" aria-hidden="true"><LockKeyhole size={18} /></span>
          <div>
            <strong>{mode === 'reset' ? '重置密码' : mode === 'login' ? '登录账户' : '注册账户'}</strong>
            <span>{mode === 'reset' ? '验证已绑定的联系方式' : mode === 'login' ? '使用已有账户登录' : '创建账户后自动登录'}</span>
          </div>
        </div>

        {mode === 'reset' ? (
          <button
            className="auth-panel__back"
            onClick={() => handleModeChange('login')}
            type="button"
          >
            <ArrowLeft size={15} aria-hidden />
            返回登录
          </button>
        ) : (
          <div className="auth-panel__tabs" role="tablist" aria-label="账户动作">
            <button
              aria-selected={mode === 'login'}
              className={mode === 'login' ? 'auth-panel__tab auth-panel__tab--active' : 'auth-panel__tab'}
              onClick={() => handleModeChange('login')}
              role="tab"
              type="button"
            >
              登录
            </button>
            <button
              aria-selected={mode === 'register'}
              className={mode === 'register' ? 'auth-panel__tab auth-panel__tab--active' : 'auth-panel__tab'}
              onClick={() => handleModeChange('register')}
              role="tab"
              type="button"
            >
              注册
            </button>
          </div>
        )}

        <div className="auth-panel__method-tabs" role="tablist" aria-label={mode === 'reset' ? '验证方式' : mode === 'login' ? '登录方式' : '注册方式'}>
          <button
            aria-selected={loginMethod === 'email'}
            className={loginMethod === 'email' ? 'auth-panel__method-tab auth-panel__method-tab--active' : 'auth-panel__method-tab'}
            onClick={() => handleLoginMethodChange('email')}
            role="tab"
            type="button"
          >
            <Mail size={15} aria-hidden />
            {mode === 'reset' ? '邮箱验证' : mode === 'login' ? '密码登录' : '邮箱注册'}
          </button>
          <button
            aria-selected={loginMethod === 'phone'}
            className={loginMethod === 'phone' ? 'auth-panel__method-tab auth-panel__method-tab--active' : 'auth-panel__method-tab'}
            onClick={() => handleLoginMethodChange('phone')}
            role="tab"
            type="button"
          >
            <Smartphone size={15} aria-hidden />
            {mode === 'reset' ? '手机号验证' : mode === 'login' ? '验证码登录' : '手机号注册'}
          </button>
        </div>

        {loginMethod === 'email' && (
          <label>
            <span>{mode === 'login' ? '邮箱或手机号' : '邮箱地址'}</span>
            <div className="auth-panel__input-with-icon">
              <Mail size={16} aria-hidden />
              <input
                autoComplete={mode === 'login' ? 'username' : 'email'}
                inputMode={mode === 'login' ? 'text' : 'email'}
                onChange={(event) => handleEmailChange(event.target.value)}
                placeholder={mode === 'login' ? 'you@example.com' : undefined}
                required
                type={mode === 'login' ? 'text' : 'email'}
                value={email}
              />
            </div>
          </label>
        )}

        {loginMethod === 'email' && mode === 'register' && (
          <label>
            <span>邮箱验证码</span>
            <div className="auth-panel__code-row">
              <div className="auth-panel__input-with-icon">
                <KeyRound size={16} aria-hidden />
                <input
                  aria-label="邮箱验证码"
                  autoComplete="one-time-code"
                  inputMode="numeric"
                  maxLength={6}
                  onChange={(event) => setEmailCode(event.target.value)}
                  placeholder={emailCodeSent ? '请输入 6 位验证码' : '先获取验证码'}
                  required
                  value={emailCode}
                />
              </div>
              <button
                className="auth-panel__code-button"
                disabled={loading || !email.trim() || emailRetrySeconds > 0}
                onClick={() => void handleSendEmailCode()}
                type="button"
              >
                {emailRetrySeconds > 0 ? `${emailRetrySeconds}s` : emailCodeSent ? '重新发送' : '获取验证码'}
              </button>
            </div>
            {emailCodeSent && (
              <small className={emailValiditySeconds > 0 ? 'auth-panel__verification-note' : 'auth-panel__verification-note auth-panel__verification-note--expired'} role="status">
                {emailValiditySeconds > 0
                  ? <>已发送验证码 · <time>{formatCountdown(emailValiditySeconds)}</time> 后失效。未收到时请检查垃圾邮件。</>
                  : '验证码已失效，请重新获取。'}
              </small>
            )}
          </label>
        )}

        {loginMethod === 'phone' && (
          <label>
            <span className="auth-panel__field-heading">
              <span>手机号</span>
              {mode !== 'reset' && <small>请关注来自“恒创联众”的验证码短信</small>}
            </span>
            <div className="auth-panel__input-with-icon">
              <Smartphone size={16} aria-hidden />
              <input
                aria-label="手机号"
                autoComplete="tel"
                onChange={(event) => handlePhoneChange(event.target.value)}
                placeholder={mode === 'login' ? '请输入手机号' : undefined}
                required
                type="tel"
                value={phone}
              />
            </div>
          </label>
        )}

        {loginMethod === 'phone' && mode !== 'reset' && (
          <label>
            <span>验证码</span>
            <div className="auth-panel__code-row">
              <div className="auth-panel__input-with-icon">
                <KeyRound size={16} aria-hidden />
                <input
                  aria-label="短信验证码"
                  autoComplete="one-time-code"
                  inputMode="numeric"
                  maxLength={8}
                  onChange={(event) => setPhoneCode(event.target.value)}
                  placeholder={codeSent ? '请输入短信验证码' : '先获取验证码'}
                  required
                  value={phoneCode}
                />
              </div>
              <button
                className="auth-panel__code-button"
                disabled={loading || !phone.trim() || phoneRetrySeconds > 0}
                onClick={() => void handleSendPhoneCode()}
                type="button"
              >
                {phoneRetrySeconds > 0 ? `${phoneRetrySeconds}s` : codeSent ? '重新发送' : '获取验证码'}
              </button>
            </div>
            {codeSent && (
              <small className={phoneValiditySeconds > 0 ? 'auth-panel__verification-note' : 'auth-panel__verification-note auth-panel__verification-note--expired'} role="status">
                {phoneValiditySeconds > 0
                  ? <>已发送验证码 · <time>{formatCountdown(phoneValiditySeconds)}</time> 后失效。</>
                  : '验证码已失效，请重新获取。'}
              </small>
            )}
          </label>
        )}

        {mode === 'register' && (
          <label>
            <span>昵称</span>
            <div className="auth-panel__input-with-icon">
              <UserRound size={16} aria-hidden />
              <input
                autoComplete="nickname"
                maxLength={30}
                minLength={2}
                onChange={(event) => setNickname(event.target.value)}
                required
                value={nickname}
              />
            </div>
          </label>
        )}

        {mode === 'register' && (
          <label>
            <span>公司名称（可选）</span>
            <div className="auth-panel__input-with-icon">
              <Building2 size={16} aria-hidden />
              <input
                autoComplete="organization"
                maxLength={100}
                onChange={(event) => setCompanyName(event.target.value)}
                placeholder="例如：记忆面包科技"
                value={companyName}
              />
            </div>
          </label>
        )}

        {mode === 'register' && (
          <p className="auth-panel__profile-note">昵称和公司名称注册后仍可修改，每项每个自然月最多 3 次。</p>
        )}

        {loginMethod === 'email' && mode !== 'reset' && (
          <label>
            <span>密码</span>
            <div className="auth-panel__input-with-icon">
              <KeyRound size={16} aria-hidden />
              <input
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                minLength={8}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="至少 8 个字符"
                required
                type="password"
                value={password}
              />
            </div>
          </label>
        )}

        {mode === 'login' && loginMethod === 'email' && (
          <button className="auth-panel__forgot" onClick={() => handleModeChange('reset')} type="button">
            忘记密码？
          </button>
        )}

        {mode === 'reset' && (
          <>
            <label>
              <span>验证码</span>
              <div className="auth-panel__code-row">
                <div className="auth-panel__input-with-icon">
                  <KeyRound size={16} aria-hidden />
                  <input
                    aria-label="验证码"
                    autoComplete="one-time-code"
                    inputMode="numeric"
                    maxLength={loginMethod === 'email' ? 6 : 8}
                    onChange={(event) => setResetCode(event.target.value)}
                    placeholder={resetCodeSent ? '请输入验证码' : '先获取验证码'}
                    required
                    value={resetCode}
                  />
                </div>
                <button
                  className="auth-panel__code-button"
                  disabled={loading || !(loginMethod === 'email' ? email.trim() : phone.trim()) || resetRetrySeconds > 0}
                  onClick={() => void handleSendPasswordResetCode()}
                  type="button"
                >
                  {resetRetrySeconds > 0 ? `${resetRetrySeconds}s` : resetCodeSent ? '重新发送' : '获取验证码'}
                </button>
              </div>
              {resetCodeSent && (
                <small className={resetValiditySeconds > 0 ? 'auth-panel__verification-note' : 'auth-panel__verification-note auth-panel__verification-note--expired'} role="status">
                  {resetValiditySeconds > 0
                    ? <>已发送验证码 · <time>{formatCountdown(resetValiditySeconds)}</time> 后失效，请查看{loginMethod === 'email' ? '邮箱' : '短信'}。</>
                    : '验证码已失效，请重新获取。'}
                </small>
              )}
            </label>
            <label>
              <span>新密码</span>
              <div className="auth-panel__input-with-icon">
                <KeyRound size={16} aria-hidden />
                <input
                  autoComplete="new-password"
                  minLength={8}
                  onChange={(event) => setNewPassword(event.target.value)}
                  placeholder="至少 8 个字符"
                  required
                  type="password"
                  value={newPassword}
                />
              </div>
            </label>
            <label>
              <span>确认新密码</span>
              <div className="auth-panel__input-with-icon">
                <KeyRound size={16} aria-hidden />
                <input
                  autoComplete="new-password"
                  minLength={8}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  required
                  type="password"
                  value={confirmPassword}
                />
              </div>
            </label>
            <p className="auth-panel__profile-note">重置成功后，已登录的其他设备会退出账户。</p>
          </>
        )}

        {error && <div className="auth-panel__error" role="alert">{error}</div>}
        {notice && <div className="auth-panel__notice" role="status">{notice}</div>}

        <button className="auth-panel__submit" disabled={loading || authRetrySeconds > 0} type="submit">
          {loading
            ? '处理中...'
            : authRetrySeconds > 0
              ? `请稍后重试 (${authRetrySeconds}s)`
              : mode === 'reset'
                ? '重置密码'
                : mode === 'login'
                  ? '登录'
                  : '注册并登录'}
          <ArrowRight size={16} aria-hidden />
        </button>
      </form>
    </div>
  )

  return (
    <AccountProfile
      accountLabel={localNickname}
      adminApiBaseUrl={adminApiBaseUrl}
      apiBaseUrl={apiBaseUrl}
      authToken={null}
      balanceError={null}
      cloudBalance={null}
      highlightedAchievementKeys={highlightedAchievementKeys}
      initialSection={initialProfileSection}
      onInitialSectionHandled={onInitialProfileSectionHandled}
      onUserChange={() => {}}
      onLogout={() => {}}
      runModeLabel={getRunModeLabel(null, null)}
      signedOutPersonalContent={signedOutPersonalContent}
      user={null}
    />
  )
}

export default AuthPanel
