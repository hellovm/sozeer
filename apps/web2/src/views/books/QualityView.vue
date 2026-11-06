<script setup lang="ts">
/**
 * QualityView · 质检报告
 *
 * ★ 与旧版对比验收：
 *   [x] 全书质检概览（平均分 / 分布 / 最差章节）
 *   [x] 逐章质检分列表（可跳转）
 *   [x] 一致性问题（冲突检查）
 *   [x] 伏笔债务（超期提醒）
 *   [x] 运行记录（成本 / 耗时）
 * ★ 只报问题不给方案 = 无用（MEMORY.md §39）→ 每条问题都带 advice
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import settingApi, { type Bible } from '@/api/modules/setting'
import { useUiStore } from '@/stores/ui'
import { ApiError } from '@/types/api'
import AppButton from '@/components/AppButton.vue'
import AppCard from '@/components/AppCard.vue'
import AppTag from '@/components/AppTag.vue'
import AppProgress from '@/components/AppProgress.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppTabs from '@/components/AppTabs.vue'

const route = useRoute()
const router = useRouter()
const ui = useUiStore()

const bookId = computed(() => String(route.params.bookId || ''))
const bible = ref<Bible | null>(null)
const runs = ref<Array<{ id: string; kind: string; model: string; cost: number
                         gate_score: number | null; status: string
                         duration_ms: number; time: string }>>([])
const consistency = ref<{ issues?: Array<{ kind?: string; level?: string
                                            message?: string; advice?: string }> } | null>(null)
const memory = ref<{ obligations?: Array<{ kind?: string; content?: string
                                            due_ch?: number; overdue?: boolean
                                            advice?: string }> } | null>(null)
const loading = ref(true)
const reindexing = ref(false)
const tab = ref('chapters')

/* ── 统计 ── */

const scored = computed(() =>
  (bible.value?.chapters || []).filter(c => c.gate_score != null))

const avgScore = computed(() => {
  if (!scored.value.length) return 0
  return Math.round(scored.value.reduce((s, c) => s + (c.gate_score || 0), 0) / scored.value.length)
})

/** 分数分布（五档） */
const distribution = computed(() => {
  const buckets = [
    { label: '优秀 ≥90', min: 90, max: 101, count: 0, kind: 'success' },
    { label: '良好 75-89', min: 75, max: 90, count: 0, kind: 'info' },
    { label: '及格 60-74', min: 60, max: 75, count: 0, kind: 'warning' },
    { label: '待改 <60', min: -1, max: 60, count: 0, kind: 'danger' },
  ]
  for (const c of scored.value) {
    const b = buckets.find(x => (c.gate_score || 0) >= x.min && (c.gate_score || 0) < x.max)
    if (b) b.count += 1
  }
  return buckets
})

/** 最需要改的章节（分数升序前 8） */
const worst = computed(() =>
  [...scored.value].sort((a, b) => (a.gate_score || 0) - (b.gate_score || 0)).slice(0, 8))

const overdue = computed(() =>
  (memory.value?.obligations || []).filter(o => o.overdue))

onMounted(load)

async function load() {
  loading.value = true
  const [b, r, c, m] = await Promise.allSettled([
    settingApi.bible(bookId.value),
    settingApi.runs(bookId.value, 20),
    settingApi.consistency(bookId.value),
    settingApi.memory(bookId.value),
  ])
  if (b.status === 'fulfilled') bible.value = b.value
  if (r.status === 'fulfilled') runs.value = r.value
  if (c.status === 'fulfilled') consistency.value = c.value
  if (m.status === 'fulfilled') memory.value = m.value
  loading.value = false
}

async function reindex() {
  reindexing.value = true
  try {
    const r = await settingApi.reindex(bookId.value)
    ui.ok(`记忆索引已重建（${r.chunks} 个片段）`)
    await load()
  } catch (e) {
    if (e instanceof ApiError) ui.err(e.message)
  } finally {
    reindexing.value = false
  }
}

function scoreKind(s?: number | null) {
  if (s == null) return 'default' as const
  if (s >= 90) return 'success' as const
  if (s >= 75) return 'info' as const
  if (s >= 60) return 'warning' as const
  return 'danger' as const
}

function openChapter(idx: number) {
  router.push({ name: 'chapter-edit', params: { bookId: bookId.value, idx } })
}

const tabs = [
  { key: 'chapters', label: '逐章评分' },
  { key: 'issues', label: '一致性问题' },
  { key: 'runs', label: '运行记录' },
]
</script>

<template>
  <div class="page">
    <header class="page__head">
      <div class="page__title-row">
        <div>
          <RouterLink class="page__back" :to="{ name: 'studio', params: { bookId } }">← 写作台</RouterLink>
          <h1 class="page__title">质检报告</h1>
          <p class="page__sub">{{ scored.length }} 章已质检 · 平均 {{ avgScore }} 分</p>
        </div>
        <AppButton size="sm" :loading="reindexing" @click="reindex">重建记忆索引</AppButton>
      </div>
    </header>

    <AppSkeleton v-if="loading" variant="card" :count="3" />

    <template v-else>
      <!-- 概览 -->
      <div class="cards">
        <AppCard padding="md">
          <div class="kpi">
            <span class="kpi__k">平均质检分</span>
            <span class="kpi__v" :class="`k-${scoreKind(avgScore)}`">{{ avgScore || '—' }}</span>
            <span class="kpi__s">{{ scored.length }} 章有评分</span>
          </div>
        </AppCard>
        <AppCard padding="md">
          <div class="kpi">
            <span class="kpi__k">待改进章节</span>
            <span class="kpi__v" :class="distribution[3].count ? 'k-danger' : ''">
              {{ distribution[3].count }}
            </span>
            <span class="kpi__s">低于 60 分</span>
          </div>
        </AppCard>
        <AppCard padding="md">
          <div class="kpi">
            <span class="kpi__k">超期伏笔</span>
            <span class="kpi__v" :class="overdue.length ? 'k-warning' : ''">{{ overdue.length }}</span>
            <span class="kpi__s">需要回收</span>
          </div>
        </AppCard>
        <AppCard padding="md">
          <div class="kpi">
            <span class="kpi__k">一致性冲突</span>
            <span class="kpi__v" :class="(consistency?.issues?.length || 0) ? 'k-warning' : ''">
              {{ consistency?.issues?.length || 0 }}
            </span>
            <span class="kpi__s">待核查</span>
          </div>
        </AppCard>
      </div>

      <!-- 分数分布 -->
      <AppCard title="分数分布" padding="md" style="margin-bottom: var(--sp-4)">
        <div class="dist">
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

      <!-- Tab 区 -->
      <AppCard padding="none">
        <div class="tabs-wrap">
          <AppTabs v-model="tab" :items="tabs" />
        </div>

        <!-- 逐章评分 -->
        <div v-if="tab === 'chapters'" class="body-pad">
          <AppEmpty v-if="!worst.length" icon="◎" title="还没有质检数据" size="md"
                    description="去章节编辑器点「重跑质检」，或直接去写作台生成新章节" />
          <div v-else>
            <h4 class="sec__h">最需要改进（{{ worst.length }} 章）</h4>
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
          </div>
        </div>

        <!-- 一致性问题 -->
        <div v-else-if="tab === 'issues'" class="body-pad">
          <AppEmpty v-if="!consistency?.issues?.length" icon="✓" title="没有发现一致性问题" size="md"
                    description="设定、时间线、人物状态均未检出冲突" />
          <ul v-else class="issues">
            <li v-for="(it, i) in consistency.issues" :key="i" class="issues__item">
              <div class="issues__head">
                <AppTag size="sm" :kind="it.level === 'error' ? 'danger'
                  : it.level === 'warn' ? 'warning' : 'default'">
                  {{ it.level || '提示' }}
                </AppTag>
                <span class="muted tiny mono">{{ it.kind || '' }}</span>
              </div>
              <p class="issues__msg">{{ it.message }}</p>
              <!-- ★ 只报问题不给方案 = 无用 -->
              <p v-if="it.advice" class="issues__advice">→ {{ it.advice }}</p>
            </li>
          </ul>

          <!-- 伏笔债务 -->
          <template v-if="overdue.length">
            <h4 class="sec__h" style="margin-top:var(--sp-5)">超期伏笔（{{ overdue.length }}）</h4>
            <ul class="issues">
              <li v-for="(o, i) in overdue" :key="i" class="issues__item issues__item--warn">
                <p class="issues__msg">{{ o.content }}</p>
                <p class="muted tiny">
                  <template v-if="o.due_ch">原计划第 {{ o.due_ch }} 章回收</template>
                </p>
                <p v-if="o.advice" class="issues__advice">→ {{ o.advice }}</p>
              </li>
            </ul>
          </template>
        </div>

        <!-- 运行记录 -->
        <div v-else class="body-pad">
          <AppEmpty v-if="!runs.length" icon="⏱" title="还没有运行记录" size="md" />
          <table v-else class="rtable">
            <thead>
              <tr>
                <th>类型</th><th>模型</th>
                <th style="text-align:right">耗时</th>
                <th style="text-align:right">成本</th>
                <th style="text-align:right">质检</th>
                <th>状态</th><th>时间</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in runs" :key="r.id">
                <td>{{ r.kind }}</td>
                <td class="mono tiny truncate" style="max-width:160px">{{ r.model || '—' }}</td>
                <td class="num mono">{{ (r.duration_ms / 1000).toFixed(1) }}s</td>
                <td class="num mono">¥{{ (r.cost || 0).toFixed(3) }}</td>
                <td class="num">
                  <AppTag v-if="r.gate_score != null" size="sm"
                          :kind="scoreKind(r.gate_score)">{{ r.gate_score }}</AppTag>
                  <span v-else class="muted">—</span>
                </td>
                <td><AppTag size="sm" :kind="r.status === 'ok' ? 'success' : 'danger'">{{ r.status }}</AppTag></td>
                <td class="mono tiny">{{ r.time }}</td>
              </tr>
            </tbody>
          </table>
        </div>
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

.tabs-wrap { padding: 0 var(--sp-4); border-bottom: 1px solid var(--border); }
.body-pad { padding: var(--sp-4); }
.sec__h { font-size: var(--fs-xs); color: var(--text-muted); margin-bottom: var(--sp-3); }

.chlist { display: flex; flex-direction: column; gap: 1px; }
.chlist__item {
  display: flex; align-items: center; gap: var(--sp-3);
  padding: 8px var(--sp-2); border-radius: var(--r-sm);
  font-size: var(--fs-sm); cursor: pointer;
}
.chlist__item:hover { background: var(--bg-hover); }
.chlist__item:focus-visible { outline: 2px solid var(--brand-500); outline-offset: -2px; }
.chlist__idx { color: var(--text-subtle); flex-shrink: 0; }

.issues { display: flex; flex-direction: column; gap: var(--sp-3); }
.issues__item {
  padding: var(--sp-3); border-radius: var(--r-md);
  background: var(--bg); border-left: 2px solid var(--border);
}
.issues__item--warn { border-left-color: var(--warning); }
.issues__head { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: 6px; }
.issues__msg { font-size: var(--fs-sm); line-height: var(--lh-normal); }
.issues__advice { font-size: var(--fs-xs); color: var(--brand-600); margin-top: 6px; }
[data-theme="dark"] .issues__advice { color: var(--brand-200); }

.rtable { width: 100%; border-collapse: collapse; font-size: var(--fs-xs); }
.rtable th, .rtable td { padding: var(--sp-2) var(--sp-3); text-align: left; border-bottom: 1px solid var(--border); }
.rtable th { color: var(--text-muted); font-weight: var(--fw-medium); }
.rtable .num { text-align: right; }
</style>
