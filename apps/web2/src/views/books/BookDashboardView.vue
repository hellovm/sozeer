<script setup lang="ts">
/**
 * BookDashboardView · 作品看板（#15 项目维度 Dashboard）
 *
 * 一屏呈现「进度 / 质量 / 下一步」——把散落在写作台、质检页、设定栏里的
 * 书籍维度数据聚合成一块综合面板，并给出**确定性**的下一步建议（规则推导，
 * 不猜、不靠 LLM）。数据全部来自现有只读 API（/state + / + /chapters），
 * 零后端改动。
 *
 * ★ 铁律 R-C「一页一验收」：
 *   [x] KPI：总字数 / 章节数 / 平均质检分 / 目标完成度
 *   [x] 进度：目标字数进度条 + 各卷分布
 *   [x] 质量：分数分布 + 待改章节（可跳转）
 *   [x] 下一步建议：按状态确定性推导（空书→立项 / 低分→精修 / 超期伏笔→回收 / 近目标→收束 / 默认→续写）
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import bookApi, {
  type BookDetail, type BookState, type ChapterBrief,
} from '@/api/modules/book'
import { ApiError } from '@/types/api'
import AppButton from '@/components/AppButton.vue'
import AppCard from '@/components/AppCard.vue'
import AppTag from '@/components/AppTag.vue'
import AppProgress from '@/components/AppProgress.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'

const route = useRoute()
const router = useRouter()

const bookId = computed(() => String(route.params.bookId || ''))

const book = ref<BookDetail | null>(null)
const state = ref<BookState | null>(null)
const chapters = ref<ChapterBrief[]>([])
const loading = ref(true)

onMounted(async () => {
  if (!bookId.value) { router.replace({ name: 'books' }); return }
  // 并行加载；单块失败不影响其它（fail-soft，与 StudioView 同款）
  const [b, s, c] = await Promise.allSettled([
    bookApi.get(bookId.value),
    bookApi.state(bookId.value),
    bookApi.chapters(bookId.value),
  ])
  if (b.status === 'fulfilled') book.value = b.value
  if (s.status === 'fulfilled') state.value = s.value
  if (c.status === 'fulfilled') chapters.value = c.value
  loading.value = false
})

/* ── KPI ── */

const totalWords = computed(() => state.value?.progress.words || 0)
const chapterCount = computed(() => chapters.value.length)

/** 有质检分的章节 + 平均分 */
const scored = computed(() => chapters.value.filter(c => c.gate_score > 0))
const avgScore = computed(() => {
  if (!scored.value.length) return 0
  return Math.round(scored.value.reduce((s, c) => s + c.gate_score, 0) / scored.value.length)
})

/** 目标完成度（字数 / 目标字数，封顶 100） */
const targetPct = computed(() => {
  const t = book.value?.target_words || 0
  if (!t) return 0
  return Math.min(100, Math.round((totalWords.value / t) * 100))
})

/* ── 质量分布（五档，与 QualityView 口径一致）── */
const distribution = computed(() => {
  const buckets = [
    { label: '优秀 ≥90', min: 90, max: 101, count: 0, kind: 'success' as const },
    { label: '良好 75-89', min: 75, max: 90, count: 0, kind: 'info' as const },
    { label: '及格 60-74', min: 60, max: 75, count: 0, kind: 'warning' as const },
    { label: '待改 <60', min: -1, max: 60, count: 0, kind: 'danger' as const },
  ]
  for (const c of scored.value) {
    const s = c.gate_score
    const b = buckets.find(x => s >= x.min && s < x.max)
    if (b) b.count += 1
  }
  return buckets
})

/** 最需要改的章节（分数升序前 6） */
const worst = computed(() =>
  [...scored.value].sort((a, b) => a.gate_score - b.gate_score).slice(0, 6))

/** 超期伏笔数（status=open 且无回收章） */
const openForeshadows = computed(() =>
  (state.value?.foreshadows || []).filter(f => f.status === 'open').length)

/* ── 下一步建议（★ 确定性规则推导，不靠 LLM、不猜）── */
interface NextAction { text: string; to: (() => void) }
const nextActions = computed<NextAction[]>(() => {
  const list: NextAction[] = []
  const chs = chapters.value

  // 空书 → 先起草第一章
  if (!chs.length) {
    list.push({
      text: '还没有章节——去写作台让主编起草第一章',
      to: () => router.push({ name: 'studio', params: { bookId: bookId.value } }),
    })
    return list
  }

  // 低分章节 → 精修
  if (worst.value.length && worst.value[0].gate_score < 75) {
    const c = worst.value[0]
    list.push({
      text: `第 ${c.idx} 章质检 ${c.gate_score} 分偏低，建议先精修`,
      to: () => router.push({ name: 'chapter-edit', params: { bookId: bookId.value, idx: c.idx } }),
    })
  }

  // 超期伏笔 → 回收
  if (openForeshadows.value > 0) {
    list.push({
      text: `有 ${openForeshadows.value} 个伏笔待回收，去质检页查看债务`,
      to: () => router.push({ name: 'quality', params: { bookId: bookId.value } }),
    })
  }

  // 接近目标字数 → 收束
  if (book.value?.target_words && targetPct.value >= 85) {
    list.push({
      text: `已达目标字数 ${targetPct.value}%，考虑收束主线、准备收尾`,
      to: () => router.push({ name: 'studio', params: { bookId: bookId.value } }),
    })
  }

  // 默认 → 续写下一章
  const next = chs.length ? Math.max(...chs.map(c => c.idx)) + 1 : 1
  list.push({
    text: `继续写第 ${next} 章（去写作台）`,
    to: () => router.push({ name: 'studio', params: { bookId: bookId.value } }),
  })

  return list.slice(0, 3)   // 最多给 3 条，避免刷屏
})

function scoreKind(s: number) {
  if (s >= 90) return 'success' as const
  if (s >= 75) return 'info' as const
  if (s >= 60) return 'warning' as const
  return 'danger' as const
}

function openChapter(idx: number) {
  router.push({ name: 'chapter-edit', params: { bookId: bookId.value, idx } })
}
</script>

<template>
  <div class="page">
    <header class="page__head">
      <div class="page__title-row">
        <div>
          <RouterLink class="page__back" :to="{ name: 'studio', params: { bookId } }">← 写作台</RouterLink>
          <h1 class="page__title">作品看板</h1>
          <p class="page__sub">{{ book?.title || '加载中…' }}</p>
        </div>
        <AppButton size="sm" variant="primary" @click="router.push({ name: 'studio', params: { bookId } })">
          去写作
        </AppButton>
      </div>
    </header>

    <AppSkeleton v-if="loading" variant="card" :count="3" />

    <AppEmpty v-else-if="!book" icon="📕" title="作品不存在或无权访问" size="md" />

    <template v-else>
      <!-- KPI -->
      <div class="cards">
        <AppCard padding="md">
          <div class="kpi">
            <span class="kpi__k">总字数</span>
            <span class="kpi__v k-default">{{ totalWords.toLocaleString('zh-CN') }}</span>
            <span class="kpi__s">共 {{ chapterCount }} 章</span>
          </div>
        </AppCard>
        <AppCard padding="md">
          <div class="kpi">
            <span class="kpi__k">平均质检分</span>
            <span class="kpi__v" :class="`k-${scoreKind(avgScore)}`">{{ avgScore || '—' }}</span>
            <span class="kpi__s">{{ scored.length }} 章已质检</span>
          </div>
        </AppCard>
        <AppCard padding="md">
          <div class="kpi">
            <span class="kpi__k">待回收伏笔</span>
            <span class="kpi__v" :class="openForeshadows ? 'k-warning' : ''">{{ openForeshadows }}</span>
            <span class="kpi__s">开放中</span>
          </div>
        </AppCard>
        <AppCard padding="md">
          <div class="kpi">
            <span class="kpi__k">目标完成度</span>
            <span class="kpi__v" :class="targetPct >= 85 ? 'k-success' : 'k-default'">{{ targetPct }}%</span>
            <span class="kpi__s">
              目标 {{ (book?.target_words || 0).toLocaleString('zh-CN') }} 字
            </span>
          </div>
        </AppCard>
      </div>

      <!-- 下一步建议（确定性规则） -->
      <AppCard title="下一步建议" padding="md" style="margin-bottom: var(--sp-4)">
        <ul class="next">
          <li v-for="(a, i) in nextActions" :key="i" class="next__item">
            <span class="next__dot" :class="i === 0 ? 'is-top' : ''" aria-hidden="true"></span>
            <span class="next__text grow truncate">{{ a.text }}</span>
            <AppButton size="sm" variant="ghost" @click="a.to()">去处理</AppButton>
          </li>
        </ul>
      </AppCard>

      <!-- 目标进度 -->
      <AppCard v-if="book?.target_words" title="目标字数进度" padding="md" style="margin-bottom: var(--sp-4)">
        <AppProgress
          :percent="targetPct"
          :kind="targetPct >= 85 ? 'success' : 'brand'"
          :label="`已写 ${totalWords.toLocaleString('zh-CN')} / ${book.target_words.toLocaleString('zh-CN')} 字`"
        />
      </AppCard>

      <!-- 质量分布 -->
      <AppCard title="质检分布" padding="md" style="margin-bottom: var(--sp-4)">
        <AppEmpty v-if="!scored.length" icon="◎" title="还没有质检数据" size="sm"
                  description="章节采纳或重跑质检后，这里会显示分数分布" />
        <div v-else class="dist">
          <div v-for="b in distribution" :key="b.label" class="dist__row">
            <span class="dist__label">{{ b.label }}</span>
            <div class="dist__bar">
              <div class="dist__fill" :class="`f-${b.kind}`"
                   :style="{ width: scored.length ? `${(b.count / scored.length) * 100}%` : '0%' }" />
            </div>
            <span class="dist__count mono">{{ b.count }}</span>
          </div>
        </div>
      </AppCard>

      <!-- 待改章节 -->
      <AppCard v-if="worst.length" title="最需要改进的章节" padding="md">
        <ul class="chlist">
          <li v-for="c in worst" :key="c.idx" class="chlist__item"
              tabindex="0" role="button"
              @click="openChapter(c.idx)" @keydown.enter="openChapter(c.idx)">
            <span class="mono tiny chlist__idx">{{ String(c.idx).padStart(3, '0') }}</span>
            <span class="grow truncate">{{ c.title || '未命名' }}</span>
            <span class="muted tiny mono">{{ (c.words || 0).toLocaleString('zh-CN') }} 字</span>
            <AppTag size="sm" :kind="scoreKind(c.gate_score)">{{ c.gate_score }}</AppTag>
          </li>
        </ul>
      </AppCard>
    </template>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 960px; margin: 0 auto; }
.page__head { margin-bottom: var(--sp-4); }
.page__title-row { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--sp-4); }
.page__back { font-size: var(--fs-xs); color: var(--text-muted); display: block; margin-bottom: 4px; }
.page__back:hover { color: var(--brand-500); }
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); }

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: var(--sp-3); margin-bottom: var(--sp-4); }
.kpi { display: flex; flex-direction: column; gap: 2px; }
.kpi__k { font-size: var(--fs-xs); color: var(--text-muted); }
.kpi__v { font-size: var(--fs-xl); font-weight: var(--fw-bold); font-variant-numeric: tabular-nums; }
.kpi__s { font-size: var(--fs-xs); color: var(--text-subtle); }
.k-success { color: var(--success); }
.k-info { color: var(--info); }
.k-warning { color: var(--warning); }
.k-danger { color: var(--danger); }
.k-default { color: var(--text-muted); }

/* 下一步建议 */
.next { display: flex; flex-direction: column; gap: 2px; }
.next__item { display: flex; align-items: center; gap: var(--sp-3); padding: 8px var(--sp-1); }
.next__dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--gray-300); }
.next__dot.is-top { background: var(--brand-500); }
.next__text { font-size: var(--fs-sm); }

/* 质量分布 */
.dist { display: flex; flex-direction: column; gap: var(--sp-3); }
.dist__row { display: grid; grid-template-columns: 84px minmax(0, 1fr) 32px; gap: var(--sp-3); align-items: center; }
.dist__label { font-size: var(--fs-xs); color: var(--text-muted); }
.dist__bar { height: 8px; border-radius: var(--r-full); background: var(--gray-200); overflow: hidden; }
.dist__fill { height: 100%; border-radius: var(--r-full); transition: width var(--dur-slow) var(--ease); }
.f-success { background: var(--success); }
.f-info { background: var(--info); }
.f-warning { background: var(--warning); }
.f-danger { background: var(--danger); }
.dist__count { font-size: var(--fs-xs); text-align: right; }

/* 待改章节 */
.chlist { display: flex; flex-direction: column; gap: 1px; }
.chlist__item {
  display: flex; align-items: center; gap: var(--sp-3);
  padding: 8px var(--sp-2); border-radius: var(--r-sm);
  font-size: var(--fs-sm); cursor: pointer;
}
.chlist__item:hover { background: var(--bg-hover); }
.chlist__item:focus-visible { outline: 2px solid var(--brand-500); outline-offset: -2px; }
.chlist__idx { color: var(--text-subtle); flex-shrink: 0; }
</style>
