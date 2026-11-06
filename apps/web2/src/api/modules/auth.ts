/* ============================================================================
 * API · 认证与会话
 * ========================================================================== */

import http, { tokenStore } from '@/api/http'
import type { CurrentUser, LoginResult } from '@/types/api'

export interface RegisterPayload {
  username: string
  password: string
  email?: string
  /** ★ v1.0-E4.11（合规）：协议同意声明（后端 False 直接 400） */
  agreed_terms?: boolean
  agreed_terms_version?: string
}

export const authApi = {
  login(username: string, password: string) {
    return http.post<LoginResult>('/api/auth/login', { username, password })
  },

  register(payload: RegisterPayload) {
    return http.post<LoginResult>('/api/auth/register', payload)
  },

  /**
   * ★ 当前用户（含权限点）
   *
   * ★★ 重要（v0.27.2 修复"刷新页面自动登出"）：
   *   后端 `/api/auth/me` 返回的是**扁平结构**
   *   `{id, username, role, email, perms, is_admin, site_name}`
   *   —— 后台 admin 也消费同一接口并按扁平字段解析，故这是既定契约，
   *   **不能把后端改成嵌套**（否则后台会崩）。
   *
   *   但前台 auth store 的 `setSession(r.user, r.perms)` 需要**嵌套**的
   *   `user` 对象。这里做归一化：把扁平响应包成 `{user, perms, role}`。
   *   同时兼容万一后端将来返回嵌套结构（r.user 存在时直接透传）。
   *
   *   ★ 根因：旧代码直接 `http.get('/api/auth/me')` 后读 `r.user`，
   *     而 r.user 永远是 undefined → 每次刷新 bootstrap 都拿不到用户 →
   *     路由守卫把人踢回登录页。
   */
  me() {
    return http.get<any>('/api/auth/me').then((r) => {
      if (!r) return r
      if (r.user) return r            // 已经是嵌套结构 → 直接透传
      return {
        user: {
          id: r.id,
          username: r.username,
          role: r.role,
          email: r.email,
        },
        perms: Array.isArray(r.perms) ? r.perms : [],
        role: r.role,
        is_admin: r.is_admin,
      }
    })
  },

  logout() {
    return http.post<{ ok: boolean }>('/api/auth/logout', {}, { silent: true })
  },

  /** 是否允许注册（首个用户自动成为管理员） */
  allowRegister() {
    return http.get<{
      allow: boolean
      first_user: boolean
      site_name: string
      site_tagline: string
    }>('/api/auth/allow-register', undefined, { skipAuth: true, silent: true })
  },

  /** ★ 登录成功后统一落盘（避免调用方各写一遍） */
  persist(r: LoginResult) {
    tokenStore.set(r.token || r.access_token, r.refresh_token)
    tokenStore.setUser(r.user)
  },
}

export default authApi
