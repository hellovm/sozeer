<script setup lang="ts">
/**
 * HomeView · 工作台首页
 * 最近作品 + 用量概览 + 快捷入口
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import bookApi, { type Book } from '@/api/modules/book'
import meApi, { type CreditAccount, type UsageStats } from '@/api/modules/me'
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
// ★ v1.0-E4：新手引导卡片（不打断、可关闭、基于真实数据）
import OnboardingCard from '@/components/OnboardingCard.vue'

const router = useRouter()
const auth = useAuthStore()

const loading = ref(true)
const books = ref<Book[]>([])
const account = ref<CreditAccount | null>(null)
// ★ v1.0-E4.13 工作台用量概览 + 套餐续费引导（服务端实算；加载失败静默降级）
const usage = ref<UsageStats | null>(null)

const greeting = (() => {
  const h = new Date().getHours()
  if (h < 6) return '夜深了'
  if (h < 12) return '早上好'
  if (h < 14) return '中午好'
  if (h < 18) return '下午好'
  return '晚上好'
})()

onMounted(async () => {
  // ★ 并行加载；单个失败不影响另一块（fail-soft）
  const [b, a, u] = await Promise.allSettled([
    bookApi.list(),
    meApi.quota(),
    meApi.usageStats(),
  ])
  if (b.status === 'fulfilled') {
    // ★ 兼容裸数组（当前契约）与 {items}（防御未来变更）
    const bv = b.value
    books.value = Array.isArray(bv) ? bv : ((bv as unknown as { items?: Book[] }).items || [])
  }
  if (a.status === 'fulfilled') account.value = a.value
  if (u.status === 'fulfilled') usage.value = u.value
  loading.value = false
})


function fmtDate(s?: string) {
  if (!s) return '—'
  return s.slice(0, 10)
}
</script>

<template>
  <div class="page">
    <header class="page__head">
      <div>
        <h1 class="page__title">{{ greeting }}，{{ auth.user?.nickname || auth.user?.username }}</h1>
        <p class="page__sub">继续你的创作，或开始一部新作品</p>
      </div>
      <AppButton variant="primary" icon="＋" @click="router.push({ name: 'books' })">新建作品</AppButton>
    </header>

    <!-- ★ v1.0-E4：新手引导（未完成且有内容时自动显示；完成/隐藏后自动消失） -->
    <OnboardingCard />

    <!-- 概览卡 -->
    <div class="stats">
      <AppCard padding="md">
        <div class="stat">
          <span class="stat__k">可用积分</span>
          <span class="stat__v">{{ account ? account.available.toLocaleString('zh-CN') : '—' }}</span>
          <span class="stat__s">
            余额 {{ account?.balance?.toLocaleString('zh-CN') ?? '—' }}
            <template v-if="account?.frozen"> · 冻结 {{ account.frozen }}</template>
          </span>
        </div>
      </AppCard>
      <AppCard padding="md">
        <div class="stat">
          <span class="stat__k">我的作品</span>
          <span class="stat__v">{{ usage ? usage.books : books.length }}</span>
          <span class="stat__s">
            共 {{ (usage?.words_total ?? books.reduce((s, b) => s + (b.words || 0), 0)).toLocaleString('zh-CN') }} 字
            <template v-if="usage?.monthly_words"> · 本月 +{{ usage.monthly_words.toLocaleString('zh-CN') }}</template>
          </span>
        </div>
      </AppCard>
      <AppCard padding="md">
        <div class="stat">
          <span class="stat__k">快捷入口</span>
          <div class="stat__links">
            <RouterLink :to="{ name: 'data-usage' }">用量与成本</RouterLink>
          </div>
        </div>
      </AppCard>
    </div>

    <!-- 最近作品 -->
    <AppCard title="最近作品" padding="none">
      <template #actions>
        <AppButton size="sm" variant="text" @click="router.push({ name: 'books' })">全部 ›</AppButton>
      </template>

      <div class="card-body-pad">
        <AppSkeleton v-if="loading" variant="card" :count="2" />
        <AppEmpty
          v-else-if="!books.length"
          icon="📖"
          title="还没有作品"
          description="点下面按钮，开始你的第一部小说。或者先和创作主编聊聊你的想法。"
          action-text="＋ 新建作品"
          @action="router.push({ name: 'books' })"
        />
        <ul v-else class="blist">
          <li
            v-for="b in books.slice(0, 5)"
            :key="b.id"
            class="blist__item"
            tabindex="0"
            role="button"
            @click="router.push({ name: 'studio', params: { bookId: b.id } })"
            @keydown.enter="router.push({ name: 'studio', params: { bookId: b.id } })"
          >
            <div class="blist__main">
              <strong class="blist__title truncate">{{ b.title }}</strong>
              <span class="blist__meta">
                {{ b.chapters || 0 }} 章 · {{ (b.words || 0).toLocaleString('zh-CN') }} 字
                <template v-if="b.theme"> · {{ b.theme }}</template>
              </span>
            </div>
            <span class="blist__date subtle tiny">{{ fmtDate(b.updated_at || b.created_at) }}</span>
          </li>
        </ul>
      </div>
    </AppCard>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 1100px; margin: 0 auto; }
.page__head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--sp-4); margin-bottom: var(--sp-5);
}
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); }

.stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--sp-4); margin-bottom: var(--sp-5);
}
.stat { display: flex; flex-direction: column; gap: 2px; }
.stat__k { font-size: var(--fs-xs); color: var(--text-muted); }
.stat__v { font-size: var(--fs-xl); font-weight: var(--fw-bold); font-variant-numeric: tabular-nums; }
.stat__s { font-size: var(--fs-xs); color: var(--text-subtle); }
.stat__links { display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-sm); }

.card-body-pad { padding: var(--sp-4); }

.blist { display: flex; flex-direction: column; }
.blist__item {
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-3) var(--sp-3);
  border-radius: var(--r-md);
  cursor: pointer;
  transition: background-color var(--dur-fast) var(--ease);
}
.blist__item:hover { background: var(--bg-hover); }
.blist__item:focus-visible { outline: 2px solid var(--brand-500); outline-offset: -2px; }
.blist__main { min-width: 0; }
.blist__title { display: block; font-size: var(--fs-sm); }
.blist__meta { font-size: var(--fs-xs); color: var(--text-muted); }
.blist__date { flex-shrink: 0; }
</style>
