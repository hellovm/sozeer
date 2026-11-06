<script setup lang="ts">
/**
 * AdminLayout.vue · 后台外壳（v0.26.2 G0-6）
 *
 * ★ 设计要点：
 *   ① **导航按权限过滤** —— 无权的项直接不渲染（而不是点了报错）。
 *      判据用 `can('xxx')`（权限点），不比 role 字符串。
 *   ② 信息密度比前台高：小字号 + 紧凑间距（后台是"干活"的地方）
 *   ③ 顶栏显示"数据更新时间" + 手动刷新（后台是常驻页，数据要新鲜）
 *   ④ ★ 登录门不套外壳（AdminGateView 全屏），所以这里判断是否渲染侧栏
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { useAdminStore } from '@/stores/admin'
import AppToastHost from '@/components/AppToastHost.vue'

const route = useRoute()
const router = useRouter()
const adm = useAdminStore()

const sideOpen = ref(false)

/**
 * ★★ 导航定义是**唯一声明处**：标题、路径、所需权限都在这。
 *   侧栏渲染、页面标题、权限过滤都从这里派生 —— 避免三处不同步。
 */
interface NavItem {
  name: string
  label: string
  /** 需要的权限点；不填 = 所有管理员可见 */
  perm?: string
}

/**
 * 后台导航。
 *
 * ★★ v1.0-E4.2：**移除 `pending` 标记**。
 *
 *   历史背景：G0-6 迁移期用 `pending: true` 标出"该页还在用旧版后台"，
 *   在导航上显示一个「旧」徽标。G0-6 已收官（15/15 页全部迁移完成），
 *   但这三个标记**忘了清** →
 *
 *   · 导航一直显示「旧」，tooltip 写"暂用旧版后台"
 *   · ★ 用户因此以为功能缺失 —— 实际 `AdminModelsView`(974行) /
 *     `AdminConfigView`(462行) **早就实现完整**
 *   · 严重误导：明明是完整新版，却被自己标成"旧的"
 *
 *   ★ 教训：**迁移标记必须与迁移状态同源**，不能靠人记着删
 *     （与 MEMORY §59「检查对象没更新」同源）。
 *     这类标记要么自动派生，要么迁移完成时立即清理。
 */
const NAV: NavItem[] = [
  { name: 'admin-dashboard', label: '总览看板' },
  { name: 'admin-users', label: '用户管理' },
  { name: 'admin-models', label: '模型配置', perm: 'model.manage' },
  { name: 'admin-content', label: '内容治理' },
  { name: 'admin-announce', label: '公告广播', perm: 'config.update' },
  { name: 'admin-config', label: '配置中心', perm: 'config.update' },
  { name: 'admin-queue', label: '任务队列' },
  { name: 'admin-rbac', label: '权限管理' },
  { name: 'admin-risk', label: '风控中心' },
  { name: 'admin-audit', label: '审计日志', perm: 'audit.view' },
  { name: 'admin-health', label: '系统状态', perm: 'system.health' },
  // ★ v1.0-E4.7：数据洞察（提示词版本对比 + 采纳漏斗）
  { name: 'admin-insight', label: '数据洞察', perm: 'audit.view' },
]

/** ★ 权限过滤：无权项不渲染（点了报错是最差的交互） */
const navItems = computed(() =>
  NAV.filter((n) => !n.perm || adm.can(n.perm)))

const pageTitle = computed(() => {
  const t = route.meta.title as string | undefined
  return t || '管理后台'
})

const isGate = computed(() => route.name === 'admin-gate')

/** 顶栏时间戳（每次路由变化刷新） */
const stamp = ref('')
watch(
  () => route.fullPath,
  () => {
    stamp.value = new Date().toLocaleTimeString('zh-CN')
    sideOpen.value = false           // 移动端切页自动收起侧栏
  },
  { immediate: true },
)

/* ── 自动刷新：后台常驻，数据要新鲜（60s） ──
   ★★ v1.0-E4.15：从 `router.go(0)` **硬重载**改为**软刷新**（bump `_r`）。
   硬重载会把整个 SPA 的内存状态（输入到一半的表单/弹窗）全冲掉 →
   用户被迫重复配置（诉求 3）。软刷新只让当前页 watch 到 _r 变化重拉数据，
   组件实例与本地草稿都保留。
   ① 自动刷新开关关闭 → 跳过（用户可整体关掉，不想要定时刷新）
   ② 编辑锁 isEditing（表单/弹窗打开）→ 跳过，绝不冲掉输入中的状态 */
let timer: number | undefined
function startAuto() {
  stopAuto()
  timer = window.setInterval(() => {
    if (route.name && route.name !== 'admin-gate' && document.visibilityState === 'visible') {
      if (!adm.autoRefresh) return        // 用户关掉了自动刷新
      if (adm.isEditing) return          // 编辑中（表单/弹窗打开）→ 跳过，保草稿
      refreshPage()                       // 软刷新（不再 router.go(0) 硬重载）
    }
  }, 60_000)
}
function stopAuto() {
  if (timer !== undefined) { window.clearInterval(timer); timer = undefined }
}
startAuto()
onBeforeUnmount(stopAuto)

function doLogout() {
  adm.logout()
  router.push({ name: 'admin-gate' })
}

function refreshPage() {
  // 触发子组件重载：用 replace 一个带时间戳的 query（子页面 watch 它）
  router.replace({ query: { ...route.query, _r: String(Date.now()) } })
}

/* ── ★ v1.0-E4.14 P0③：后台强制 2FA 拦截引导 ──
 * 被中间件 403(code=2FA_REQUIRED) 拦下时，topbar 下显示整宽横幅：
 *   说明「管理员已开启后台强制 2FA」+ 指引去前台「安全设置」启用 TOTP。
 * 前台与后台会话隔离（不同 token key），启用 TOTP 需在**前台**身份下操作，
 * 故横幅给「去前台启用」主按钮（跳前台安全页），不在此处代填验证码。 */
function goEnable2fa() {
  adm.dismiss2fa()
  router.push({ name: 'account-security' })
}
</script>

<template>
  <!-- ═══ 登录门：全屏，不套外壳 ═══ -->
  <RouterView v-if="isGate" />

  <!-- ═══ 主壳 ═══ -->
  <div v-else class="admin">
    <!-- 移动端遮罩 -->
    <div v-if="sideOpen" class="scrim" @click="sideOpen = false" />

    <aside class="side" :class="{ open: sideOpen }">
      <div class="brand">
        <span class="brand__cn">溯字</span>
        <span class="brand__en">ADMIN</span>
      </div>

      <nav class="nav">
        <RouterLink
          v-for="n in navItems"
          :key="n.name"
          class="nav__item"
          :class="{ on: route.name === n.name }"
          :to="{ name: n.name }"
        >
          <span class="nav__label">{{ n.label }}</span>
        </RouterLink>
      </nav>

      <div class="side__foot">
        <div v-if="adm.session" class="who">
          <div class="who__name">{{ adm.session.username || '—' }}</div>
          <div class="who__role">{{ adm.session.role || '' }}</div>
        </div>
        <button class="btn-mini" @click="doLogout">退出</button>
      </div>
    </aside>

    <main class="main">
      <header class="topbar">
        <button class="menu-btn" aria-label="打开菜单" @click="sideOpen = !sideOpen">☰</button>
        <h1 class="topbar__title">{{ pageTitle }}</h1>
        <div class="topbar__right">
          <!-- ★ 403 提示条（不弹窗打断，留在顶栏） -->
          <span v-if="adm.notice" class="notice" :title="adm.notice">
            {{ adm.notice }}
          </span>
          <span class="stamp">{{ stamp }}</span>
          <button class="btn-mini" @click="refreshPage">刷新</button>
          <!-- ★ v1.0-E4.15：自动刷新开关（关后 60s 定时刷新不再触发，编辑更安心） -->
          <button class="btn-mini" :class="{ off: !adm.autoRefresh }"
                  :title="adm.autoRefresh ? '自动刷新已开启（每 60s），点击关闭' : '自动刷新已关闭，点击开启'"
                  @click="adm.toggleAutoRefresh()">
            自动{{ adm.autoRefresh ? '开' : '关' }}
          </button>
          <RouterLink class="btn-mini" :to="{ name: 'intro' }" target="_blank">前台</RouterLink>
        </div>
      </header>

      <!-- ★ v1.0-E4.14 P0③：后台强制 2FA 拦截引导（整宽横幅，不弹窗） -->
      <div v-if="adm.need2fa" class="tfa-banner">
        <span class="tfa-banner__ico">🛡️</span>
        <div class="tfa-banner__body">
          <strong>该账号需先启用两步验证（2FA）才能进入后台。</strong>
          <span class="tfa-banner__sub">
            管理员已开启「后台强制 2FA」。请在前台「安全设置」完成 TOTP 绑定后返回。
          </span>
        </div>
        <button class="tfa-banner__btn" @click="goEnable2fa">去前台启用</button>
        <button class="tfa-banner__close" aria-label="关闭提示" @click="adm.dismiss2fa()">×</button>
      </div>

      <div class="view">
        <RouterView />
      </div>
    </main>

    <AppToastHost />
  </div>
</template>

<style scoped>
/* ★ 后台用独立的密度：字号更小、间距更紧（信息密度换可读性） */
.admin {
  display: flex;
  min-height: 100vh;
  background: var(--bg);
  color: var(--text);
}

/* ── 侧栏 ── */
.side {
  width: 196px;
  flex: none;
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  position: sticky;
  top: 0;
  height: 100vh;
}
.brand {
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.brand__cn { font-size: 15px; font-weight: 700; letter-spacing: .06em; }
.brand__en { font-size: 10px; color: var(--brand); letter-spacing: .16em; }

.nav { flex: 1; overflow-y: auto; padding: 8px 0; }
.nav__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 8px 16px;
  font-size: 13px;
  color: var(--text-muted);
  text-decoration: none;
  border-left: 2px solid transparent;
}
.nav__item:hover { background: var(--surface-2); color: var(--text); }
.nav__item.on {
  color: var(--brand);
  border-left-color: var(--brand);
  background: color-mix(in srgb, var(--brand) 8%, transparent);
  font-weight: 600;
}
/* ★ v1.0-E4.2：`.nav__badge` 样式已删除 —— 迁移标记移除后不再有徽标。
   留着会让后来者以为"还有个徽标只是没渲染"，徒增困惑。 */

.side__foot {
  padding: 10px 14px;
  border-top: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.who { min-width: 0; }
.who__name { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.who__role { font-size: 10px; color: var(--text-faint); }

/* ── 主区 ── */
.main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 18px;
  height: 48px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  position: sticky;
  top: 0;
  z-index: 5;
}
.topbar__title { font-size: 15px; font-weight: 600; flex: 1; margin: 0; }
.topbar__right { display: flex; align-items: center; gap: 10px; }
.stamp { font-size: 11px; color: var(--text-faint); font-variant-numeric: tabular-nums; }
.notice {
  font-size: 11px;
  color: var(--danger, #d33);
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ★ v1.0-E4.14 P0③：后台强制 2FA 拦截引导横幅（整宽，不弹窗） */
.tfa-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  background: color-mix(in srgb, var(--danger, #d33) 8%, var(--surface));
  border-bottom: 1px solid var(--border);
  font-size: 13px;
}
.tfa-banner__ico { font-size: 18px; flex: none; }
.tfa-banner__body { flex: 1; min-width: 0; }
.tfa-banner__sub { display: block; font-size: 11px; color: var(--text-muted); margin-top: 2px; }
.tfa-banner__btn {
  flex: none;
  font-size: 12px;
  padding: 5px 12px;
  border: none;
  border-radius: 6px;
  background: var(--danger, #d33);
  color: #fff;
  cursor: pointer;
  font-family: inherit;
}
.tfa-banner__btn:hover { opacity: .9; }
.tfa-banner__close {
  flex: none;
  background: none;
  border: none;
  font-size: 18px;
  color: var(--text-faint);
  cursor: pointer;
  padding: 0 4px;
  line-height: 1;
}
.tfa-banner__close:hover { color: var(--text); }

.view { flex: 1; padding: 18px; min-width: 0; }

.btn-mini {
  font-size: 11px;
  padding: 4px 9px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text-muted);
  cursor: pointer;
  font-family: inherit;
  text-decoration: none;
}
.btn-mini:hover { color: var(--text); border-color: var(--brand); }
/* ★ v1.0-E4.15：自动刷新开关关闭态 —— 置灰提醒当前不会自动刷新 */
.btn-mini.off { color: var(--text-faint); opacity: .7; }

.menu-btn { display: none; background: none; border: none; font-size: 17px;
  color: var(--text); cursor: pointer; padding: 0 4px; }
.scrim { display: none; }

@media (max-width: 860px) {
  .side {
    position: fixed;
    left: 0; top: 0; bottom: 0;
    z-index: 20;
    transform: translateX(-100%);
    transition: transform .18s ease;
    box-shadow: 0 0 24px rgba(0,0,0,.18);
  }
  .side.open { transform: translateX(0); }
  .menu-btn { display: block; }
  .scrim { display: block; position: fixed; inset: 0; z-index: 15;
    background: rgba(0,0,0,.32); }
  .view { padding: 12px; }
}
</style>
