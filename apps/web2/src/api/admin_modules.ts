/**
 * admin_modules.ts · 后台业务 API（v0.26.2 G0-6）
 *
 * ★★ 本文件所有类型都**逐个对照后端源码**写的，不做"合理推测"。
 *   本项目列表返回风格有三种共存（见 MEMORY §80/§97）：
 *     · `{items: [...], total}`  —— 后台列表基本都用这个（带分页）
 *     · 裸数组 `[...]`            —— 前台用得多
 *     · 自定义键                  —— 少数
 *   ★ 猜错**不报错**，只表现为"页面空白"。
 */
import { adminApi, adminDownload, adminReq } from '@/api/admin'

/* ================================================================ 通用分页 */

export interface Paged<T> {
  total: number
  items: T[]
  /** 部分端点会带 limit/offset 回显 */
  limit?: number
  offset?: number
}

/* ================================================================ 会话 */

export interface AdminMe {
  id: string
  username: string
  nickname?: string
  role: string
  /** ★ 权限点列表 —— 前端判权的唯一依据（绝不比 role 字符串） */
  perms?: string[]
  is_admin?: boolean
}

export interface AdminLoginResult {
  ok?: boolean
  token?: string
  access_token?: string
  role?: string
  perms?: string[]
  is_admin?: boolean
  user?: { role?: string }
  detail?: string
}

/**
 * ★★ 管理员登录。
 *
 * ★ 登录接口**不能**用全局 401 事件（否则输错密码会触发"会话失效"跳转）。
 *   → silent401: true，由调用方展示行内错误（与前台 http.ts 同一铁律，§34/§77）。
 */
export function adminLogin(username: string, password: string) {
  return adminApi.post<AdminLoginResult>('/api/auth/login', { username, password })
}

export function adminMe() {
  return adminApi.get<AdminMe>('/api/auth/me')
}

/* ================================================================ 总览看板 */

export interface DashboardOverview {
  generated_at: string
  window_days: number
  users: { total: number; active: number; new_in_window: number; deleted: number }
  revenue: {
    paid_orders: number
    total_cents: number
    total_yuan: number
    window_orders: number
    window_cents: number
    window_yuan: number
    pending_orders: number
    refunded_orders: number
    refunded_cents: number
    refunded_yuan: number
    net_cents: number
    net_yuan: number
  }
  content: {
    books: number
    new_books_in_window: number
    chapters: number
    words: number
  }
  tasks: { running: number; failed_in_window: number }
  moderation: { open_flags: number; hidden_books: number }
  credits: { outstanding: number; frozen: number }
}

export interface DashboardTrend {
  labels: string[]
  new_users: number[]
  new_books: number[]
  paid_orders: number[]
  revenue_cents: number[]
  revenue_yuan: number[]
}

export const dashboardApi = {
  overview: (days = 14) =>
    adminApi.get<DashboardOverview>('/api/admin/dashboard', { days }),
  trend: (days = 14) =>
    adminApi.get<DashboardTrend>('/api/admin/dashboard/trend', { days }),
}

/* ================================================================ 用户管理 */

export interface AdminUserRow {
  id: string
  username: string
  nickname: string
  email: string
  /** ★ 已是脱敏值（138****8888）—— 后台也不给全量 */
  phone: string
  role: string
  is_active: number
  deleted: boolean
  credits: number
  credits_frozen: number
  books: number
  totp_enabled: number
  last_login_at: string
  created_at: string
}

export interface UserSearchQuery {
  keyword?: string
  role?: string
  status?: string
  limit?: number
  offset?: number
}

export const usersApi = {
  search: (q: UserSearchQuery = {}) =>
    adminApi.get<Paged<AdminUserRow>>('/api/admin/users/search', {
      keyword: q.keyword, role: q.role, status: q.status,
      limit: q.limit ?? 50, offset: q.offset ?? 0,
    }),

  /** 封禁 / 解封（★ 需要原因，审计留痕） */
  setActive: (userId: string, active: boolean, reason: string) =>
    adminApi.post<{ ok: boolean; error?: string }>('/api/admin/users/ban',
      { user_id: userId, active, reason }),

  /** 角色变更（★ 需要原因） */
  setRole: (userId: string, role: string, reason: string) =>
    adminApi.post<{ ok: boolean; error?: string }>('/api/admin/users/role',
      { user_id: userId, role, reason }),

  /** ★ v1.0-E4.13 批量操作（ban/unban/set_role/adjust_credits；服务端逐个写审计） */
  bulk: (action: string, userIds: string[], reason: string,
         opts: { role?: string; delta?: number } = {}) =>
    adminApi.post<{ ok: boolean; total: number; succeeded: number; failed: number;
                    errors: Array<{ user_id: string; error: string }> }>(
      '/api/admin/users/bulk',
      { action, user_ids: userIds, reason, role: opts.role || '', delta: opts.delta || 0 }),

  /** ★ v1.0-E4.13 导出指定用户全部个人数据（ZIP，个保法合规协助） */
  exportUser: (userId: string, filename: string) =>
    adminDownload(`/api/admin/users/${encodeURIComponent(userId)}/export`, filename),

  /** ★ v1.0-E4.13 管理员强制删除用户（级联硬删；高危，需原因） */
  forceDelete: (userId: string, reason: string, hardDeleteVouchers = false) =>
    adminReq<{ ok: boolean }>(`/api/admin/users/${encodeURIComponent(userId)}`,
      { method: 'DELETE', body: { reason, hard_delete_vouchers: hardDeleteVouchers } }),
}

/* ================================================================ 订单财务 */

export interface AdminOrderRow {
  id: string
  user_id: string
  user_name?: string
  plan_key?: string
  kind?: string
  pay_channel?: string
  amount_cents: number
  credits: number
  status: string
  trade_no?: string
  paid_at?: string
  created_at: string
}

export interface OrderQuery {
  status?: string
  keyword?: string
  limit?: number
  offset?: number
}

export const ordersApi = {
  list: (q: OrderQuery = {}) =>
    adminApi.get<Paged<AdminOrderRow>>('/api/admin/orders', {
      status: q.status, keyword: q.keyword,
      limit: q.limit ?? 50, offset: q.offset ?? 0,
    }),

  /** 导出 CSV（★ 走 fetch+Blob，直接开链接不带 token 会 401） */
  exportCsv: (status = '') =>
    adminDownload('/api/admin/orders/export', `orders_${today()}.csv`,
      status ? { status } : undefined),
}

/* ================================================================ 发票管理 */

export type InvoiceStatus =
  | 'applied' | 'approved' | 'issued' | 'rejected' | 'void'

export interface AdminInvoiceRow {
  id: string
  user_id: string
  user_name?: string
  title: string
  tax_no?: string
  title_type: 'personal' | 'company'
  kind: 'normal' | 'special'
  amount_cents: number
  order_count: number
  status: InvoiceStatus
  invoice_no?: string
  file_path?: string
  reject_reason?: string
  void_reason?: string
  created_at: string
}

export interface InvoiceSummary {
  pending: number
  issued_count: number
  issued_cents: number
  void_count: number
  rejected_count: number
}

export interface InvoiceQuery {
  status?: string
  keyword?: string
  limit?: number
  offset?: number
}

export const invoicesApi = {
  list: (q: InvoiceQuery = {}) =>
    adminApi.get<Paged<AdminInvoiceRow>>('/api/admin/invoices', {
      status: q.status, keyword: q.keyword,
      limit: q.limit ?? 50, offset: q.offset ?? 0,
    }),

  summary: () => adminApi.get<InvoiceSummary>('/api/admin/invoices/summary'),

  /** 审核：approve=true 通过 / false 驳回（★ 必须带原因） */
  review: (invoiceId: string, approve: boolean, reason: string) =>
    adminApi.post<Record<string, unknown>>('/api/admin/invoices/review',
      { invoice_id: invoiceId, approve, reason }),

  /** ★ 开具（不可逆 —— 发票号占位不回收） */
  issue: (invoiceId: string, reason: string) =>
    adminApi.post<Record<string, unknown>>('/api/admin/invoices/issue',
      { invoice_id: invoiceId, reason }),

  /** ★ 作废（发票号不回收，订单释放供用户重开） */
  void: (invoiceId: string, reason: string) =>
    adminApi.post<Record<string, unknown>>('/api/admin/invoices/void',
      { invoice_id: invoiceId, reason }),

  /** 发票文件是**只读**端点（/api/invoices/{id}/file），不在 /admin 下 */
  downloadFile: (invoiceId: string) =>
    adminDownload(`/api/invoices/${encodeURIComponent(invoiceId)}/file`,
      `invoice_${invoiceId}.pdf`),

  exportLedger: (status = '') =>
    adminDownload('/api/admin/finance/export/invoices', `invoices_${today()}.csv`,
      status ? { status } : undefined),
}

/* ================================================================ 财务对账 */

export interface FinanceSummary {
  window: { start: string; end: string; days?: number }
  gross_cents: number
  refund_cents: number
  net_cents: number
  paid_orders: number
  refunded_orders: number
  pending_orders: number
  pending_cents: number
  failed_orders: number
  avg_order_cents: number
  by_channel: Array<{ channel: string; count: number; cents: number }>
  by_plan: Array<{ plan_key: string; count: number; cents: number }>
  daily: Array<{
    date: string
    paid_count: number
    paid_cents: number
    refund_count: number
    refund_cents: number
    net_cents: number
  }>
  alert?: string
}

export interface ReconcileResult {
  generated_at: string
  orders_vs_events: {
    checked_orders: number
    checked_events: number
    issues: number
    orphan_events: Array<{
      order_id: string; provider: string; amount_cents: number; created_at: string
    }>
    amount_mismatch: Array<{
      order_id: string; provider: string; order_cents: number
      event_cents: number[]; created_at: string
    }>
    paid_without_event: Array<{
      order_id: string; pay_channel: string; amount_cents: number; paid_at: string
    }>
    advice: string[]
  }
  credits: {
    checked: number
    unbalanced: number
    items: Array<{ user_id: string; diff: number }>
    advice: string[]
  }
  invoices: {
    checked: number
    problems: number
    items: Array<{
      invoice_id: string; invoice_no?: string; kind: string; advice: string
    }>
    advice: string[]
  }
}

export const financeApi = {
  summary: (days = 30) =>
    adminApi.get<FinanceSummary>('/api/admin/finance/summary', { days }),

  reconcile: (days = 30, limit = 300) =>
    adminApi.get<ReconcileResult>('/api/admin/finance/reconcile', { days, limit }),

  exportRevenue: (days = 30) =>
    adminDownload('/api/admin/finance/export/revenue', `revenue_${days}d.csv`, { days }),

  exportReconcile: (limit = 300) =>
    adminDownload('/api/admin/finance/export/reconcile', 'reconcile.csv', { limit }),

  exportBalances: (limit = 5000) =>
    adminDownload('/api/admin/finance/export/balances', 'balances.csv', { limit }),
}

/* ================================================================ 小工具 */

/** 导出文件名用的日期（本地时区，避免 UTC 导致"日期差一天"） */
export function today(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** 分转元（保留 2 位） */
export function fmtMoney(cents: number | null | undefined): string {
  return '¥' + (Number(cents || 0) / 100).toFixed(2)
}

/** 千分位 */
export function fmtNum(n: number | null | undefined): string {
  return Number(n || 0).toLocaleString('zh-CN')
}

/** 中文计数（亿 / 万） */
export function fmtWords(n: number | null | undefined): string {
  const v = Number(n || 0)
  if (v >= 1e8) return (v / 1e8).toFixed(2) + ' 亿'
  if (v >= 1e4) return (v / 1e4).toFixed(2) + ' 万'
  return fmtNum(v)
}
