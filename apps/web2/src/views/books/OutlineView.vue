<script setup lang="ts">
/**
 * OutlineView · 三级大纲（总纲 · 卷 · 章）
 *
 * ★ 与旧版对比验收：
 *   [x] 总纲（书籍级：题材/平台/目标字数/章节区间）
 *   [x] 卷大纲（可折叠展开）
 *   [x] 章列表（挂在该卷下，含字数/质检分）
 *   [x] 世界观设定分组展示
 * ★ 新增：进度条（按字数目标）、点击章节直达编辑
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

const route = useRoute()
const router = useRouter()
const ui = useUiStore()

const bookId = computed(() => String(route.params.bookId || ''))
const bible = ref<Bible | null>(null)
const loading = ref(true)
const collapsed = ref<Set<number>>(new Set())

/** 卷 → 章节分组 */
const groups = computed(() => {
  const b = bible.value
  if (!b) return []
  const vols = [...b.volumes].sort((x, y) => x.idx - y.idx)
  const buckets = vols.map(v => ({
    idx: v.idx,
    name: v.name || `第${v.idx}卷`,
    outline: v.outline,
    start: v.start_ch,
    end: v.end_ch ?? Number.MAX_SAFE_INTEGER,
    chapters: [] as typeof b.chapters,
    words: 0,
  }))
  const orphan = [] as typeof b.chapters
  for (const c of b.chapters) {
    const hit = buckets.find(v => c.idx >= v.start && c.idx <= v.end)
    if (hit) { hit.chapters.push(c); hit.words += c.words || 0 }
    else orphan.push(c)
  }
  const out = buckets.filter(v => v.chapters.length)
  if (orphan.length) {
    out.push({
      idx: 0, name: '未分卷', outline: '', start: 0, end: 0,
      chapters: orphan, words: orphan.reduce((s, c) => s + (c.words || 0), 0),
    })
  }
  return out
})

/** 世界观分组 */
const worldGroups = computed(() => {
  const m = new Map<string, Array<{ key: string; value: string }>>()
  for (const w of bible.value?.world || []) {
    const c = w.category || '未分类'
    if (!m.has(c)) m.set(c, [])
    m.get(c)!.push({ key: w.key, value: w.value })
  }
  return [...m.entries()].map(([category, items]) => ({ category, items }))
})

/** 字数进度（相对目标） */
const wordProgress = computed(() => {
  const b = bible.value
  if (!b) return 0
  const target = b.book.target_words || 0
  if (!target) return 0
  return Math.min(100, Math.round((b.progress.words / target) * 100))
})

onMounted(load)

async function load() {
  loading.value = true
  try {
    bible.value = await settingApi.bible(bookId.value)
  } catch (e) {
    if (e instanceof ApiError) ui.err(e.message)
  } finally {
    loading.value = false
  }
}

function toggle(idx: number) {
  const s = new Set(collapsed.value)
  if (s.has(idx)) s.delete(idx)
  else s.add(idx)
  collapsed.value = s
}

function openChapter(idx: number) {
  router.push({ name: 'chapter-edit', params: { bookId: bookId.value, idx } })
}

function fmtWords(n: number) {
  if (n >= 10000) return (n / 10000).toFixed(1) + ' 万'
  return n.toLocaleString('zh-CN')
}
</script>

<template>
  <div class="page">
    <header class="page__head">
      <RouterLink class="page__back" :to="{ name: 'studio', params: { bookId } }">← 写作台</RouterLink>
      <h1 class="page__title">大纲 · {{ bible?.book.title || '…' }}</h1>
      <p class="page__sub">三级结构：总纲 → 卷 → 章</p>
    </header>

    <AppSkeleton v-if="loading" variant="card" :count="3" />

    <template v-else-if="bible">
      <!-- 总纲 -->
      <AppCard title="总纲" padding="md">
        <template #actions>
          <AppButton size="sm" variant="text"
                     @click="router.push({ name: 'studio', params: { bookId } })">去调整</AppButton>
        </template>
        <dl class="meta">
          <div class="meta__row">
            <dt>题材</dt>
            <dd>
              <AppTag v-if="bible.book.theme" size="sm" kind="brand">{{ bible.book.theme }}</AppTag>
              <span v-else class="muted">未设置</span>
              <AppTag v-for="t in bible.book.tags" :key="t" size="sm">{{ t }}</AppTag>
            </dd>
          </div>
          <div class="meta__row"><dt>发布平台</dt><dd>{{ bible.book.platform || '未设置' }}</dd></div>
          <div class="meta__row">
            <dt>章节区间</dt>
            <dd class="mono">{{ bible.book.chapter_min }} – {{ bible.book.chapter_max }} 字/章</dd>
          </div>
          <div class="meta__row">
            <dt>目标字数</dt>
            <dd class="mono">{{ fmtWords(bible.book.target_words) }} 字</dd>
          </div>
        </dl>

        <div v-if="bible.book.target_words" class="prog">
          <AppProgress
            :percent="wordProgress"
            size="sm"
            :label="`已完成 ${fmtWords(bible.progress.words)} / ${fmtWords(bible.book.target_words)} 字`"
          />
        </div>
      </AppCard>

      <!-- 卷 + 章 -->
      <AppCard title="卷与章节" padding="none" style="margin-top: var(--sp-4)">
        <AppEmpty v-if="!groups.length" icon="≡" title="还没有大纲" size="md"
                  description="去写作台让主编先出一版总纲与卷结构" />
        <div v-else class="vols">
          <section v-for="g in groups" :key="g.idx" class="vol">
            <button
              class="vol__head"
              type="button"
              :aria-expanded="!collapsed.has(g.idx)"
              @click="toggle(g.idx)"
            >
              <span class="vol__caret" :class="{ 'is-closed': collapsed.has(g.idx) }">▾</span>
              <strong class="grow truncate">{{ g.name }}</strong>
              <span class="muted tiny">{{ g.chapters.length }} 章 · {{ fmtWords(g.words) }} 字</span>
            </button>

            <div v-show="!collapsed.has(g.idx)" class="vol__body">
              <p v-if="g.outline" class="vol__outline">{{ g.outline }}</p>
              <ul class="chlist">
                <li
                  v-for="c in g.chapters"
                  :key="c.idx"
                  class="chlist__item"
                  tabindex="0" role="button"
                  @click="openChapter(c.idx)"
                  @keydown.enter="openChapter(c.idx)"
                >
                  <span class="mono tiny chlist__idx">{{ String(c.idx).padStart(3, '0') }}</span>
                  <span class="grow truncate">{{ c.title || '未命名' }}</span>
                  <span class="muted tiny mono">{{ fmtWords(c.words || 0) }}</span>
                  <AppTag v-if="c.gate_score != null" size="sm"
                          :kind="c.gate_score >= 85 ? 'success'
                            : c.gate_score >= 70 ? 'info' : 'warning'">{{ c.gate_score }}</AppTag>
                </li>
              </ul>
            </div>
          </section>
        </div>
      </AppCard>

      <!-- 世界观 -->
      <AppCard v-if="worldGroups.length" title="世界观设定" padding="md" style="margin-top: var(--sp-4)">
        <div class="world">
          <div v-for="g in worldGroups" :key="g.category" class="world__group">
            <h4 class="world__title">{{ g.category }}</h4>
            <dl class="world__list">
              <div v-for="w in g.items" :key="w.key" class="world__row">
                <dt>{{ w.key }}</dt>
                <dd>{{ w.value }}</dd>
              </div>
            </dl>
          </div>
        </div>
      </AppCard>
    </template>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 900px; margin: 0 auto; }
.page__head { margin-bottom: var(--sp-4); }
.page__back { font-size: var(--fs-xs); color: var(--text-muted); display: block; margin-bottom: 4px; }
.page__back:hover { color: var(--brand-500); }
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); }

/* 总纲 */
.meta { display: flex; flex-direction: column; gap: var(--sp-2); }
.meta__row { display: flex; gap: var(--sp-3); font-size: var(--fs-sm); }
.meta__row dt { flex-shrink: 0; width: 84px; color: var(--text-muted); }
.meta__row dd { margin: 0; display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.prog { margin-top: var(--sp-4); padding-top: var(--sp-3); border-top: 1px solid var(--border); }

/* 卷 */
.vols { display: flex; flex-direction: column; }
.vol + .vol { border-top: 1px solid var(--border); }
.vol__head {
  display: flex; align-items: center; gap: var(--sp-2);
  width: 100%; padding: var(--sp-3) var(--sp-4);
  border: none; background: transparent; color: var(--text);
  font-size: var(--fs-sm); text-align: left;
}
.vol__head:hover { background: var(--bg-hover); }
.vol__caret { font-size: 10px; color: var(--text-subtle); transition: transform var(--dur-fast) var(--ease); }
.vol__caret.is-closed { transform: rotate(-90deg); }

.vol__body { padding: 0 var(--sp-4) var(--sp-3); }
.vol__outline {
  padding: var(--sp-3); margin-bottom: var(--sp-3);
  border-radius: var(--r-md); background: var(--bg-sunken);
  font-size: var(--fs-xs); line-height: var(--lh-relaxed); color: var(--text-muted);
  white-space: pre-wrap;
}

.chlist { display: flex; flex-direction: column; gap: 1px; }
.chlist__item {
  display: flex; align-items: center; gap: var(--sp-3);
  padding: 7px var(--sp-2); border-radius: var(--r-sm);
  font-size: var(--fs-sm); cursor: pointer;
  transition: background-color var(--dur-fast) var(--ease);
}
.chlist__item:hover { background: var(--bg-hover); }
.chlist__item:focus-visible { outline: 2px solid var(--brand-500); outline-offset: -2px; }
.chlist__idx { color: var(--text-subtle); flex-shrink: 0; }

/* 世界观 */
.world { display: flex; flex-direction: column; gap: var(--sp-4); }
.world__title { font-size: var(--fs-xs); color: var(--text-muted); margin-bottom: var(--sp-2); }
.world__list { display: flex; flex-direction: column; gap: 6px; }
.world__row { display: flex; gap: var(--sp-3); font-size: var(--fs-xs); }
.world__row dt { flex-shrink: 0; width: 88px; color: var(--text-subtle); }
.world__row dd { margin: 0; flex: 1 1 auto; min-width: 0; word-break: break-word; line-height: var(--lh-normal); }
</style>
