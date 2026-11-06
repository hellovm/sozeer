/* ============================================================================
 * HTTP 请求层（G0-2）
 *
 * ★★ 这里承载了两条血泪教训（MEMORY.md §34）：
 *
 *   ① **401 必须区分来源**：
 *       · 登录/注册接口的 401 = 密码错误 → 要显示原因，**不能**清 token 跳登录
 *       · 其他接口的 401      = 会话失效 → 才清 token + 跳登录
 *      无差别处理会导致"点了登录没反应"（v0.21.1 实测踩坑）。
 *
 *   ② **token 存储要用 localStorage 而不是内存**：
 *      刷新页面不能掉登录态。
 *
 * ★ 另：token 自动刷新用**单例 Promise** 去重 ——
 *   并发 5 个请求同时 401 时，只应刷新一次，其余等这一次结果。
 *   否则会并发刷新 5 次，且后 4 次用的是已被轮换作废的 refresh_token
 *   → 触发服务端的**重放检测** → 整族令牌被吊销 → 用户被迫重登。
 * ========================================================================== */

import { ApiError, type ApiErrorBody } from '@/types/api'

const TOKEN_KEY = 'sozeer.token'
const REFRESH_KEY = 'sozeer.refresh'
const USER_KEY = 'sozeer.user'

/** ★ 这些路径的 401 属于"业务失败"，不做会话失效处理 */
const AUTH_ENDPOINTS = [
  '/api/auth/login',
  '/api/auth/register',
  '/api/auth/refresh',
]

export interface RequestOptions extends Omit<RequestInit, 'body'> {
  /** 请求体（对象自动 JSON 序列化；FormData 原样传） */
  body?: unknown
  /** 查询参数（会被序列化到 URL） */
  query?: Record<string, string | number | boolean | null | undefined>
  /** 超时毫秒（默认 30s；长任务接口可单独调大） */
  timeout?: number
  /** ★ 跳过自动附加 Authorization（如登录、公开接口） */
  skipAuth?: boolean
  /** ★ 跳过 401 自动刷新（避免刷新循环） */
  skipRefresh?: boolean
  /** 静默模式：不触发全局错误提示（由调用方自己处理） */
  silent?: boolean
}

/* ─────────────────────────── Token 存取 ─────────────────────────── */

export const tokenStore = {
  get(): string {
    try { return localStorage.getItem(TOKEN_KEY) || '' } catch { return '' }
  },
  getRefresh(): string {
    try { return localStorage.getItem(REFRESH_KEY) || '' } catch { return '' }
  },
  set(token: string, refresh = ''): void {
    try {
      localStorage.setItem(TOKEN_KEY, token)
      if (refresh) localStorage.setItem(REFRESH_KEY, refresh)
    } catch { /* 隐私模式下 localStorage 不可用，降级为内存态 */ }
  },
  clear(): void {
    try {
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(REFRESH_KEY)
      localStorage.removeItem(USER_KEY)
    } catch { /* ignore */ }
  },
  getUser(): unknown {
    try {
      const raw = localStorage.getItem(USER_KEY)
      return raw ? JSON.parse(raw) : null
    } catch { return null }
  },
  setUser(u: unknown): void {
    try { localStorage.setItem(USER_KEY, JSON.stringify(u)) } catch { /* ignore */ }
  },
}

/* ─────────────────────── 全局事件（解耦 store 与 http） ───────────────────────
 * ★ 为什么用事件而不是直接 import store：
 *   避免循环依赖（store 会 import http 的 api 方法）。
 */

type Listener = (payload?: unknown) => void
const listeners = new Map<string, Set<Listener>>()

export const bus = {
  on(evt: string, fn: Listener): () => void {
    if (!listeners.has(evt)) listeners.set(evt, new Set())
    listeners.get(evt)!.add(fn)
    return () => listeners.get(evt)?.delete(fn)
  },
  emit(evt: string, payload?: unknown): void {
    listeners.get(evt)?.forEach(fn => {
      try { fn(payload) } catch (e) { console.error('[bus]', evt, e) }
    })
  },
}

export const EVT = {
  /** 会话失效（需要清 token 跳登录） */
  AUTH_EXPIRED: 'auth:expired',
  /** 无权限（403） */
  FORBIDDEN: 'auth:forbidden',
  /** 被限流 */
  RATE_LIMITED: 'http:rate-limited',
  /** 全局错误提示 */
  ERROR: 'http:error',
} as const

/* ─────────────────────────── URL 组装 ─────────────────────────── */

function buildUrl(path: string, query?: RequestOptions['query']): string {
  if (!query) return path
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v === null || v === undefined) continue
    sp.append(k, String(v))
  }
  const qs = sp.toString()
  if (!qs) return path
  return path + (path.includes('?') ? '&' : '?') + qs
}

/* ─────────────────────────── 错误归一化 ─────────────────────────── */

function pickMessage(status: number, body: ApiErrorBody): string {
  const d = body?.detail
  if (typeof d === 'string' && d.trim()) return d
  // Pydantic 校验错误是数组
  if (Array.isArray(d) && d.length) {
    const first = d[0]
    return first?.msg ? `${first.msg}` : '请求参数有误'
  }
  return body?.message || body?.error || defaultMessage(status)
}

function defaultMessage(status: number): string {
  const map: Record<number, string> = {
    400: '请求参数有误',
    401: '未登录或登录已过期',
    403: '没有权限执行此操作',
    404: '资源不存在',
    409: '操作冲突，请刷新后重试',
    413: '内容过大',
    422: '请求参数校验失败',
    429: '操作过于频繁，请稍后再试',
    500: '服务器开小差了，请稍后重试',
    502: '网关异常，请稍后重试',
    503: '服务暂时不可用',
    504: '请求超时，请稍后重试',
  }
  if (map[status]) return map[status]
  if (status >= 500) return '服务器异常，请稍后重试'
  return `请求失败（HTTP ${status}）`
}

/* ─────────────────────────── Token 刷新（单例去重） ─────────────────────────── */

let refreshing: Promise<'ok' | 'hard' | 'soft'> | null = null

/**
 * 尝试用 refresh_token 换新令牌。
 * 返回值三态（★ v0.27.1 修复"等一段时间后自动退出"）：
 *   · 'ok'   —— 刷新成功，可安全重放原请求
 *   · 'hard' —— 服务端明确判定令牌失效/重用/过期 → 必须登出重登
 *   · 'soft' —— 临时故障（网络异常 / 429 限流 / 5xx）→ **不登出**，
 *               只把原 401 抛给调用方（下次请求会再试），避免抖动即踢人
 */
async function refreshToken(): Promise<'ok' | 'hard' | 'soft'> {
  // ★ 单例：并发请求只触发一次刷新
  if (refreshing) return refreshing

  refreshing = (async () => {
    const rt = tokenStore.getRefresh()
    if (!rt) return 'hard'
    try {
      const res = await fetch('/api/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: rt }),
      })
      if (res.status === 401) return 'hard'      // 服务端明确拒绝 → 重登
      if (!res.ok) return 'soft'                 // 429/5xx 等临时故障 → 不登出
      const data = await res.json()
      const newTok = data?.token || data?.access_token || ''
      if (!newTok) return 'hard'
      tokenStore.set(newTok, data?.refresh_token || rt)
      return 'ok'
    } catch {
      return 'soft'                              // 网络异常 → 不登出
    }
  })()

  try {
    return await refreshing
  } finally {
    // ★ 无论成功失败都要清掉，否则下次 401 时拿到的还是旧 Promise
    refreshing = null
  }
}

/* ─────────────────────────── 核心请求方法 ─────────────────────────── */

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const {
    body, query, timeout = 30000, skipAuth = false,
    skipRefresh = false, silent = false, headers, ...rest
  } = opts

  const isAuthEndpoint = AUTH_ENDPOINTS.some(p => path.startsWith(p))

  // 超时控制（AbortController）
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeout)

  const finalHeaders: Record<string, string> = {
    Accept: 'application/json',
    ...(headers as Record<string, string> | undefined),
  }

  let payload: BodyInit | undefined
  if (body instanceof FormData) {
    payload = body                       // ★ FormData 不设 Content-Type（让浏览器带 boundary）
  } else if (body !== undefined && body !== null) {
    finalHeaders['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }

  // ★ 除登录/注册等公开接口外，自动附加令牌
  let usedToken = ''
  if (!skipAuth) {
    const tok = tokenStore.get()
    usedToken = tok
    if (tok) finalHeaders.Authorization = `Bearer ${tok}`
  }

  let res: Response
  try {
    res = await fetch(buildUrl(path, query), {
      ...rest,
      headers: finalHeaders,
      body: payload,
      signal: ctrl.signal,
      // ★ 携带 cookie（若后端将来用 cookie 鉴权）
      credentials: 'same-origin',
    })
  } catch (e) {
    clearTimeout(timer)
    const aborted = (e as Error)?.name === 'AbortError'
    const err = new ApiError(
      aborted ? 504 : 0,
      aborted ? '请求超时，请检查网络后重试' : '网络异常，请检查网络连接',
      {},
      aborted ? 'timeout' : 'network',
    )
    if (!silent) bus.emit(EVT.ERROR, err)
    throw err
  } finally {
    clearTimeout(timer)
  }

  // ── 204 / 空响应 ──
  if (res.status === 204) return undefined as T

  const ct = res.headers.get('content-type') || ''
  const isJson = ct.includes('json')

  if (!res.ok) {
    let data: ApiErrorBody = {}
    try {
      data = isJson ? await res.json() : { detail: await res.text() }
    } catch { data = {} }

    const msg = pickMessage(res.status, data)
    const err = new ApiError(res.status, msg, data)

    // ★★ 关键分支（血泪教训 §34）
    if (res.status === 401) {
      if (isAuthEndpoint) {
        // 登录/注册接口的 401 = 凭据错误 → 交给调用方展示，**不动 token**
      } else if (!skipRefresh && !skipAuth) {
        // ★ 跨标签页协同（v0.27.1）：其他标签页可能已刷新并写入 localStorage。
        //   若 localStorage 的 token 已和本次请求用的不同 → 直接用新 token 重试，
        //   不再触发刷新（避免并发刷新触发服务端重放检测）。
        const cur = tokenStore.get()
        if (cur && cur !== usedToken) {
          return request<T>(path, { ...opts, skipRefresh: true })
        }
        const st = await refreshToken()
        if (st === 'ok') {
          // 刷新成功 → 重放原请求（用新 token）
          return request<T>(path, { ...opts, skipRefresh: true })
        }
        if (st === 'hard') {
          tokenStore.clear()
          bus.emit(EVT.AUTH_EXPIRED)
        }
        // soft（网络/429/5xx 临时故障）→ 不登出，原 401 抛给调用方
      } else {
        tokenStore.clear()
        bus.emit(EVT.AUTH_EXPIRED)
      }
    } else if (res.status === 403) {
      bus.emit(EVT.FORBIDDEN, err)
    } else if (res.status === 429) {
      bus.emit(EVT.RATE_LIMITED, err)
    }

    if (!silent && res.status !== 401 && res.status !== 403) {
      bus.emit(EVT.ERROR, err)
    }
    throw err
  }

  if (!isJson) return (await res.text()) as unknown as T
  try {
    return (await res.json()) as T
  } catch {
    return undefined as T
  }
}

/* ─────────────────────────── 便捷方法 ─────────────────────────── */

export const http = {
  get: <T>(path: string, query?: RequestOptions['query'], opts: RequestOptions = {}) =>
    request<T>(path, { ...opts, method: 'GET', query }),

  post: <T>(path: string, body?: unknown, opts: RequestOptions = {}) =>
    request<T>(path, { ...opts, method: 'POST', body }),

  put: <T>(path: string, body?: unknown, opts: RequestOptions = {}) =>
    request<T>(path, { ...opts, method: 'PUT', body }),

  patch: <T>(path: string, body?: unknown, opts: RequestOptions = {}) =>
    request<T>(path, { ...opts, method: 'PATCH', body }),

  del: <T>(path: string, opts: RequestOptions = {}) =>
    request<T>(path, { ...opts, method: 'DELETE' }),

  /** ★ SSE / 流式请求：返回原始 Response，由调用方读流 */
  async stream(path: string, body?: unknown, opts: RequestOptions = {}) {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(opts.headers as Record<string, string> | undefined),
    }
    const tok = tokenStore.get()
    if (tok) headers.Authorization = `Bearer ${tok}`
    return fetch(buildUrl(path, opts.query), {
      method: opts.method || 'POST',
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  },

  /** 文件下载（带鉴权头；★ 不能用 window.open，因为它不带 Authorization） */
  async download(path: string, query?: RequestOptions['query']): Promise<Blob> {
    const headers: Record<string, string> = {}
    const tok = tokenStore.get()
    if (tok) headers.Authorization = `Bearer ${tok}`
    const res = await fetch(buildUrl(path, query), { headers })
    if (!res.ok) {
      let data: ApiErrorBody = {}
      try { data = await res.json() } catch { /* ignore */ }
      throw new ApiError(res.status, pickMessage(res.status, data), data)
    }
    return res.blob()
  },
}

export default http
