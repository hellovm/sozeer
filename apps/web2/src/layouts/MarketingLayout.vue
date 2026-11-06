<script setup lang="ts">
/**
 * MarketingLayout · 公开营销页布局（v0.26.1）
 *
 * ★ 用于介绍页 / 核心技术 / 价格 / 关于我们：**无需登录**。
 * ★ 与 AppLayout 的区别：
 *   · 导航是"营销式"（滚动锚点 + 登录/注册 CTA），不是应用式功能导航
 *   · 顶部/底部都有广告位（这是广告变现的主要位置）
 *   · SEO 友好：语义化标签 + 可爬取的纯文本内容
 */
import { computed, onMounted, ref } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import http from '@/api/http'

const route = useRoute()
const auth = useAuthStore()
const ui = useUiStore()

const NAV = [
  { key: 'intro', label: '产品介绍', to: { name: 'intro' } },
  { key: 'tech', label: '核心技术', to: { name: 'tech' } },
  { key: 'about', label: '关于我们', to: { name: 'about' } },
]

const activeKey = computed(() => (route.meta.mk as string) || '')
const year = new Date().getFullYear()

/* ★★ v1.0-E4.11（合规/E1 备案）：页脚 ICP。
 *   API 一直返回 icp 字段但前台从未渲染 —— 备案号必须可见，不能只躺配置里。
 *   ★ fail-open：配置拉取失败就什么都不显示（不要渲染一个"undefined"）。 */
const icp = ref('')
const contactEmail = ref('')
/* ★★ v1.0-E4.16（细节优化①）：页脚版权旁展示版本号。
 *   来源 /api/build（匿名可读，E3 已加白名单），只取 version；
 *   拉不到就不显示（fail-open，绝不为个版本号报错）。 */
const appVersion = ref('')
interface SiteInfo { icp?: string; contact_email?: string }
onMounted(async () => {
  try {
    const r = await http.get<SiteInfo>('/api/public/site', undefined, { skipAuth: true, silent: true })
    icp.value = (r.icp || '').trim()
    contactEmail.value = (r.contact_email || '').trim()
  } catch {
    /* 静默：营销页不因配置接口失败报错 */
  }
  try {
    const b = await http.get<{ version?: string }>('/api/build', undefined, { skipAuth: true, silent: true })
    appVersion.value = (b.version || '').trim()
  } catch {
    /* 静默：版本号展示是增强，不是功能 */
  }
})
</script>

<template>
  <div class="mk">
    <!-- ── 顶栏 ── -->
    <header class="mk__top">
      <div class="mk__wrap mk__top-in">
        <RouterLink class="mk__brand" :to="{ name: 'intro' }">
          <span class="mk__logo" aria-hidden="true">溯</span>
          <span class="mk__name">溯字 <em>SOZEER</em></span>
        </RouterLink>

        <nav class="mk__nav" aria-label="站点导航">
          <RouterLink
            v-for="n in NAV" :key="n.key"
            class="mk__navitem"
            :class="{ 'is-active': activeKey === n.key }"
            :to="n.to"
          >{{ n.label }}</RouterLink>
        </nav>

        <div class="mk__cta">
          <template v-if="auth.isLoggedIn">
            <RouterLink class="btn btn--primary btn--sm" :to="{ name: 'home' }">
              进入工作台
            </RouterLink>
          </template>
          <template v-else>
            <RouterLink class="btn btn--ghost btn--sm" :to="{ name: 'login' }">登录</RouterLink>
            <RouterLink class="btn btn--primary btn--sm" :to="{ name: 'register' }">免费注册</RouterLink>
          </template>
        </div>
      </div>
    </header>

    <!-- ── 顶部广告位（全站通用） ── -->
    <div class="mk__wrap">
    </div>

    <!-- ── 内容 ── -->
    <!--
      ★★ v1.0-E4.2 修复（**真实白屏事故**）：

        原实现用 `<slot />` —— 但这是**路由布局组件**
        （在 router 里作为 `component` + `children` 使用），
        子路由由 vue-router **动态注入 `RouterView`**，
        **不会填充父组件的 slot**。

        结果：页头、页脚、广告位都正常，**只有内容区永远空白**
        （已用无头浏览器实测：`main` 内 HTML 仅 47 字符、文本 0 字符，
         控制台零报错 —— 因为压根不是错误，是渲染目标错了）。

        ★ 判据：**三种布局必须同构**（AppLayout / AdminLayout 都用
          `<RouterView>`，MarketingLayout 独独用 `<slot>` 就是隐患）。
        ★ 教训：布局组件的"内容出口"取决于**它是怎么被使用的**：
          · 路由布局（router `component`）→ 必须 `<RouterView>`
          · 普通包裹组件（`<Foo><Bar/></Foo>`）→ 用 `<slot>`
          两者长得像，但用错就是整页空白，且没有任何报错。
    -->
    <main class="mk__main">
      <div class="mk__wrap">
        <RouterView />
      </div>
    </main>

    <!-- ── 底部广告位 ── -->
    <div class="mk__wrap">
    </div>

    <!-- ── 页脚 ── -->
    <footer class="mk__foot">
      <div class="mk__wrap">
        <div class="mk__foot-grid">
          <div>
            <div class="mk__brand mk__brand--foot">
              <span class="mk__logo" aria-hidden="true">溯</span>
              <span class="mk__name">溯字 <em>SOZEER</em></span>
            </div>
            <p class="mk__foot-desc">
              AI 长篇创作与短剧生成工作台。<br>让好故事被看见。
            </p>
          </div>
          <div>
            <h4>产品</h4>
            <RouterLink :to="{ name: 'intro' }">功能介绍</RouterLink>
            <RouterLink :to="{ name: 'tech' }">核心技术</RouterLink>
            <RouterLink :to="{ name: 'pricing' }">价格方案</RouterLink>
          </div>
          <div>
            <h4>账号</h4>
            <RouterLink :to="{ name: 'login' }">登录</RouterLink>
            <RouterLink :to="{ name: 'register' }">注册</RouterLink>
            <RouterLink :to="{ name: 'help' }">帮助中心</RouterLink>
          </div>
          <div>
            <h4>公司</h4>
            <RouterLink :to="{ name: 'about' }">关于我们</RouterLink>
            <a v-if="contactEmail" :href="`mailto:${contactEmail}`">联系方式</a>
          </div>
          <div>
            <h4>法律</h4>
            <RouterLink :to="{ name: 'terms' }">用户协议</RouterLink>
            <RouterLink :to="{ name: 'privacy' }">隐私政策</RouterLink>
          </div>
        </div>
        <div class="mk__copy">
          <span>© {{ year }} 溯字 SOZEER. All rights reserved.</span>
          <!-- ★ v1.0-E4.16：版本号（构建信息 /api/build，拉不到就不显示） -->
          <span v-if="appVersion" class="mk__ver">v{{ appVersion }}</span>
          <!-- ★ v1.0-E4.11（合规）：ICP 备案号 —— 链到工信部备案系统（法定要求） -->
          <a v-if="icp" class="mk__icp" href="https://beian.miit.gov.cn/"
             target="_blank" rel="noopener noreferrer">{{ icp }}</a>
        </div>
      </div>
    </footer>
  </div>
</template>

<style scoped>
.mk { min-height: 100vh; display: flex; flex-direction: column; background: var(--bg); }
.mk__wrap { width: 100%; max-width: 1120px; margin: 0 auto; padding: 0 var(--sp-4); }

.mk__top {
  position: sticky; top: 0; z-index: 20;
  background: color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--border);
}
.mk__top-in {
  display: flex; align-items: center; gap: var(--sp-4);
  height: 60px;
}
.mk__brand { display: flex; align-items: center; gap: var(--sp-2); text-decoration: none; }
/* ★★ v1.0-E4.16（细节优化②）修复 logo 消失：
 *   原引用 `var(--brand)` —— 设计令牌里**只有 --brand-500 系**，没有裸 --brand，
 *   背景解析失败 → 透明底 + 白字 = 视觉上"什么都没有"。
 *   ★ 教训：引用变量必须先确认令牌表里有（同 §键重命名必须搬运历史数据）。 */
.mk__logo {
  width: 30px; height: 30px; border-radius: 8px; flex: none;
  display: grid; place-items: center;
  background: var(--brand-500); color: #fff; font-weight: 700;
}
.mk__name { font-weight: 600; color: var(--text); letter-spacing: .02em; }
.mk__name em { font-style: normal; font-size: var(--fs-xs); color: var(--text-faint); }
.mk__nav { display: flex; gap: var(--sp-1); margin-left: var(--sp-4); flex: 1; }
.mk__navitem {
  padding: 6px 12px; border-radius: 999px; text-decoration: none;
  color: var(--text-muted); font-size: var(--fs-sm);
}
.mk__navitem:hover { color: var(--text); background: var(--surface-2); }
.mk__navitem.is-active { color: var(--brand-500); background: var(--brand-50); }
.mk__cta { display: flex; gap: var(--sp-2); }

.mk__ad { margin: var(--sp-4) 0; }
.mk__main { flex: 1; padding: var(--sp-6) 0; }

.mk__foot { border-top: 1px solid var(--border); padding: var(--sp-6) 0 var(--sp-4); }
/* ★★ v1.0-E4.16（细节优化③）四列并排：
 *   原模板 2fr + 1fr×3 = 4 个轨道却装 5 个子块（品牌/产品/账号/公司/法律），
 *   "法律"被挤到第二行孤零零一列（截图实锤）。
 *   现在：品牌块 1.6fr + 四个链接列各 1fr，五行并排一屏收纳。 */
.mk__foot-grid {
  display: grid; grid-template-columns: 1.6fr 1fr 1fr 1fr 1fr;
  gap: var(--sp-5);
}
.mk__brand--foot { margin-bottom: var(--sp-2); }
.mk__foot-desc { color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.7; }
.mk__foot h4 { font-size: var(--fs-sm); margin-bottom: var(--sp-2); color: var(--text); }
.mk__foot a {
  display: block; padding: 3px 0; color: var(--text-muted);
  font-size: var(--fs-sm); text-decoration: none;
}
.mk__foot a:hover { color: var(--brand-500); }
.mk__copy {
  margin-top: var(--sp-5); padding-top: var(--sp-3);
  border-top: 1px solid var(--border);
  color: var(--text-faint); font-size: var(--fs-xs);
  display: flex; gap: var(--sp-4); align-items: baseline; flex-wrap: wrap;
}
.mk__icp { color: var(--text-faint); text-decoration: none; }
.mk__icp:hover { color: var(--brand-500); }
.mk__ver { color: var(--text-faint); }
@media (max-width: 720px) {
  .mk__nav { display: none; }
  .mk__foot-grid { grid-template-columns: 1fr 1fr; }
}
</style>
