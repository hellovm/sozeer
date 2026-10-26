<script setup lang="ts">
/**
 * GenrePacksView · 题材包（v0.26.1）
 *
 * ★★ 契约（对照 routers/genres.py）：
 *   GET /api/genres → **裸数组**（不是 {items}！）
 *   GET /api/genres/market → { items: [...] }
 *
 * ★ 题材包是什么：一个题材 = 度量衡表 + 扫描模式 + 视觉锚点 + 提示词片段。
 *   它决定"这个世界怎么运转"（境界/货币/时间单位）与"画风长什么样"。
 *   所以列表要展示 metrics 与 visual_anchor，而不只是名字。
 */
import { onMounted, ref } from 'vue'
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'
import AppTag from '@/components/AppTag.vue'
import AppTabs from '@/components/AppTabs.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import type { TabItem } from '@/types/components'
import { useUiStore } from '@/stores/ui'
import { assetApi, type GenrePack, type MarketPack } from '@/api/modules/asset'

const ui = useUiStore()
const loading = ref(true)
const busy = ref('')
const tab = ref('builtin')
const packs = ref<GenrePack[]>([])
const market = ref<MarketPack[]>([])

const TABS: TabItem[] = [
  { key: 'builtin', label: '内置题材' },
  { key: 'market', label: '题材市场' },
]

async function load() {
  loading.value = true
  try {
    const [g, m] = await Promise.all([
      assetApi.genres().catch(() => ({ items: [] as GenrePack[], builtin_count: 0 })),
      assetApi.market().catch(() => ({ items: [] })),
    ])
    // ★ 真实契约：{items, builtin_count}（不是裸数组 —— 已在测试里被拦下）
    packs.value = Array.isArray(g) ? g : (g.items || [])
    market.value = m.items || []
  } finally {
    loading.value = false
  }
}

async function act(key: string, fn: () => Promise<unknown>, okMsg: string) {
  busy.value = key
  try {
    await fn()
    ui.ok(okMsg)
    await load()
  } catch (e) {
    ui.err((e as Error).message || '操作失败')
  } finally {
    busy.value = ''
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <header class="page__head">
      <div>
        <h1 class="page__title">题材包</h1>
        <p class="page__sub">
          题材决定"世界怎么运转"（度量衡）与"画风长什么样"（视觉锚点）
        </p>
      </div>
      <AppButton variant="ghost" @click="load">刷新</AppButton>
    </header>

    <AppTabs v-model="tab" :items="TABS" />

    <AppSkeleton v-if="loading" variant="card" :count="3" />

    <!-- 内置题材 -->
    <div v-else-if="tab === 'builtin'">
      <AppEmpty v-if="!packs.length" icon="🎨" title="没有可用的题材包" />
      <div v-else class="grid">
        <AppCard v-for="p in packs" :key="p.key" :accent="p.builtin ? 'brand' : 'none'">
          <div class="pk__head">
            <span class="pk__icon">{{ p.icon || '📦' }}</span>
            <div>
              <b>{{ p.name }}</b>
              <div class="muted tiny"><code>{{ p.key }}</code></div>
            </div>
            <AppTag v-if="p.builtin" size="sm" kind="brand">内置</AppTag>
            <AppTag v-else size="sm">自定义</AppTag>
          </div>

          <p class="muted tiny pk__desc">{{ p.desc || '（无描述）' }}</p>

          <!-- 度量衡（这个世界的单位体系） -->
          <div v-if="p.metrics && Object.keys(p.metrics).length" class="pk__metrics">
            <span class="pk__label">度量衡</span>
            <div class="pk__chips">
              <AppTag v-for="(v, k) in p.metrics" :key="k" size="sm">{{ k }}: {{ v }}</AppTag>
            </div>
          </div>

          <!-- 视觉锚点（画风统一的关键） -->
          <div v-if="p.visual_anchor" class="pk__anchor">
            <span class="pk__label">视觉锚点</span>
            <p class="muted tiny">{{ p.visual_anchor }}</p>
          </div>

          <div class="pk__foot">
            <AppTag v-if="p.scan_mode" size="sm" kind="info">扫描：{{ p.scan_mode }}</AppTag>
            <AppTag v-if="p.version" size="sm">v{{ p.version }}</AppTag>
          </div>
        </AppCard>
      </div>
    </div>

    <!-- 题材市场 -->
    <div v-else>
      <AppEmpty v-if="!market.length" icon="🏪" title="市场暂无内容"
                description="市场用于发布与安装社区题材包。" />
      <div v-else class="grid">
        <AppCard v-for="m in market" :key="m.key">
          <div class="pk__head">
            <span class="pk__icon">🏪</span>
            <div>
              <b>{{ m.name }}</b>
              <div class="muted tiny">
                <code>{{ m.key }}</code>
                <template v-if="m.author"> · by {{ m.author }}</template>
              </div>
            </div>
            <AppTag v-if="m.installed" size="sm" kind="success">已安装</AppTag>
          </div>
          <p class="muted tiny pk__desc">{{ m.desc || '（无描述）' }}</p>
          <div class="pk__foot">
            <AppTag v-if="m.version" size="sm">v{{ m.version }}</AppTag>
            <AppTag v-if="m.rating" size="sm" kind="warning">★ {{ m.rating }}</AppTag>
            <AppTag v-if="m.installs" size="sm">{{ m.installs }} 次安装</AppTag>
          </div>
          <div class="pk__actions">
            <AppButton v-if="!m.installed" size="sm" variant="primary"
                       :loading="busy === `i-${m.key}`"
                       @click="act(`i-${m.key}`, () => assetApi.installPack(m.key), '安装成功')">
              安装
            </AppButton>
            <AppButton v-else size="sm"
                       :loading="busy === `u-${m.key}`"
                       @click="act(`u-${m.key}`, () => assetApi.uninstallPack(m.key), '已卸载')">
              卸载
            </AppButton>
          </div>
        </AppCard>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 1080px; margin: 0 auto; }
.page__head { display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--sp-4); margin-bottom: var(--sp-4); }
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--sp-3); margin-top: var(--sp-3); }
.pk__head { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: var(--sp-2); }
.pk__head > div { flex: 1; min-width: 0; }
.pk__icon { font-size: 20px; }
.pk__desc { margin: 0 0 var(--sp-2); line-height: 1.7; }
.pk__metrics, .pk__anchor { margin-bottom: var(--sp-2); }
.pk__label { display: block; font-size: var(--fs-xs); color: var(--text-faint);
  margin-bottom: 4px; }
.pk__chips { display: flex; flex-wrap: wrap; gap: 4px; }
.pk__anchor p { margin: 0; line-height: 1.6; }
.pk__foot { display: flex; gap: 4px; flex-wrap: wrap; margin-top: var(--sp-2); }
.pk__actions { margin-top: var(--sp-3); }
code { background: var(--surface-2); padding: 1px 5px; border-radius: 4px;
  font-size: var(--fs-xs); }
</style>
