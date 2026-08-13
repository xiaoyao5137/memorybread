import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AccountContactBindings, {
  maskBoundEmail,
  maskBoundPhone,
} from '../components/AccountContactBindings'
import type { CloudUser } from '../types'
import {
  bindAccountContact,
  sendAccountContactVerificationCode,
} from '../utils/authApi'

vi.mock('../utils/authApi', () => ({
  bindAccountContact: vi.fn(),
  sendAccountContactVerificationCode: vi.fn(),
}))

const user: CloudUser = {
  id: '019f0000-0000-7000-8000-000000000001',
  nickname: '小麦',
  email: 'xiaomai@example.com',
  phone: null,
  status: 'active',
  roles: ['user'],
  locale: 'zh-CN',
  timezone: 'Asia/Shanghai',
  created_at: '2026-08-01T08:00:00Z',
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AccountContactBindings', () => {
  it('masks contacts without exposing a replace action', () => {
    expect(maskBoundEmail('xiaomai@example.com')).toBe('xi***@example.com')
    expect(maskBoundPhone('+8613800138000')).toBe('+86 138****8000')

    render(
      <AccountContactBindings
        adminApiBaseUrl="https://account.example.com"
        authToken="test-token"
        onUserChange={vi.fn()}
        user={{ ...user, phone: '+8613800138000' }}
      />,
    )

    expect(screen.getByText('xi***@example.com')).toBeInTheDocument()
    expect(screen.getByText('+86 138****8000')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '绑定邮箱' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '绑定手机号' })).not.toBeInTheDocument()
  })

  it('sends a code and binds an unoccupied phone number', async () => {
    vi.mocked(sendAccountContactVerificationCode).mockResolvedValue({
      retry_after_seconds: 60,
      expires_in_seconds: 600,
    })
    const updatedUser = { ...user, phone: '+8613800138000' }
    vi.mocked(bindAccountContact).mockResolvedValue(updatedUser)
    const onUserChange = vi.fn()

    render(
      <AccountContactBindings
        adminApiBaseUrl="https://account.example.com"
        authToken="test-token"
        onUserChange={onUserChange}
        user={user}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '绑定手机号' }))
    fireEvent.change(screen.getByLabelText('手机号'), { target: { value: '13800138000' } })
    fireEvent.click(screen.getByRole('button', { name: '获取验证码' }))
    await waitFor(() => expect(sendAccountContactVerificationCode).toHaveBeenCalledWith(
      'https://account.example.com',
      'test-token',
      'phone',
      '13800138000',
    ))

    fireEvent.change(screen.getByLabelText('验证码'), { target: { value: '123456' } })
    fireEvent.click(screen.getByRole('button', { name: '确认绑定' }))
    await waitFor(() => expect(onUserChange).toHaveBeenCalledWith(updatedUser))
    expect(bindAccountContact).toHaveBeenCalledWith(
      'https://account.example.com',
      'test-token',
      'phone',
      '13800138000',
      '123456',
      undefined,
    )
    expect(await screen.findByText('手机号绑定成功。')).toBeInTheDocument()
  })
})
