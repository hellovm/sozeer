/* ============================================================================
 * Store · 会话与身份（auth）
 *
 * ★★ 核心设计：权限判断只看 `perms`（权限点），**绝不比 role 字符串**
 *   （MEMORY.md §29：`d.role !== 'admin'` 会让 owner / 自定义角色永久不可用）
 * ========================================================================== */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import authApi from '@/api/modules/auth'
import { tokenStore } from '@/api/http'
import type { CurrentUser } from '@/types/api'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<CurrentUser | null>(null)
  const perms = ref<string[]>([])
  const role = ref('')
  const ready = ref(false)          // 是否已完成一次会话探测
  const loading = ref(false)

  const isLoggedIn = computed(() => Boolean(user.value?.id))

  /**
   * ★ 权限判断（唯一入口）
   * 支持通配符：`book.*` 匹配 `book.read`，但**不匹配** `bookmark.read`
   * （用 '.' 分隔，避免 fnmatch 的前缀误匹配）
   */
  function can(perm: string): boolean {
    if (!perm) return false
    for (const p of perms.value) {
      if (p === '*') return true
      if (p === perm) return true
      if (p.endsWith('.*')) {
        const prefix = p.slice(0, -1)          // 'book.'
        if (perm.startsWith(prefix)) return true
      }
    }
    return false
  }

  function canAny(...list: string[]): boolean {
    return list.some(can)
  }

  function setSession(u: CurrentUser, p: string[]) {
    user.value = u
    perms.value = p
    role.value = u.role || ''
    tokenStore.setUser(u)
  }

  function clearSession() {
    user.value = null
    perms.value = []
    role.value = ''
    tokenStore.clear()
  }

  /** 登录 */
  async function login(username: string, password: string) {
    loading.value = true
    try {
      const r = await authApi.login(username, password)
      authApi.persist(r)
      setSession(r.user, r.perms || [])
      return r
    } finally {
      loading.value = false
    }
  }

  /** 注册（首个用户自动成为管理员） */
  async function register(username: string, password: string, email?: string) {
    loading.value = true
    try {
      // ★ v1.0-E4.11（合规）：显式声明已同意协议（与注册页勾选框联动；
      //   后端缺省 True 兼容老客户端，新前端必须显式带这个字段）
      const r = await authApi.register({ username, password, email, agreed_terms: true, agreed_terms_version: 'v1' })
      if (r?.token || r?.access_token) {
        authApi.persist(r)
        setSession(r.user, r.perms || [])
      }
      return r
    } finally {
      loading.value = false
    }
  }

  /**
   * ★ 启动时探测会话（刷新页面后恢复登录态）
   * 失败不抛异常 —— 静默进入未登录状态即可（首屏不该被 401 打断）
   */
  async function bootstrap() {
    if (ready.value) return
    try {
      if (tokenStore.get()) {
        const r = await authApi.me()
        setSession(r.user, r.perms || [])
      }
    } catch {
      clearSession()
    } finally {
      ready.value = true
    }
  }

  /** 刷新权限点（权限被管理员改动后调用） */
  async function refreshPerms() {
    try {
      const r = await authApi.me()
      setSession(r.user, r.perms || [])
    } catch { /* ignore */ }
  }

  async function logout() {
    try {
      if (tokenStore.get()) await authApi.logout()
    } catch { /* ★ 登出失败也要清本地态，否则用户"退不出去" */ }
    clearSession()
  }

  return {
    user, perms, role, ready, loading,
    isLoggedIn, can, canAny,
    setSession, clearSession, login, register, logout,
    bootstrap, refreshPerms,
  }
})
