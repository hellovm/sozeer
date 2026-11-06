/* ============================================================================
 * API · 个人中心 / 账户（/api/me/*）
 * ========================================================================== */

import http from '@/api/http'

export interface Profile {
  id: string
  username: string
  nickname?: string
  avatar?: string
  bio?: string
  /** ★ 后端返回的是**已脱敏**手机号（138****8888），不可回填到输入框 */
  phone?: string
  /** 是否已绑定手机（脱敏值非空不代表已绑定） */
  phone_bound?: boolean
  email?: string
  phone_verified?: number
  email_verified?: number
  totp_enabled?: number
  role?: string
  /** ★ 权限点列表 —— 前端判断权限的唯一依据（不比 role 字符串） */
  perms?: string[]
  is_admin?: boolean
  created_at?: string
  last_login_at?: string
}

export interface CreditAccount {
  balance: number
  frozen: number
  available: number
  total_earned?: number
  total_spent?: number
}

export interface LedgerEntry {
  id: string
  delta: number
  balance_after: number
  kind: string
  ref_id?: string
  note?: string
  created_at: string
}

export interface UsageSummary {
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
  cost_cents?: number
  by_task?: Array<{ task: string; calls: number; tokens: number; cost_cents: number }>
  by_day?: Array<{ date: string; tokens: number; cost_cents: number }>
}

/** ★ v1.0-E4.13 工作台用量概览（服务端实算，不缓存不美化） */
export interface UsageStats {
  books: number
  chapters: number
  words_total: number
  monthly_words: number
  dramas: number
  tasks: number
  balance: number
  total_spent: number
}

/**
 * ★ v1.0-E4.13 我的当前套餐（由最近一笔已支付订单反推，无订阅表假设）。
 *   未购过 → active=false + is_trial=true（前端引导升级，不做本地判断）。
 */
export interface PlanInfo {
  active: boolean
  is_trial: boolean
  plan_key: string
  plan_name: string
  effective: string | null
  expiry: string | null
  renewal: boolean
  period: string
  latest_order_id: string | null
  amount_cents: number
  note: string
}

export const meApi = {
  profile() {
    return http.get<Profile>('/api/me/profile')
  },

  /**
   * ★★ 更新资料：后端**只接受 nickname 与 bio**（对照 me.py:36 ProfileIn）。
   *   ★ 传 email 会被**静默忽略**（不报错）—— 邮箱有独立的验证流程，
   *     不能靠改资料字段绕过。这正是"猜错不报错"的典型场景。
   */
  updateProfile(payload: { nickname?: string; bio?: string }) {
    return http.patch<{ ok: boolean; nickname: string; bio: string }>(
      '/api/me/profile', payload)
  },

  changePassword(oldPassword: string, newPassword: string) {
    return http.post<{ ok: boolean }>('/api/me/password', {
      old_password: oldPassword,
      new_password: newPassword,
    })
  },

  /** ★ 权限点（前端判断权限**唯一**依据，不比 role 字符串） */
  permissions() {
    return http.get<{ role: string; perms: string[] }>('/api/me/permissions')
  },

  // ── 积分与用量 ──
  quota() {
    return http.get<CreditAccount>('/api/me/quota')
  },

  // ── ★ v1.0-E4 新手引导 ──
  //
  //   ★ 契约（逐个核对过 apps/api/onboarding.py）：
  //     steps 里每项都带 done —— 由**服务端**用真实数据推导，
  //     前端不做任何"是否完成"的判断（避免两头判定不一致）
  onboarding() {
    return http.get<OnboardingState>('/api/me/onboarding')
  },

  hideOnboarding() {
    return http.post<{ ok: boolean; hidden: boolean }>('/api/me/onboarding/hide')
  },

  showOnboarding() {
    return http.post<{ ok: boolean; hidden: boolean }>('/api/me/onboarding/show')
  },

  /** ★ first=true 时才弹庆祝（防重复打扰） */
  celebrateOnboarding() {
    return http.post<{ ok: boolean; first: boolean }>('/api/me/onboarding/celebrate')
  },

  usage(days = 30) {
    return http.get<UsageSummary>('/api/me/usage', { days })
  },

  /** ★ v1.0-E4.13 工作台用量总览（books/chapters/words/monthly_words/dramas/tasks/积分）。
   *  ★ 契约即后端 GET /api/me/usage（uc.usage_stats，服务端实算） */
  usageStats() {
    return http.get<UsageStats>('/api/me/usage')
  },


  /** 日更趋势（v1.0-E4.11：后端按章节 updated_at 聚合，裸数组 date 升序补 0） */
  dailyWrites(days = 30) {
    return http.get<Array<{ date: string; words: number; chapters: number }>>(
      '/api/stats/daily-writes', { days })
  },

  cost(days = 30) {
    return http.get<UsageSummary>('/api/me/cost', { days })
  },

  // ── 登录安全 ──
  sessions() {
    return http.get<{ items: LoginSession[] }>('/api/me/sessions')
  },

  revokeSession(sessionId: string) {
    return http.post<{ ok: boolean }>('/api/me/sessions/revoke', { session_id: sessionId })
  },

  revokeAllSessions() {
    return http.post<{ ok: boolean }>('/api/me/sessions/revoke-all', {})
  },

  loginRecords(limit = 50) {
    return http.get<{ items: LoginRecord[] }>('/api/me/login-records', { limit })
  },

  // ── 二次验证（TOTP） ──
  setup2fa() {
    return http.post<{ secret: string; otpauth_url: string; qr_svg?: string }>(
      '/api/me/2fa/setup', {})
  },

  enable2fa(code: string) {
    return http.post<{ ok: boolean; recovery_codes?: string[] }>(
      '/api/me/2fa/enable', { code })
  },

  disable2fa(code: string) {
    return http.post<{ ok: boolean }>('/api/me/2fa/disable', { code })
  },

  // ── 通知 ──
  notifications(limit = 30, offset = 0) {
    return http.get<{ total: number; items: Notification[] }>(
      '/api/me/notifications', { limit, offset })
  },

  notificationCount() {
    return http.get<{ unread: number }>('/api/me/notifications/count')
  },

  readNotifications(ids: string[]) {
    return http.post<{ ok: boolean }>('/api/me/notifications/read', { ids })
  },

  /**
   * ★★ 通知偏好：返回 `{items: {kind: {inapp, email, label, forced}}, forced_kinds, kinds}`。
   *   ★ 我一开始按 `{prefs, forced}` 实现 → 页面空白。
   *   ★ `forced_kinds` 是**不可关闭**的类型列表（安全类），UI 必须禁用其开关。
   */
  notificationPrefs() {
    return http.get<{
      items: Record<string, NotifyPrefItem>
      forced_kinds: string[]
      kinds: string[]
    }>('/api/me/notification-prefs')
  },

  /**
   * ★ 保存单个类型的偏好（**不是整表提交** —— 后端是 PerKind 接口）。
   *   为什么按类型提交：避免并发时互相覆盖（用户在多端改不同项）。
   */
  saveNotificationPref(kind: string, inapp?: boolean, email?: boolean) {
    return http.post<{ ok: boolean }>('/api/me/notification-prefs',
      { kind, inapp, email })
  },

  // ── 账号注销 ──
  deleteStatus() {
    return http.get<{ pending: boolean; requested_at?: string; execute_at?: string }>(
      '/api/me/delete-status')
  },

  requestDelete(reason: string) {
    return http.post<{ ok: boolean; execute_at: string }>(
      '/api/me/delete-request', { reason })
  },

  cancelDelete() {
    return http.post<{ ok: boolean }>('/api/me/delete-cancel', {})
  },

  /** 数据导出（个人信息可携带权） */
  exportData() {
    return http.post<{ ok: boolean; task_id?: string; download_url?: string }>(
      '/api/me/export', {})
  },
}

export interface NotifyPrefItem {
  /** 站内信开关 */
  inapp?: boolean
  /** 邮件开关 */
  email?: boolean
  /** 展示名 */
  label?: string
  /** ★ 是否强制开启（安全类） */
  forced?: boolean
}

export interface LoginSession {
  id: string
  ip: string
  /** 后端返回 user_agent；部分实现返回 ua —— 两者都声明以兼容 */
  user_agent?: string
  ua?: string
  created_at: string
  last_seen_at?: string
  /** 是否当前设备（服务端按 UA 前缀标出） */
  current?: boolean
}

export interface LoginRecord {
  id: string
  ip: string
  user_agent: string
  success: number
  reason: string
  created_at: string
}

export interface Notification {
  id: string
  kind: string
  title: string
  body: string
  read: number
  link?: string
  created_at: string
}

/* ── ★ v1.0-E4 新手引导 ──
 *
 * ★ 为什么 done 由服务端给：进度来自**真实数据**（有没有建过书），
 *   不是"点没点过引导按钮"。这样用户跳过引导自己摸会了，也会自动打勾。
 */
export interface OnboardingStep {
  key: string
  title: string
  desc: string
  action: string
  link: string
  hint?: string
  done: boolean
}

export interface OnboardingState {
  steps: OnboardingStep[]
  done_count: number
  total: number
  /** 0-100 */
  percent: number
  all_done: boolean
  hidden: boolean
  /** ★ 唯一判据：show=true 才渲染（未上线/已完成/已隐藏都是 false） */
  show: boolean
  reason?: string
  facts?: Record<string, number>
}

export default meApi
