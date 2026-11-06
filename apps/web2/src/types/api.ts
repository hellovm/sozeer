/* ============================================================================
 * 类型定义：API 通用契约
 * ========================================================================== */

/** 后端统一错误体（FastAPI HTTPException 的 detail） */
export interface ApiErrorBody {
  detail?: string | Array<{ loc: string[]; msg: string; type: string }>
  message?: string
  error?: string
  retry_after?: number
  hint?: string
}

/** 分页响应通用形状（后端各列表接口不完全统一，这里是常见形态） */
export interface Paged<T> {
  total: number
  items: T[]
}

/** 当前登录用户 */
export interface CurrentUser {
  id: string
  username: string
  role: string
  nickname?: string
  avatar?: string
  email?: string
}

/** 登录响应（v0.20.7 起为嵌套结构；v0.25.5 起顶层冗余 role/perms） */
export interface LoginResult {
  ok: boolean
  user: CurrentUser
  role: string
  /** ★ 权限点（RBAC 展开）—— 前端判断权限**必须**用这个，不要比 role 字符串 */
  perms: string[]
  is_admin: boolean
  token: string
  access_token: string
  access_ttl: number
  refresh_token: string
  risk_level?: string
  risk_notes?: string[]
}

/** 业务错误（由 http 层抛出，带 HTTP 状态码便于调用方分支处理） */
export class ApiError extends Error {
  status: number
  code: string
  body: ApiErrorBody

  constructor(status: number, message: string, body: ApiErrorBody = {}, code = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
    this.code = code
  }

  /** 是否为"未登录/会话失效"（★ 注意：登录接口本身的 401 不算） */
  get isAuthExpired(): boolean {
    return this.status === 401
  }

  /** 是否为"无权限" */
  get isForbidden(): boolean {
    return this.status === 403
  }

  /** 是否为"被限流" */
  get isRateLimited(): boolean {
    return this.status === 429
  }

  /** 是否为"资源不存在" */
  get isNotFound(): boolean {
    return this.status === 404
  }
}
