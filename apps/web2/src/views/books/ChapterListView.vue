<script setup lang="ts">
/**
 * ChapterListView · 章节列表
 *
 * ★ 与旧版对比验收：
 *   [x] 按卷分组展示
 *   [x] 字数 / 状态 / 质检分
 *   [x] 搜索过滤
 *   [x] 点击进入编辑
 * ★ 新增：质检分色阶、批量重跑质检、排序切换
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import bookApi, { type BookDetail, type ChapterBrief, type BookState } from '@/api/modules/book'
import { useUiStore } from '@/stores/ui'
import AppButton from '@/components/AppButton.vue'
import AppCard from '@/components/AppCard.vue'
import AppInput from '@/components/AppInput.vue'
import AppTag from '@/components/AppTag.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'

const route = useRoute()
const router = useRouter()
const ui = useUiStore()

const bookId = computed(() => String(route.params.bookId || ''))
const book = ref<BookDetail | null>(null)
const chapters = ref<ChapterBrief[]>([])
const state = ref<BookState | null>(null)
const loading = ref(true)
const keyword = ref('')
const sort = ref<'idx' | 'words' | 'score'>('idx')
const rechecking = ref(false)
/* ★ v1.0-E4.11：书稿导出（txt/md/epub/docx/pdf/投稿包），带鉴权 Blob 下载 */
const exporting = ref(false)
const EXPORTS = [
  { fmt: 'docx', label: 'DOCX 文档' },
  { fmt: 'pdf', label: 'PDF 文档' },
  { fmt: 'epub', label: 'EPUB 电子书' },
  { fmt: 'txt', label: 'TXT 纯文本' },
  { fmt: 'md', label: 'Markdown' },
  { fmt: 'publish', label: '投稿包（含设定集）' },
] as const

async function doExport(fmt: string) {
  exporting.value = true
  try {
    const blob = await bookApi.exportBook(bookId.value, fmt)
    const ext = fmt === 'publish' ? 'txt' : fmt
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${(book.value?.title || 'book').slice(0, 40)}.${ext}`
    a.click()
    URL.revokeObjectURL(url)
    ui.ok('导出已开始下载')
  } catch (e) {
    ui.err((e as Error).message || '导出失败')
  } finally { exporting.value = false }
}

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  let list = chapters.value
  if (kw) list = list.filter(c => (c.title || '').toLowerCase().includes(kw))
  const arr = [...list]
  if (sort.value === 'words') arr.sort((a, b) => (b.words || 0) - (a.words || 0))
  else if (sort.value === 'score') arr.sort((a, b) => (a.gate_score ?? 999) - (b.gate_score ?? 999))
  else arr.sort((a, b) => a.idx - b.idx)
  return arr
})

/** 按卷分组 */
const groups = computed(() => {
  const vols = [...(state.value?.volumes || [])].sort((a, b) => a.idx - b.idx)
  const buckets = vols.map(v => ({
    name: v.name || `第${v.idx}卷`,
    start: v.start_ch,
    end: v.end_ch ?? Number.MAX_SAFE_INTEGER,
    items: [] as ChapterBrief[],
  }))
  const orphan: ChapterBrief[] = []
  for (const c of filtered.value) {
    const hit = buckets.find(v => c.idx >= v.start && c.idx <= v.end)
    if (hit) hit.items.push(c)
    else orphan.push(c)
  }
  const out = buckets.filter(b => b.items.length)
  if (orphan.length) out.push({ name: '未分卷', start: 0, end: 0, items: orphan })
  return out
})

const stats = computed(() => {
  const total = chapters.value.length
  const words = chapters.value.reduce((s, c) => s + (c.words || 0), 0)
  const scored = chapters.value.filter(c => c.gate_score != null)
  const avg = scored.length
    ? Math.round(scored.reduce((s, c) => s + (c.gate_score || 0), 0) / scored.length)
    : 0
  return { total, words, avg }
})

onMounted(load)

async function load() {
  loading.value = true
  const [b, c, s] = await Promise.allSettled([
    bookApi.get(bookId.value), bookApi.chapters(bookId.value), bookApi.state(bookId.value),
  ])
  if (b.status === 'fulfilled') book.value = b.value
  if (c.status === 'fulfilled') chapters.value = c.value
  if (s.status === 'fulfilled') state.value = s.value
  loading.value = false
}

function open(idx: number) {
  router.push({ name: 'chapter-edit', params: { bookId: bookId.value, idx } })
}

/** 质检分 → 语义色 */
function scoreKind(s?: number) {
  if (s == null) return 'default' as const
  if (s >= 90) return 'success' as const
  if (s >= 75) return 'info' as const
  if (s >= 60) return 'warning' as const
  return 'danger' as const
}

function statusLabel(s: string) {
  return ({ draft: '草稿', done: '已完成', final: '定稿', empty: '空' } as Record<string, string>)[s] || s || '—'
}

/** 批量重跑质检（逐章串行，避免打爆后端） */
async function recheckAll() {
  if (!chapters.value.length) return
  rechecking.value = true
  let ok = 0
  try {
    for (const c of chapters.value) {
      try {
        const r = await bookApi.recheckGate(bookId.value, c.idx)
        const sc = (r as { score?: number }).score
        if (sc != null) c.gate_score = sc
        ok += 1
      } catch { /* 单章失败继续 */ }
    }
    ui.ok(`已重跑 ${ok}/${chapters.value.length} 章质检`)
  } finally {
    rechecking.value = false
  }
}
</script>

<template>
  <div class="page">
    <header class="page__head">
      <div class="page__title-row">
        <div>
          <RouterLink class="page__back" :to="{ name: 'studio', params: { bookId } }">← 写作台</RouterLink>
          <h1 class="page__title">章节 · {{ book?.title || '…' }}</h1>
          <p class="page__sub">
            {{ stats.total }} 章 · {{ stats.words.toLocaleString('zh-CN') }} 字
            <template v-if="stats.avg"> · 平均质检 <b>{{ stats.avg }}</b></template>
          </p>
        </div>
        <div class="row gap-2">
          <!-- ★ v1.0-E4.11：导出下拉（原生 details 无 JS 依赖，风格与全站一致） -->
          <details class="exp">
            <summary class="btn btn--sm" :class="{ 'is-busy': exporting }">导出 ▾</summary>
            <div class="exp__menu" role="menu">
              <button v-for="e in EXPORTS" :key="e.fmt" role="menuitem"
                      :disabled="exporting" @click="doExport(e.fmt)">{{ e.label }}</button>
            </div>
          </details>
          <AppButton size="sm" :loading="rechecking" @click="recheckAll">重跑质检</AppButton>
          <AppButton variant="primary" size="sm"
                     @click="router.push({ name: 'studio', params: { bookId } })">去写作台生成</AppButton>
        </div>
      </div>
    </header>

    <div class="toolbar">
      <div class="toolbar__search">
        <AppInput v-model="keyword" size="sm" placeholder="搜索章节名" clearable aria-label="搜索章节" />
      </div>
      <select v-model="sort" class="toolbar__select" aria-label="排序">
        <option value="idx">按章节顺序</option>
        <option value="words">按字数</option>
        <option value="score">按质检分（低优先）</option>
      </select>
    </div>

    <AppSkeleton v-if="loading" variant="table" :rows="8" />

    <AppEmpty
      v-else-if="!filtered.length"
      :icon="keyword ? '🔍' : '📄'"
      :title="keyword ? '没有匹配的章节' : '还没有章节'"
      :description="keyword ? '换个关键词试试' : '去写作台让主编起草第一章'"
      :action-text="keyword ? '' : '去写作台'"
      @action="router.push({ name: 'studio', params: { bookId } })"
    />

    <div v-else class="groups">
      <AppCard v-for="g in groups" :key="g.name" padding="none">
        <template #header>
          <div class="row-between grow">
            <strong>{{ g.name }}</strong>
            <span class="muted tiny">{{ g.items.length }} 章</span>
          </div>
        </template>
        <div class="ctable">
          <div
            v-for="c in g.items"
            :key="c.id || c.idx"
            class="ctable__row"
            tabindex="0" role="button"
            @click="open(c.idx)"
            @keydown.enter="open(c.idx)"
          >
            <span class="ctable__idx mono">{{ String(c.idx).padStart(3, '0') }}</span>
            <span class="ctable__title truncate">{{ c.title || '未命名' }}</span>
            <span class="ctable__words mono">{{ (c.words || 0).toLocaleString('zh-CN') }} 字</span>
            <AppTag size="sm" :kind="c.status === 'draft' ? 'warning' : 'default'">
              {{ statusLabel(c.status) }}
            </AppTag>
            <AppTag v-if="c.gate_score != null" size="sm" :kind="scoreKind(c.gate_score)">
              {{ c.gate_score }}
            </AppTag>
            <span v-else class="muted tiny">未质检</span>
          </div>
        </div>
      </AppCard>
    </div>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 900px; margin: 0 auto; }
.page__head { margin-bottom: var(--sp-4); }
.page__title-row { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--sp-4); }
.page__back { font-size: var(--fs-xs); color: var(--text-muted); display: block; margin-bottom: 4px; }
.page__back:hover { color: var(--brand-500); }
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); }

.toolbar { display: flex; align-items: center; gap: var(--sp-3); margin-bottom: var(--sp-4); }
.toolbar__search { flex: 1 1 auto; max-width: 280px; }
.toolbar__select {
  height: 30px; padding: 0 var(--sp-2);
  border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--bg-elevated); color: var(--text); font-size: var(--fs-xs);
}

.groups { display: flex; flex-direction: column; gap: var(--sp-4); }

.ctable { display: flex; flex-direction: column; }
.ctable__row {
  display: grid;
  grid-template-columns: 40px minmax(0, 1fr) 88px 64px 56px;
  gap: var(--sp-3); align-items: center;
  padding: var(--sp-3) var(--sp-4);
  border-top: 1px solid var(--border);
  font-size: var(--fs-sm); cursor: pointer;
  transition: background-color var(--dur-fast) var(--ease);
}
.ctable__row:hover { background: var(--bg-hover); }
.ctable__row:focus-visible { outline: 2px solid var(--brand-500); outline-offset: -2px; }
.ctable__idx { color: var(--text-subtle); font-size: var(--fs-xs); }
.ctable__words { color: var(--text-muted); font-size: var(--fs-xs); text-align: right; }

@media (max-width: 640px) {
  .ctable__row { grid-template-columns: 36px minmax(0, 1fr) 56px; }
  .ctable__words, .ctable__row > :nth-child(4) { display: none; }
}

/* 导出下拉（details/summary 原生实现） */
.exp { position: relative; }
.exp summary {
  list-style: none; cursor: pointer; user-select: none;
  display: inline-flex; align-items: center; gap: 4px;
  border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--bg-elevated); color: var(--text);
  padding: 4px 12px; font-size: var(--fs-xs);
}
.exp summary::-webkit-details-marker { display: none; }
.exp summary:hover { border-color: var(--brand-500); color: var(--brand); }
.exp summary.is-busy { opacity: .6; pointer-events: none; }
.exp__menu {
  position: absolute; top: calc(100% + 4px); right: 0; z-index: 30;
  min-width: 176px; padding: 4px;
  background: var(--bg-elevated); border: 1px solid var(--border);
  border-radius: var(--r-md); box-shadow: var(--sh-md);
  display: flex; flex-direction: column;
}
.exp__menu button {
  text-align: left; padding: 7px 10px; border: 0; border-radius: var(--r-sm);
  background: transparent; color: var(--text); font-size: var(--fs-xs); cursor: pointer;
}
.exp__menu button:hover { background: var(--brand-soft); color: var(--brand); }
.exp__menu button:disabled { opacity: .5; cursor: not-allowed; }
</style>
