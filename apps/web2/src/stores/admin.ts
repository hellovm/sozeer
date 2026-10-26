/**
 * stores/admin.ts · 后台会话状态（v0.26.2 G0-6）
 *
 * ★★ 核心职责：
 *   ① 管理后台自己的会话（与前台 store/auth.ts **完全隔离**）
 *   ② `can(perm)` —— 全项目**唯一**的权限判断入口
 *
 * ★★ 为什么 `can()` 绝不能比 role 字符串（MEMORY §29）：
 *   RBAC 下角色可自定义。运营可能建一个 `ops` 角色并授予 admin.access，
 *   若写 `role === 'admin'`，这个角色**永远看不到后台入口且不报错**。
 *   → 一律用权限点 + 通配符（`book.*` 匹配 `book.write`）。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  ADMIN_TOKEN_KEY, ADMIN_LAST_PAGE_KEY,
  clearAdminToken, getAdminToken, onAdminEvent, setAdminToken,
} from '@/api/admin'
import {
  adminLogin, adminMe,
  type AdminLoginResult, type AdminMe,
} from '@/api/admin_modules'

/** 后台会话（含权限点）*/
interface AdminSession {
  id: string
  username: string
  nickname: string
  role: string
  perms: string[]
  is_admin: boolean
}

export const useAdminStore = defineStore('admin', () => {
  /* ---------------------------------------------------------- 状态 */

  const token = ref<string>(getAdminToken())
  const session = ref<AdminSession | null>(null)
  const ready = ref(false)          // 是否已完成一次会话恢复
  const busy = ref(false)
  /** 顶栏提示：登录过期 / 权限不足（非弹窗，避免打断） */
  const notice = ref('')
  /**
   * ★ v1.0-E4.14 P0③：后台强制 2FA 拦截态。
   *   被中间件以 `code=2FA_REQUIRED` 拒之门外时置 true，
   *   顶栏据此显示带「去启用」按钮的引导卡（而不是泛泛的"权限不足"）。
   */
  const need2fa = ref(false)

  /* ─────────────── 编辑锁 + 自动刷新开关（v1.0-E4.15）───────────────
   * ★ 后台常驻页有 60s 自动刷新（数据要新鲜）。但若用户在配置表单里输入到一半，
   *   定时刷新会把整个页/表单状态冲掉 → 被迫重复配置（诉求 3）。
   *   ① 编辑锁：页内表单/弹窗打开期间 beginEdit()，关闭时 endEdit()，
   *     计数 > 0 时 AdminLayout 的自动刷新**跳过**，绝不丢编辑中的状态。
   *   ② 可关开关：用户可整体关掉自动刷新（持久化到 sessionStorage，刷新后保留选择）。
   */
  const editingCount = ref(0)
  const isEditing = computed(() => editingCount.value > 0)
  function beginEdit(): void { editingCount.value += 1 }
  function endEdit(): void { editingCount.value = Math.max(0, editingCount.value - 1) }

  const AUTO_REFRESH_KEY = 'nf_admin_auto_refresh'
  function _readAutoRefresh(): boolean {
    try {
      const v = sessionStorage.getItem(AUTO_REFRESH_KEY)
      return v === null ? true : v === '1'   // 默认开（后台常驻数据要新鲜）
    } catch { return true }
  }
  const autoRefresh = ref(_readAutoRefresh())
  function setAutoRefresh(on: boolean): void {
    autoRefresh.value = on
    try { sessionStorage.setItem(AUTO_REFRESH_KEY, on ? '1' : '0') } catch { /* ignore */ }
  }
  function toggleAutoRefresh(): void { setAutoRefresh(!autoRefresh.value) }

  /* ---------------------------------------------------------- 权限 */

  /**
   * ★★ 权限判断（全项目唯一入口）。
   *
   * 支持通配符：`book.*` 命中 `book.write` / `book.read`。
   * ★ 注意通配符用 `.` 分隔（`book.*` **不**匹配 `bookmark.read`）——
   *   用 fnmatch 的 `book*` 会前缀误匹配（MEMORY §25）。
   */
  function can(perm?: string | null): boolean {
    if (!perm) return false
    const list = session.value?.perms || []
    for (const p of list) {
      if (p === '*' || p === perm) return true
      if (p.endsWith('.*') && perm.startsWith(p.slice(0, -1))) return true
      if (p.endsWith('*') && perm.startsWith(p.slice(0, -1))) return true
    }
    return false
  }

  /** 便捷判断：是否已登录且具备后台访问权 */
  const isAuthed = computed(() => !!token.value && !!session.value)

  /* ---------------------------------------------------------- 登录 */

  /**
   * ★★ 管理员登录。
   *
   * ★ 判据是 **perms 含 admin.access**（不是 role 字符串）。
   *   同时兼容三种后端返回结构（v0.20.7 起改过两次）：
   *     ① `{token, perms, is_admin, user:{role}}`  —— 当前
   *     ② `{token, role}`                          —— 中间版本
   *     ③ 只有 role                                —— 更旧
   *   ★ 兼容是**必要**的：升级不同步时新版前端配旧版后端不应白屏。
   */
  async function login(username: string, password: string): Promise<void> {
    busy.value = true
    notice.value = ''
    try {
      const d: AdminLoginResult = await adminLogin(username, password)

      const permList = Array.isArray(d.perms) ? d.perms
        : (d.role === 'admin' || d.role === 'owner' ? ['admin.access'] : [])
      const canAdmin = d.is_admin === true
        || permList.includes('admin.access')
        || permList.includes('*')
        || d.role === 'admin' || d.role === 'owner'

      if (!canAdmin) {
        const who = d.role || '未知'
        throw new Error(
          `该账号没有后台访问权限（角色：${who}）。请联系所有者分配「访问管理后台」权限。`,
        )
      }

      const tk = d.access_token || d.token || ''
      if (!tk) throw new Error('登录响应缺少令牌，请检查后端版本')

      setAdminToken(tk)
      token.value = tk
      session.value = {
        id: '',
        username,
        nickname: '',
        role: d.role || d.user?.role || '',
        perms: permList,
        is_admin: canAdmin,
      }
      // 拉一次 me 补全用户名/昵称，并复核权限（★ 以服务端为准）
      await refresh()
    } finally {
      busy.value = false
    }
  }

  /**
   * 恢复 / 刷新会话（页面刷新后调用）。
   *
   * ★ 兜底策略：`/api/auth/me` 失败但 token 还在 → **仍然放行**。
   *   为什么：能拿到 token 说明刚登录成功过；此时若因网络抖动把人踢回登录页，
   *   体验很差。真正的鉴权由后端每个请求负责（拿不到数据会显示错误）。
   *   ★ 但 `can()` 会退化为空权限 → 页面显示"需要 xxx 权限"，不会越权。
   */
  async function refresh(): Promise<boolean> {
    if (!token.value) { ready.value = true; return false }
    try {
      const me: AdminMe = await adminMe()
      session.value = {
        id: me.id,
        username: me.username || session.value?.username || '',
        nickname: me.nickname || '',
        role: me.role || '',
        perms: Array.isArray(me.perms) && me.perms.length
          ? me.perms
          : (me.role === 'admin' || me.role === 'owner' ? ['admin.access'] : []),
        is_admin: me.is_admin === true
          || me.role === 'admin' || me.role === 'owner'
          || (me.perms || []).includes('admin.access'),
      }
      ready.value = true
      return true
    } catch (e) {
      // ★ 401 会被全局事件处理（清会话）；其他错误保留会话
      if ((e as { isUnauthorized?: boolean }).isUnauthorized) {
        session.value = null
        ready.value = true
        return false
      }
      if (!session.value) {
        session.value = {
          id: '', username: '', nickname: '', role: '',
          perms: [], is_admin: true,   // ★ 兜底：能进来说明后端放行过
        }
      }
      ready.value = true
      return true
    }
  }

  /* ---------------------------------------------------------- 登出 */

  function logout(reason = ''): void {
    clearAdminToken()
    token.value = ''
    session.value = null
    notice.value = reason
    need2fa.value = false
    try { sessionStorage.removeItem(ADMIN_LAST_PAGE_KEY) } catch { /* ignore */ }
  }

  /** 关闭 2FA 引导卡（用户手动关掉提示，但不一定已启用 TOTP） */
  function dismiss2fa(): void {
    need2fa.value = false
  }

  /* ---------------------------------------------------------- 记住页面 */

  function rememberPage(path: string): void {
    try { sessionStorage.setItem(ADMIN_LAST_PAGE_KEY, path) } catch { /* ignore */ }
  }

  function lastPage(): string {
    try { return sessionStorage.getItem(ADMIN_LAST_PAGE_KEY) || '' } catch { return '' }
  }

  /* ---------------------------------------------------------- 事件订阅 */

  /**
   * ★ 订阅请求层的鉴权事件（只订阅一次）。
   *
   * ★ 为什么用事件而不是让 http 层 import store：
   *   会形成 `store → api → store` 循环依赖，Vite 下表现为
   *   "运行时某个导出是 undefined"，很难定位。
   */
  let _bound = false
  function bindEvents(): void {
    if (_bound) return
    _bound = true

    onAdminEvent('unauthorized', ({ message }) => {
      // 401 = 会话失效 → 清会话，让路由守卫跳登录门
      logout(message || '登录已过期，请重新登录')
    })

    onAdminEvent('forbidden', ({ message, code }) => {
      // ★★ 403 **不能**清会话跳登录！
      //   403 是"权限不足"，用户仍然登录着。跳登录页会让人误判为"登录过期"，
      //   反复重登却永远进不去（无效操作循环）。
      if (code === '2FA_REQUIRED') {
        // ★ v1.0-E4.14 P0③：后台强制 2FA 拦截 → 专属引导（不显示"权限不足"）
        need2fa.value = true
        notice.value = ''
      } else {
        need2fa.value = false
        notice.value = `权限不足：${message}`
      }
    })
  }

  return {
    token, session, ready, busy, notice, need2fa,
    isAuthed, can,
    login, refresh, logout,
    dismiss2fa,
    rememberPage, lastPage, bindEvents,
    // ★ 编辑锁 + 自动刷新开关（v1.0-E4.15）
    isEditing, beginEdit, endEdit,
    autoRefresh, setAutoRefresh, toggleAutoRefresh,
  }
})
