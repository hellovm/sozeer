/**
 * admin.ts · 管理后台专用请求层（v0.26.2 G0-6）
 *
 * ★★★ 为什么后台要有**独立**的请求层，而不是复用 http.ts：
 *
 *   ① **令牌隔离**（最容易踩的坑）
 *      前台与后台可能在**同一个浏览器**里同时登录（开发者/运营常态）。
 *      若共用 `nf_token`，登录后台会覆盖前台令牌 → 前一个身份被踢下线；
 *      更糟的是登出后台会清掉前台令牌 → 用户以为"前台莫名其妙掉登录了"。
 *      → 后台用独立 key `nf_admin_token`，两套会话**互不影响**。
 *
 *   ② **错误语义不同**
 *      前台 401 = 会话失效跳登录页；
 *      后台 403 = **权限不足**（不是没登录！）→ 必须提示"需要 xxx 权限"，
 *      而不是把管理员踢回登录页（那会让人以为是登录过期，反复重登）。
 *
 *   ③ **不参与自动刷新令牌**
 *      后台是低频操作页，用短会话即可。刷新逻辑越简单越不容易出问题
 *      （前台那套单例去重 + 重放检测在后台没有收益，反而增加故障面）。
 *
 * ★ 路径前缀：后台 API 全部在 `/api/admin/*`（少数只读端点在 `/api/*`）。
 */
import type { ApiErrorBody } from '@/types/api'

/** ★ 独立的存储键 —— 绝不与前台 `nf_token` 共用 */
export const ADMIN_TOKEN_KEY = 'nf_admin_token'
/** 记住上次访问的后台页面（刷新后回到原处） */
export const ADMIN_LAST_PAGE_KEY = 'nf_admin_last_page'

export class AdminApiError extends Error {
  status: number
  /** 是否为权限不足（而非未登录）—— 用于区分 UI 提示 */
  isForbidden: boolean
  /** 未登录（需要跳登录门） */
  isUnauthorized: boolean
  detail: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'AdminApiError'
    this.status = status
    this.isForbidden = status === 403
    this.isUnauthorized = status === 401
    this.detail = detail
  }
}

/**
 * ★ 把后端 detail 归一化成人类可读字符串。
 *
 * 后端 detail 有三种形态：
 *   · 字符串（业务错误：400/401/403 多为这种）→ 原样
 *   · pydantic 数组（422/400 校验失败：[{loc,msg,type}]）→ 逐条拼"字段: 消息"
 *   · 对象 → 取 message/error，否则 JSON 截断
 *
 * ★ 教训（用户截图实锤）：detail 是数组时直接 `String(detail)` 会产出
 *   "[object Object],[object Object]" 天书 —— 必须专门格式化。
 */
function fmtDetail(d: unknown, fallback: string): string {
  if (typeof d === 'string' && d.trim()) return d
  if (Array.isArray(d) && d.length) {
    const parts = d.slice(0, 6).map((it): string => {
      const o = it as { loc?: unknown[]; msg?: string }
      const field = Array.isArray(o?.loc)
        ? o.loc.filter(Boolean).join('.')
        : ''
      const msg = o?.msg || '参数有误'
      return field ? `${field}: ${msg}` : msg
    })
    const tail = d.length > 6 ? `…（共 ${d.length} 项）` : ''
    return parts.join('；') + tail
  }
  if (d && typeof d === 'object') {
    try {
      const o = d as Record<string, unknown>
      const m = o.message || o.error
      if (typeof m === 'string' && m.trim()) return m
      const s = JSON.stringify(o) ?? ''
      return s.length > 120 ? `${s.slice(0, 120)}…` : s
    } catch { /* 循环引用等 → 落兜底 */ }
  }
  return fallback
}

/* ------------------------------------------------------------------ 事件总线 */

type AdminEvent = 'unauthorized' | 'forbidden'
type Handler = (payload: { path: string; message: string; code?: string }) => void

const _bus = new Map<AdminEvent, Set<Handler>>()

/**
 * ★ 用事件总线解耦（admin.ts 不 import store，避免循环依赖）。
 *   store 在初始化时订阅，收到 unauthorized 就清会话并跳登录门。
 */
export function onAdminEvent(ev: AdminEvent, fn: Handler): () => void {
  if (!_bus.has(ev)) _bus.set(ev, new Set())
  _bus.get(ev)!.add(fn)
  return () => { _bus.get(ev)?.delete(fn) }
}

function emit(ev: AdminEvent, payload: { path: string; message: string; code?: string }) {
  _bus.get(ev)?.forEach(fn => {
    try { fn(payload) } catch { /* 订阅方异常不能影响请求流程 */ }
  })
}

/* ------------------------------------------------------------------ 令牌 */

export function getAdminToken(): string {
  try { return localStorage.getItem(ADMIN_TOKEN_KEY) || '' } catch { return '' }
}

export function setAdminToken(t: string): void {
  try {
    if (t) localStorage.setItem(ADMIN_TOKEN_KEY, t)
    else localStorage.removeItem(ADMIN_TOKEN_KEY)
  } catch { /* 隐私模式下 localStorage 可能不可用 → 退化为内存会话 */ }
}

export function clearAdminToken(): void {
  setAdminToken('')
}

/* ------------------------------------------------------------------ 核心请求 */

interface AdminReqOpts {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  /** 查询参数（自动跳过 undefined / 空串，避免 `?keyword=` 污染缓存） */
  query?: Record<string, string | number | boolean | undefined | null>
  /** 超时（默认 30s；导出类接口需要更长） */
  timeoutMs?: number
  /** 不触发全局 401 事件（登录接口自身用） */
  silent401?: boolean
}

function buildUrl(path: string, query?: AdminReqOpts['query']): string {
  if (!query) return path
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue
    sp.append(k, String(v))
  }
  const qs = sp.toString()
  return qs ? `${path}?${qs}` : path
}

/**
 * 后台统一请求入口。
 *
 * ★ 与前台 http.ts 的区别（刻意不共用）：
 *   · 用 `nf_admin_token`
 *   · 403 不跳登录，而是抛 `isForbidden`（由调用方/页面显示权限提示）
 *   · 不做令牌刷新
 */
export async function adminReq<T = unknown>(
  path: string,
  opts: AdminReqOpts = {},
): Promise<T> {
  const { method = 'GET', body, query, timeoutMs = 30_000, silent401 } = opts
  const url = buildUrl(path, query)

  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), timeoutMs)

  const headers: Record<string, string> = { Accept: 'application/json' }
  const tk = getAdminToken()
  if (tk) headers.Authorization = `Bearer ${tk}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let res: Response
  try {
    res = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctl.signal,
      credentials: 'omit',        // ★ 后台不用 cookie（避免与前台会话混淆）
    })
  } catch (e) {
    clearTimeout(timer)
    if ((e as Error).name === 'AbortError') {
      throw new AdminApiError(0, `请求超时（${Math.round(timeoutMs / 1000)}s）：${path}`)
    }
    throw new AdminApiError(0, `网络错误：${(e as Error).message}`)
  } finally {
    clearTimeout(timer)
  }

  // ── 解析响应（★ 兼容 JSON / 纯文本 / 空体）
  const ct = res.headers.get('content-type') || ''
  let payload: unknown = null
  if (ct.includes('application/json')) {
    const text = await res.text()
    if (text.trim()) {
      try { payload = JSON.parse(text) } catch { payload = { raw: text } }
    }
  } else {
    payload = await res.text()
  }

  if (res.ok) return payload as T

  // ── 错误分类（★ 这里是后台与前台的**关键差异**）
  const detail = (payload as ApiErrorBody | null)?.detail
    ?? (payload as { message?: string } | null)?.message
    ?? (typeof payload === 'string' && payload ? payload : '')
  // ★ v1.0-E4.14 P0③：后端 403 可带结构化 code（如 2FA_REQUIRED / IP_WHITELIST）。
  //   透传给事件，store 据此做专属引导（而非泛泛的"权限不足"）。
  const code: string | undefined =
    (payload as { code?: string } | null)?.code ?? undefined

  if (res.status === 401) {
    const msg401 = fmtDetail(detail, '登录已过期')
    if (!silent401) {
      emit('unauthorized', { path, message: msg401 })
    }
    throw new AdminApiError(401, msg401, detail)
  }

  if (res.status === 403) {
    // ★★ 403 是"权限不足/安全门禁"，**不是**"未登录"。
    //   绝不能清 token 跳登录页 —— 那会让管理员以为登录过期，反复重登。
    const msg = fmtDetail(detail, '权限不足')
    emit('forbidden', { path, message: msg, code })
    throw new AdminApiError(403, msg, detail)
  }

  throw new AdminApiError(
    res.status,
    fmtDetail(detail, `请求失败（HTTP ${res.status}）`),
    detail,
  )
}

/* ------------------------------------------------------------------ 文件下载 */

/**
 * 带鉴权的文件下载（导出 CSV / 下载发票）。
 *
 * ★ 为什么不能用 <a href> 或 window.open：
 *   后台令牌在 localStorage，不在 cookie 里 → 直接开链接**不带 Authorization**
 *   → 服务端 401（表现为"点了导出没反应"）。必须走 fetch + Blob。
 */
export async function adminDownload(
  path: string,
  filename: string,
  query?: AdminReqOpts['query'],
): Promise<void> {
  const url = buildUrl(path, query)
  const tk = getAdminToken()
  const res = await fetch(url, {
    headers: tk ? { Authorization: `Bearer ${tk}` } : {},
    credentials: 'omit',
  })
  if (!res.ok) {
    const fallback = `下载失败（HTTP ${res.status}）`
    let msg = fallback
    try {
      const j = await res.json()
      msg = fmtDetail(j?.detail, fallback)
    } catch { /* 非 JSON 响应，保留默认文案 */ }
    throw new AdminApiError(res.status, msg)
  }
  const blob = await res.blob()
  const href = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = href
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  // ★ 延迟释放：立即 revoke 在部分浏览器会中断下载
  setTimeout(() => URL.revokeObjectURL(href), 4000)
}

/* ------------------------------------------------------------------ 便捷方法 */

export const adminApi = {
  get: <T = unknown>(path: string, query?: AdminReqOpts['query']) =>
    adminReq<T>(path, { method: 'GET', query }),
  post: <T = unknown>(path: string, body?: unknown) =>
    adminReq<T>(path, { method: 'POST', body }),
  patch: <T = unknown>(path: string, body?: unknown) =>
    adminReq<T>(path, { method: 'PATCH', body }),
  put: <T = unknown>(path: string, body?: unknown) =>
    adminReq<T>(path, { method: 'PUT', body }),
  del: <T = unknown>(path: string, query?: AdminReqOpts['query']) =>
    adminReq<T>(path, { method: 'DELETE', query }),
}
