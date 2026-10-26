<script setup lang="ts">
/**
 * AppLayout · 主应用布局（G0-4）
 *
 * 结构：TopNav（5 项主导航）+ SideNav（二级）+ 内容区
 * ★ 导航 ≤5 项（方案 P2）；动词优先（P3）
 */
import { computed } from 'vue'
// ★ v1.0-E4.2：显式导入 RouterView（模板里在用）。
//   虽然 `app.use(router)` 会全局注册它，运行时不报错，
//   但**显式导入**有三个好处：
//     ① 类型检查能覆盖到（全局注册的组件模板类型是 any）
//     ② 阅读代码时"用了什么"一目了然，不必去猜是否全局注册
//     ③ 与 MarketingLayout / AdminLayout 保持一致（三处同构）
import { RouterView, useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const ui = useUiStore()

/** ★ 5 项主导航（含首页） */
const NAV = [
  { key: 'home', label: '工作台', icon: '◎', to: { name: 'home' } },
  { key: 'books', label: '写书', icon: '✎', to: { name: 'books' } },
  { key: 'drama', label: '短剧', icon: '▶', to: { name: 'drama' } },
  { key: 'assets', label: '素材库', icon: '❖', to: { name: 'asset-characters' } },
  { key: 'data', label: '数据', icon: '▤', to: { name: 'data-usage' } },
]

const activeNav = computed(() => (route.meta.nav as string) || 'home')

const initials = computed(() => {
  const n = auth.user?.nickname || auth.user?.username || 'U'
  return n.slice(0, 1).toUpperCase()
})

async function onLogout() {
  await auth.logout()
  ui.ok('已退出登录')
  router.push({ name: 'login' })
}
</script>

<template>
  <div class="layout">
    <!-- ── 顶栏 ── -->
    <header class="topnav">
      <RouterLink class="topnav__brand" :to="{ name: 'home' }">
        <span class="topnav__logo" aria-hidden="true">溯</span>
        <span class="topnav__name">溯字 <em>SOZEER</em></span>
      </RouterLink>

      <nav class="topnav__nav" aria-label="主导航">
        <RouterLink
          v-for="n in NAV"
          :key="n.key"
          class="topnav__item"
          :class="{ 'is-active': activeNav === n.key }"
          :to="n.to"
        >
          <span class="topnav__ico" aria-hidden="true">{{ n.icon }}</span>
          <span>{{ n.label }}</span>
        </RouterLink>
      </nav>

      <div class="topnav__right">
        <button
          class="topnav__icon-btn"
          type="button"
          title="命令面板（⌘K）"
          aria-label="打开命令面板"
          @click="ui.openCommand()"
        >⌘K</button>

        <button
          class="topnav__icon-btn"
          type="button"
          :aria-label="ui.isDark ? '切换到亮色主题' : '切换到暗色主题'"
          @click="ui.toggleTheme()"
        >{{ ui.isDark ? '☀' : '☾' }}</button>

        <div class="topnav__user">
          <button class="topnav__avatar" type="button" :aria-label="`账号菜单：${auth.user?.username}`">
            {{ initials }}
          </button>
          <div class="menu" role="menu">
            <div class="menu__head">
              <strong>{{ auth.user?.nickname || auth.user?.username }}</strong>
              <span class="muted tiny">{{ auth.role || '—' }}</span>
            </div>
            <RouterLink class="menu__item" :to="{ name: 'account-profile' }" role="menuitem">个人资料</RouterLink>
            <RouterLink class="menu__item" :to="{ name: 'account-security' }" role="menuitem">安全设置</RouterLink>
            <RouterLink class="menu__item" :to="{ name: 'account-notifications' }" role="menuitem">通知中心</RouterLink>
            <RouterLink class="menu__item" :to="{ name: 'help' }" role="menuitem">帮助中心</RouterLink>
            <!-- ★ 用权限位判断，不比 role 字符串 -->
            <a v-if="auth.can('admin.access')" class="menu__item menu__item--accent" href="/admin/" target="_blank" rel="noopener">管理后台 ↗</a>
            <div class="menu__divider" />
            <button class="menu__item menu__item--danger" type="button" role="menuitem" @click="onLogout">退出登录</button>
          </div>
        </div>
      </div>
    </header>

    <!-- ── 全局 Banner（额度不足等常驻提示） ── -->
    <div v-if="ui.banners.length" class="banners">
      <div v-for="b in ui.banners" :key="b.id" class="banner" :class="`banner--${b.kind}`">
        <span class="grow">{{ b.text }}</span>
        <RouterLink v-if="b.action" class="banner__link" :to="b.action.to">{{ b.action.label }}</RouterLink>
        <button class="banner__close" type="button" aria-label="关闭提示" @click="ui.dismissBanner(b.id)">×</button>
      </div>
    </div>

    <!-- ── 内容区 ── -->
    <!--
      ★★ v1.0-E4.2：给 `<component>` 补 `:key`。

      Vue 3 的 `<Transition>` 依赖 **key 变化**来判断"旧节点离场、
      新节点进场"。没有 key 时：
        · 同一位置渲染不同路由 → Vue 认为是**同一个组件**（复用实例）
        · `<Transition mode="out-in">` 会等"离场"完成再进场，
          但离场判断失效 → 可能**卡在过渡中间态**（内容不可见）

      ★ 用 `route.path` 而非 `route.name`：同一路由不同参数
        （如 `/books/a` → `/books/b`）也应该是**两次切换**，
        用 name 会漏掉参数变化。
    -->
    <main class="main">
      <RouterView v-slot="{ Component }">
        <Transition name="fade" mode="out-in">
          <component :is="Component" :key="route.path" />
        </Transition>
      </RouterView>
    </main>
  </div>
</template>

<style scoped>
.layout {
  min-height: 100vh;
  display: flex; flex-direction: column;
  background: var(--bg);
}

/* ── 顶栏 ── */
.topnav {
  position: sticky; top: 0; z-index: var(--z-sticky);
  height: var(--topnav-h);
  display: flex; align-items: center; gap: var(--sp-5);
  padding: 0 var(--sp-5);
  background: var(--bg-elevated);
  border-bottom: 1px solid var(--border);
}

.topnav__brand { display: flex; align-items: center; gap: var(--sp-2); flex-shrink: 0; }
.topnav__logo {
  width: 26px; height: 26px;
  display: grid; place-items: center;
  border-radius: 7px; background: var(--brand-500); color: #fff;
  font-size: 15px; font-weight: 700; font-family: serif;
}
.topnav__name { font-size: var(--fs-sm); font-weight: var(--fw-bold); color: var(--text); }
.topnav__name em { font-style: normal; color: var(--brand-500); font-size: 11px; letter-spacing: .5px; }

.topnav__nav { display: flex; align-items: center; gap: var(--sp-1); flex: 1 1 auto; }
.topnav__item {
  display: flex; align-items: center; gap: 6px;
  height: 32px; padding: 0 var(--sp-3);
  border-radius: var(--r-md);
  font-size: var(--fs-sm); color: var(--text-muted);
  transition: background-color var(--dur-fast) var(--ease),
              color var(--dur-fast) var(--ease);
}
.topnav__item:hover { background: var(--bg-hover); color: var(--text); }
.topnav__item.is-active { background: var(--brand-50); color: var(--brand-600); font-weight: var(--fw-medium); }
.topnav__ico { font-size: 13px; opacity: .8; }

.topnav__right { display: flex; align-items: center; gap: var(--sp-2); flex-shrink: 0; }
.topnav__icon-btn {
  height: 30px; min-width: 30px; padding: 0 var(--sp-2);
  border: 1px solid var(--border); border-radius: var(--r-md);
  background: transparent; color: var(--text-muted);
  font-size: var(--fs-xs);
}
.topnav__icon-btn:hover { background: var(--bg-hover); color: var(--text); }

/* 头像 + 下拉菜单 */
.topnav__user { position: relative; }
.topnav__avatar {
  width: 30px; height: 30px;
  display: grid; place-items: center;
  border: none; border-radius: 50%;
  background: var(--brand-500); color: #fff;
  font-size: var(--fs-xs); font-weight: 700;
}
.menu {
  position: absolute; right: 0; top: calc(100% + 6px);
  min-width: 190px; padding: var(--sp-2);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  box-shadow: var(--sh-lg);
  opacity: 0; visibility: hidden; transform: translateY(-4px);
  transition: opacity var(--dur-fast) var(--ease),
              transform var(--dur-fast) var(--ease),
              visibility var(--dur-fast);
}
/* ★ 用 hover + focus-within 让键盘用户也能打开 */
.topnav__user:hover .menu,
.topnav__user:focus-within .menu {
  opacity: 1; visibility: visible; transform: translateY(0);
}
.menu__head {
  display: flex; flex-direction: column; gap: 2px;
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--border);
  margin-bottom: var(--sp-2);
}
.menu__item {
  display: block; width: 100%;
  padding: 7px var(--sp-3);
  border: none; border-radius: var(--r-sm);
  background: transparent; color: var(--text);
  font-size: var(--fs-sm); text-align: left;
}
.menu__item:hover { background: var(--bg-hover); }
.menu__item--accent { color: var(--brand-500); }
.menu__item--danger { color: var(--danger); }
.menu__divider { height: 1px; background: var(--border); margin: var(--sp-2) 0; }

/* ── Banner ── */
.banners { display: flex; flex-direction: column; }
.banner {
  display: flex; align-items: center; gap: var(--sp-3);
  padding: var(--sp-2) var(--sp-5);
  font-size: var(--fs-sm);
  border-bottom: 1px solid var(--border);
}
.banner--warn { background: var(--warning-bg); color: #8a6510; }
.banner--err  { background: var(--danger-bg); color: var(--danger); }
.banner--info { background: var(--info-bg); color: var(--info); }
.banner--ok   { background: var(--success-bg); color: var(--success); }
.banner__link { text-decoration: underline; font-weight: var(--fw-medium); }
.banner__close {
  border: none; background: transparent; color: inherit;
  font-size: 16px; line-height: 1; opacity: .6;
}
.banner__close:hover { opacity: 1; }

/* ── 内容 ── */
.main { flex: 1 1 auto; min-height: 0; }

/* 页面切换过渡 */
.fade-enter-active, .fade-leave-active { transition: opacity var(--dur-fast) var(--ease); }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* ── 响应式（G9）：窄屏收起文字标签 ── */
@media (max-width: 900px) {
  .topnav { gap: var(--sp-3); padding: 0 var(--sp-3); }
  .topnav__name { display: none; }
  .topnav__item span:not(.topnav__ico) { display: none; }
  .topnav__item { padding: 0 var(--sp-2); }
}
</style>
