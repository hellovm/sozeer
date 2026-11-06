/* ============================================================================
 * 路由表（G0-4）
 *
 * ★ 信息架构（方案 §1.3，导航 ≤5 项）：
 *   写书 / 短剧 / 素材库 / 数据 / (头像菜单)
 * ========================================================================== */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
// ★ 静态导入 store：动态 import 会与 App.vue 等处的静态导入产生重复 chunk 警告，
//   且 store 体积极小，没有必要为它做代码分割。
import { useAuthStore } from '@/stores/auth'
import { useAdminStore } from '@/stores/admin'

const routes: RouteRecordRaw[] = [
  /* ─────────── 营销页（v0.26.1 · 无需登录 · SEO 入口） ─────────── */
  {
    path: '/',
    component: () => import('@/layouts/MarketingLayout.vue'),
    children: [
      {
        path: '',
        name: 'intro',
        component: () => import('@/views/public/IntroView.vue'),
        meta: { public: true, mk: 'intro', title: '产品介绍' },
      },
      {
        path: 'tech',
        name: 'tech',
        component: () => import('@/views/public/TechView.vue'),
        meta: { public: true, mk: 'tech', title: '核心技术' },
      },
      {
        path: 'about',
        name: 'about',
        component: () => import('@/views/public/AboutView.vue'),
        meta: { public: true, mk: 'about', title: '关于我们' },
      },
      /* ★ v1.0-E4.11（合规/E1 备案）：协议页 —— 注册勾选的链接目标 */
      {
        path: 'terms',
        name: 'terms',
        component: () => import('@/views/public/TermsView.vue'),
        meta: { public: true, mk: 'terms', title: '用户协议' },
      },
      {
        path: 'privacy',
        name: 'privacy',
        component: () => import('@/views/public/PrivacyView.vue'),
        meta: { public: true, mk: 'privacy', title: '隐私政策' },
      },
    ],
  },

  /* ─────────── 公开页 ─────────── */
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/auth/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('@/views/auth/RegisterView.vue'),
    meta: { public: true, title: '注册' },
  },
  /* ★ v1.0-E4.16：/pricing 已移入营销布局分组（见上方）；此处不再有独立路由。 */

  /* ─────────── ★ 帮助中心（v1.0-E4.2 从 /app/help 提升为公开路由）───────────
   *
   * ★★ 为什么必须提升（两个真实缺陷）：
   *
   *   ① **链接 404**：原路由是 `/app` 的子路由（`/app/help`），
   *      但页脚/导航/命令面板都用 `{ name: 'help' }` 跳转 ——
   *      按名字跳转本身正确，**可直接访问 `/help` 却是 404**。
   *      用户从搜索引擎或分享链接进来就白屏。
   *
   *   ② **公开内容被登录拦截**：`/app` 整个分组有登录守卫，
   *      于是"帮助中心"这个**本该谁都能看**的页面，
   *      未登录访问会被弹到 `/login`。
   *      而帮助中心的**主要访客恰恰是"还没注册、想先了解"的人**。
   *
   * ★ 判据：**"要不要登录"应该由内容性质决定，不由它在导航里的位置决定**。
   *   帮助/价格/介绍/关于 = 获客入口，必须公开且可被搜索引擎收录。
   *
   * ★ 用 MarketingLayout 包裹：与其他公开页一致的页头页脚（含注册 CTA）。
   */
  {
    path: '/help',
    component: () => import('@/layouts/MarketingLayout.vue'),
    children: [
      {
        path: '',
        name: 'help',
        component: () => import('@/views/public/HelpView.vue'),
        meta: { public: true, mk: 'help', title: '帮助中心' },
      },
    ],
  },
  // ★ 兼容旧链接：`/app/help` 仍可访问（301 到新地址）
  {
    path: '/app/help',
    redirect: { name: 'help' },
  },

  /* ─────────── 主应用（需登录） ─────────── */
  // ★ v0.26.1：应用根路径从 `/` 改为 `/app`，把 `/` 让给营销介绍页。
  //   为什么不让两者共存于 `/`：Vue Router 按注册顺序匹配，同名 path 后者
  //   永远不可达（静默失效）。显式分开路径最清晰，且营销页能被搜索引擎收录。
  {
    path: '/app',
    component: () => import('@/layouts/AppLayout.vue'),
    children: [
      // 工作台首页（v0.27.0 起为 `/app`）
      {
        path: '',
        name: 'home',
        component: () => import('@/views/home/HomeView.vue'),
        meta: { title: '工作台', nav: 'home' },
      },

      /* ── 写书 ── */
      {
        path: 'books',
        name: 'books',
        component: () => import('@/views/books/BookListView.vue'),
        meta: { title: '我的作品', nav: 'books' },
      },
      {
        path: 'books/:bookId',
        name: 'studio',
        component: () => import('@/views/books/StudioView.vue'),
        meta: { title: '写作台', nav: 'books' },
      },
      {
        path: 'books/:bookId/chapters',
        name: 'chapters',
        component: () => import('@/views/books/ChapterListView.vue'),
        meta: { title: '章节', nav: 'books' },
      },
      {
        path: 'books/:bookId/chapters/:idx',
        name: 'chapter-edit',
        component: () => import('@/views/books/ChapterEditView.vue'),
        meta: { title: '章节编辑', nav: 'books' },
      },
      {
        path: 'books/:bookId/characters',
        name: 'characters',
        component: () => import('@/views/books/CharacterListView.vue'),
        meta: { title: '人物', nav: 'books' },
      },
      {
        path: 'books/:bookId/outline',
        name: 'outline',
        component: () => import('@/views/books/OutlineView.vue'),
        meta: { title: '大纲', nav: 'books' },
      },
      {
        path: 'books/:bookId/quality',
        name: 'quality',
        component: () => import('@/views/books/QualityView.vue'),
        meta: { title: '质检报告', nav: 'books' },
      },

      /* ── 短剧 ── */
      {
        path: 'drama',
        name: 'drama',
        component: () => import('@/views/drama/DramaListView.vue'),
        meta: { title: '短剧项目', nav: 'drama' },
      },
      {
        path: 'drama/:projectId',
        name: 'drama-detail',
        component: () => import('@/views/drama/DramaDetailView.vue'),
        meta: { title: '制作台', nav: 'drama' },
      },

      /* ── 素材库 ── */
      {
        path: 'assets/characters',
        name: 'asset-characters',
        component: () => import('@/views/assets/CharacterAssetsView.vue'),
        meta: { title: '角色资产', nav: 'assets' },
      },
      {
        path: 'assets/genres',
        name: 'asset-genres',
        component: () => import('@/views/assets/GenrePacksView.vue'),
        meta: { title: '题材包', nav: 'assets' },
      },

      /* ── 数据 ── */
      {
        path: 'data/usage',
        name: 'data-usage',
        component: () => import('@/views/data/UsageView.vue'),
        meta: { title: '用量与成本', nav: 'data' },
      },
      {
        path: 'data/stats',
        name: 'data-stats',
        component: () => import('@/views/data/StatsView.vue'),
        meta: { title: '写作统计', nav: 'data' },
      },

      /* ── 账户 ── */
      {
        path: 'account/profile',
        name: 'account-profile',
        component: () => import('@/views/account/ProfileView.vue'),
        meta: { title: '个人资料' },
      },
      {
        path: 'account/security',
        name: 'account-security',
        component: () => import('@/views/account/SecurityView.vue'),
        meta: { title: '安全设置' },
      },
      {
        path: 'account/notifications',
        name: 'account-notifications',
        component: () => import('@/views/account/NotificationView.vue'),
        meta: { title: '通知中心' },
      },
      // ★ v1.0-E4.2：`help` 已提升为顶层公开路由（见上方），此处移除。
      //   保留会导致**同名路由重复注册** —— Vue Router 会用后注册的覆盖，
      //   表现为"改了个寂寞"（新旧路由都指向同一个组件但 meta 不同）。
    ],
  },

  /* ─────────── 管理后台（v0.26.2 G0-6 · 独立会话 · 独立布局） ───────────
   *
   * ★★ 为什么用 `/admin` 子路径而不是复用前台布局：
   *   后台的会话（token）、导航、样式密度、权限模型都与前台不同。
   *   共用布局会让"前台用户"与"管理员"两种身份互相污染。
   *
   * ★★ 双轨到主域名（v0.27.0 已完成切换）：
   *   新版后台现在就在 `/admin`；旧版后台已降级到 `/legacy/admin`（回滚用）。
   */
  {
    path: '/admin',
    component: () => import('@/layouts/AdminLayout.vue'),
    children: [
      {
        path: '',
        name: 'admin-gate',
        component: () => import('@/views/admin/AdminGateView.vue'),
        meta: { public: true, title: '后台登录' },
      },
      {
        path: 'dashboard',
        name: 'admin-dashboard',
        component: () => import('@/views/admin/DashboardView.vue'),
        meta: { admin: true, title: '总览看板' },
      },
      {
        path: 'users',
        name: 'admin-users',
        component: () => import('@/views/admin/AdminUsersView.vue'),
        meta: { admin: true, title: '用户管理' },
      },
      // ★ v1.0-E4.3：套餐管理（后台配置价格体系）
      //   ★ 放在「订单财务」之后 —— 信息架构上"价格体系"属于财务域。
      /* ── 待迁移页面（G0-6 后续批次）───────────────────────────────
       * ★ 保持路由可访问并明确标注"迁移中"，比 404 更好：
       *   管理员不会以为"功能被删了"。 */
      {
        path: 'models',
        name: 'admin-models',
        component: () => import('@/views/admin/AdminModelsView.vue'),
        meta: { admin: true, title: '模型配置', requiredPerm: 'model.manage' },
      },
      {
        path: 'content',
        name: 'admin-content',
        component: () => import('@/views/admin/AdminContentView.vue'),
        meta: { admin: true, title: '内容治理' },
      },
      {
        path: 'announce',
        name: 'admin-announce',
        component: () => import('@/views/admin/AdminAnnounceView.vue'),
        meta: { admin: true, title: '公告广播', requiredPerm: 'config.update' },
      },
      {
        path: 'config',
        name: 'admin-config',
        component: () => import('@/views/admin/AdminConfigView.vue'),
        meta: { admin: true, title: '配置中心', requiredPerm: 'config.update' },
      },
      {
        path: 'queue',
        name: 'admin-queue',
        component: () => import('@/views/admin/AdminQueueView.vue'),
        meta: { admin: true, title: '任务队列' },
      },
      {
        path: 'rbac',
        name: 'admin-rbac',
        component: () => import('@/views/admin/AdminRbacView.vue'),
        meta: { admin: true, title: '权限管理' },
      },
      {
        path: 'risk',
        name: 'admin-risk',
        component: () => import('@/views/admin/AdminRiskView.vue'),
        meta: { admin: true, title: '风控中心' },
      },
      {
        path: 'audit',
        name: 'admin-audit',
        component: () => import('@/views/admin/AdminAuditView.vue'),
        meta: { admin: true, title: '审计日志', requiredPerm: 'audit.view' },
      },
      {
        path: 'health',
        name: 'admin-health',
        component: () => import('@/views/admin/AdminHealthView.vue'),
        meta: { admin: true, title: '系统状态', requiredPerm: 'system.health' },
      },
      {
        // ★★ v1.0-E4.7：数据洞察（提示词版本对比 + 采纳漏斗）
        //   ★ 为什么放在最后：它属于"分析类"页面，与运维/审计同区
        path: 'insight',
        name: 'admin-insight',
        component: () => import('@/views/admin/AdminInsightView.vue'),
        meta: { admin: true, title: '数据洞察', requiredPerm: 'audit.view' },
      },
    ],
  },

  /* ─────────── 错误页 ─────────── */
  {
    path: '/forbidden',
    name: 'forbidden',
    component: () => import('@/views/errors/ForbiddenView.vue'),
    meta: { public: true, title: '无权访问' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/errors/NotFoundView.vue'),
    meta: { public: true, title: '页面不存在' },
  },
]

const router = createRouter({
  // ★★ v0.27.0 主域名切换：新版已提升到 `/`，因此 base 是 '/'。
  //   history 模式需要服务端把所有未匹配路径回退到 index.html ——
  //   后端已用显式 catch-all 路由实现（main.py 的 `_new_spa`，注册在最后）。
  //   ★ 为什么不能用 StaticFiles mount：它**不做** SPA fallback。
  //   ★ 旧版回滚入口在 `/legacy/`（同一构建产物，路径前缀由后端决定）。
  history: createWebHistory('/'),
  routes,
  scrollBehavior(_to, _from, saved) {
    return saved || { top: 0 }
  },
})

/* ─────────────────────────── 全局守卫 ─────────────────────────── */

router.beforeEach(async (to) => {
  const isAdminRoute = Boolean(to.meta.admin) || to.path.startsWith('/admin')

  /* ══════════════ 后台分支（独立会话，不与前台互踢） ══════════════ */
  if (isAdminRoute) {
    const adm = useAdminStore()
    adm.bindEvents()                    // ★ 订阅 401/403 事件（幂等）

    if (!adm.ready) {
      await adm.refresh()
    }

    // 登录门本身无需鉴权
    if (to.name === 'admin-gate') {
      // 已登录 → 直接进看板（避免停留在登录门）
      if (adm.isAuthed) {
        return { name: 'admin-dashboard' }
      }
      return true
    }

    if (!adm.isAuthed) {
      // ★ 记住想去哪，登录后回跳（后台常用深链）
      adm.rememberPage(to.fullPath)
      return { name: 'admin-gate' }
    }

    // ★ 权限守卫：用权限点，绝不比 role 字符串（MEMORY §29）
    const need = to.meta.requiredPerm as string | string[] | undefined
    if (need) {
      const list = Array.isArray(need) ? need : [need]
      if (!list.some((p) => adm.can(p))) {
        return { name: 'forbidden', query: { need: list.join(','), from: to.fullPath } }
      }
    }
    return true
  }

  /* ══════════════ 前台分支 ══════════════ */
  const auth = useAuthStore()

  // ★ 首次进入先探测会话（刷新页面恢复登录态）
  if (!auth.ready) {
    await auth.bootstrap()
  }

  const isPublic = Boolean(to.meta.public)

  if (!isPublic && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  // 已登录还去登录页 → 回首页
  if (isPublic && auth.isLoggedIn && (to.name === 'login' || to.name === 'register')) {
    return { name: 'home' }
  }

  // ★ 权限守卫：路由声明 requiredPerm 时检查权限点（不比 role 字符串）
  const need = to.meta.requiredPerm as string | string[] | undefined
  if (need) {
    const list = Array.isArray(need) ? need : [need]
    if (!auth.canAny(...list)) {
      return { name: 'forbidden', query: { need: list.join(','), from: to.fullPath } }
    }
  }

  return true
})

router.afterEach((to) => {
  const t = (to.meta.title as string) || ''
  document.title = t ? `${t} · 溯字 SOZEER` : '溯字 SOZEER · AI 小说与短剧创作平台'
})

export default router
