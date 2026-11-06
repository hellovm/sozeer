<script setup lang="ts">
/**
 * StatsView · 写作统计（v0.26.1）
 *
 * ★ 数据来源：作品列表聚合（后端暂无专门统计接口 → 前端算，标注清楚）
 *   GET /api/books → 裸数组 [{title, chapters, words, quality_score, status}]
 * ★ 为什么标注"前端算"：避免将来误以为这是权威数据。
 *   真实日更趋势需要 chapter 级时间戳，后端补接口后再替换。
 *
 * ★★ v1.0-E4.11：日更趋势已接后端真数据 ——
 *   GET /api/stats/daily-writes?days=30（按章节 updated_at 聚合，服务端补零）。
 *   服务端 SVG 折线图（无图表库依赖，与全站"少依赖"风格一致）。
 */
import { computed, onMounted, ref } from 'vue'
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'
import AppProgress from '@/components/AppProgress.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import AppTag from '@/components/AppTag.vue'
import { useUiStore } from '@/stores/ui'
import { bookApi } from '@/api/modules/book'
import { meApi } from '@/api/modules/me'

const ui = useUiStore()
const loading = ref(true)
const books = ref<Array<{
  id: string; title: string; chapters?: number; words?: number
  quality_score?: number; status?: string; platform?: string
  updated_at?: string
}>>([])

/** 日更趋势（服务端真数据；空数组=无数据，显示空态而非假折线） */
const daily = ref<Array<{ date: string; words: number; chapters: number }>>([])
const dailyLoading = ref(false)

const totals = computed(() => {
  const words = books.value.reduce((s, b) => s + (b.words || 0), 0)
  const chapters = books.value.reduce((s, b) => s + (b.chapters || 0), 0)
  const scored = books.value.filter(b => (b.quality_score || 0) > 0)
  const avg = scored.length
    ? Math.round(scored.reduce((s, b) => s + (b.quality_score || 0), 0) / scored.length)
    : 0
  const avgLen = chapters ? Math.round(words / chapters) : 0
  const last7 = daily.value.slice(-7).reduce((s, d) => s + d.words, 0)
  return { words, chapters, avg, avgLen, count: books.value.length, last7 }
})

/** 按字数排序的排行（Top 8） */
const ranking = computed(() =>
  [...books.value].sort((a, b) => (b.words || 0) - (a.words || 0)).slice(0, 8))
const maxWords = computed(() => Math.max(1, ...ranking.value.map(b => b.words || 0)))

/** 折线图几何：把 daily 映射成 SVG polyline 点（viewBox 640x160，左右留 8px） */
const W = 640
const H = 160
const PAD = 8
const sparkPoints = computed(() => {
  if (!daily.value.length) return ''
  const max = Math.max(1, ...daily.value.map(d => d.words))
  const n = daily.value.length
  return daily.value.map((d, i) => {
    const x = PAD + (i / Math.max(1, n - 1)) * (W - PAD * 2)
    const y = H - PAD - (d.words / max) * (H - PAD * 2)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
})
/** X 轴起止日期标注 */
const axisLabel = computed(() => {
  if (daily.value.length < 2) return { a: '', b: '' }
  return { a: daily.value[0].date.slice(5), b: daily.value[daily.value.length - 1].date.slice(5) }
})
const dailyMaxWords = computed(() => Math.max(1, ...daily.value.map(d => d.words)))

function fmtWords(n = 0) {
  if (n >= 10000) return `${(n / 10000).toFixed(1)} 万`
  return n.toLocaleString()
}

async function load() {
  loading.value = true
  dailyLoading.value = true
  try {
    const r = await bookApi.list()
    books.value = Array.isArray(r) ? r : ((r as unknown as { items?: never[] }).items || [])
    // 日更趋势独立拉取（失败不阻塞主统计——两个数据源解耦）
    try {
      daily.value = await meApi.dailyWrites(30)
    } catch {
      daily.value = []
    }
  } catch (e) {
    ui.err((e as Error).message || '加载失败')
  } finally { loading.value = false; dailyLoading.value = false }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <header class="page__head">
      <div>
        <h1 class="page__title">写作统计</h1>
        <p class="page__sub">字数、章节、质检概览</p>
      </div>
      <AppButton variant="ghost" @click="load">刷新</AppButton>
    </header>

    <AppSkeleton v-if="loading" variant="card" :count="2" />

    <AppEmpty v-else-if="!books.length" icon="▤" title="还没有作品"
              description="创建作品并开始写作后，这里会有统计。" />

    <template v-else>
      <div class="cards">
        <AppCard><div class="stat">
          <span class="stat__label">总字数</span>
          <strong class="stat__num">{{ fmtWords(totals.words) }}</strong>
        </div></AppCard>
        <AppCard><div class="stat">
          <span class="stat__label">总章节</span>
          <strong class="stat__num">{{ totals.chapters.toLocaleString() }}</strong>
        </div></AppCard>
        <AppCard><div class="stat">
          <span class="stat__label">平均章长</span>
          <strong class="stat__num">{{ totals.avgLen.toLocaleString() }} 字</strong>
        </div></AppCard>
        <AppCard><div class="stat">
          <span class="stat__label">平均质检分</span>
          <strong class="stat__num">{{ totals.avg || '—' }}</strong>
        </div></AppCard>
      </div>

      <AppCard title="字数排行" subtitle="按总字数排序（前 8）" class="mt">
        <ul class="rank">
          <li v-for="b in ranking" :key="b.id">
            <div class="rank__row">
              <RouterLink class="rank__title"
                          :to="{ name: 'studio', params: { bookId: b.id } }">
                {{ b.title }}
              </RouterLink>
              <span class="rank__num">{{ fmtWords(b.words) }} 字</span>
            </div>
            <AppProgress :percent="Math.round(((b.words || 0) / maxWords) * 100)"
                         :show-text="false" size="sm" />
            <div class="rank__meta muted tiny">
              {{ b.chapters || 0 }} 章 ·
              <span v-if="b.updated_at">更新于 {{ b.updated_at }}</span>
              <AppTag v-if="b.quality_score" size="sm"
                      :kind="b.quality_score >= 90 ? 'success'
                        : b.quality_score >= 75 ? 'warning' : 'danger'">
                质检 {{ b.quality_score }}
              </AppTag>
            </div>
          </li>
        </ul>
      </AppCard>

      <!-- ★★ v1.0-E4.11：日更趋势（服务端真数据，30 天） -->
      <AppCard title="日更趋势" subtitle="近 30 天每日新增字数（按章节保存时间）" class="mt">
        <template v-if="daily.length">
          <div class="trend">
            <div class="trend__stat">
              <span class="stat__label">近 7 天</span>
              <strong class="stat__num">{{ fmtWords(totals.last7) }} 字</strong>
            </div>
            <div class="trend__stat">
              <span class="stat__label">30 天峰值</span>
              <strong class="stat__num">{{ fmtWords(dailyMaxWords) }} 字/日</strong>
            </div>
          </div>
          <svg class="trend__chart" :viewBox="`0 0 ${W} ${H}`" role="img"
               aria-label="近 30 天每日新增字数折线图">
            <!-- 网格基线（浅色，light 主题下可读） -->
            <line :x1="PAD" :y1="H - PAD" :x2="W - PAD" :y2="H - PAD"
                  stroke="var(--border)" stroke-width="1" />
            <polyline :points="sparkPoints" fill="none"
                      stroke="var(--brand)" stroke-width="2.5"
                      stroke-linejoin="round" stroke-linecap="round" />
          </svg>
          <div class="trend__axis muted tiny">
            <span>{{ axisLabel.a }}</span><span>{{ axisLabel.b }}</span>
          </div>
        </template>
        <p v-else-if="dailyLoading" class="muted tiny">加载趋势中…</p>
        <p v-else class="muted tiny">
          近 30 天还没有章节保存记录 —— 开始写作后这里会出现你的日更曲线。
        </p>
      </AppCard>

      <AppCard title="统计口径说明" class="mt">
        <p class="muted tiny">
          总量类指标由作品列表聚合得出（前端计算）；日更趋势来自服务端
          /api/stats/daily-writes（按章节保存时间聚合，权威口径）。
        </p>
      </AppCard>
    </template>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 900px; margin: 0 auto; }
.page__head { display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--sp-4); margin-bottom: var(--sp-4); }
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); }
.cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--sp-3); }
.stat { display: flex; flex-direction: column; gap: 4px; }
.stat__label { font-size: var(--fs-xs); color: var(--text-faint); }
.stat__num { font-size: 20px; color: var(--text); font-variant-numeric: tabular-nums; }
.mt { margin-top: var(--sp-4); }
.rank { list-style: none; margin: 0; padding: 0; }
.rank li { padding: var(--sp-3) 0; border-bottom: 1px solid var(--border); }
.rank li:last-child { border-bottom: none; }
.rank__row { display: flex; justify-content: space-between; align-items: baseline;
  gap: var(--sp-3); margin-bottom: 6px; }
.rank__title { color: var(--text); text-decoration: none; font-size: var(--fs-sm); }
.rank__title:hover { color: var(--brand); }
.rank__num { font-size: var(--fs-sm); color: var(--text-muted);
  font-variant-numeric: tabular-nums; }
.rank__meta { margin-top: 6px; display: flex; align-items: center; gap: 6px; }
.trend { display: flex; gap: var(--sp-6); margin-bottom: var(--sp-3); }
.trend__stat { display: flex; flex-direction: column; gap: 2px; }
.trend__chart { width: 100%; height: 160px; display: block; }
.trend__axis { display: flex; justify-content: space-between; margin-top: 4px; }
@media (max-width: 720px) { .cards { grid-template-columns: repeat(2, 1fr); } }
</style>
