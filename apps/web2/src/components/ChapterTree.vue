<script setup lang="ts">
/**
 * ChapterTree · 章节树（写作台左栏）
 *
 * ★ 按卷分组展示；显示字数与状态；当前章高亮
 * ★ 无障碍：role="tree" + aria-expanded（卷可折叠）
 */
import { computed, ref } from 'vue'
import type { ChapterBrief, Volume, PendingDraft } from '@/api/modules/book'
import AppEmpty from '@/components/AppEmpty.vue'
import PendingDraftPanel from '@/components/PendingDraftPanel.vue'

const props = defineProps<{
  chapters: ChapterBrief[]
  volumes: Volume[]
  activeIdx: number
  loading?: boolean
  /** 待审草稿（迁入左栏底部，原 StudioView 头部抽屉） */
  drafts?: PendingDraft[]
  draftsLoading?: boolean
  draftBusyId?: string
  draftsOpen?: boolean
}>()

const emit = defineEmits<{
  select: [idx: number]
  create: []
  accept: [d: PendingDraft]
  edit: [d: PendingDraft]
  discard: [d: PendingDraft]
  'update:draftsOpen': [v: boolean]
}>()

/** 折叠的卷（默认全展开） */
const collapsed = ref<Set<string>>(new Set())

/** ★ 按卷分组：卷覆盖范围内归入该卷，未覆盖的归入"未分卷" */
const groups = computed(() => {
  const vols = [...props.volumes].sort((a, b) => a.idx - b.idx)
  const buckets = vols.map(v => ({
    key: `v-${v.idx}`,
    name: v.name || `第${v.idx}卷`,
    idx: v.idx,
    start: v.start_ch,
    end: v.end_ch ?? Number.MAX_SAFE_INTEGER,
    items: [] as ChapterBrief[],
  }))
  const orphan: ChapterBrief[] = []

  for (const c of props.chapters) {
    const hit = buckets.find(v => c.idx >= v.start && c.idx <= v.end)
    if (hit) hit.items.push(c)
    else orphan.push(c)
  }

  const out = buckets.filter(v => v.items.length > 0)
  if (orphan.length) {
    out.push({
      key: 'v-none', name: '未分卷', idx: 0,
      start: 0, end: 0, items: orphan,
    })
  }
  return out
})

const totalWords = computed(() =>
  props.chapters.reduce((s, c) => s + (c.words || 0), 0))

function toggle(key: string) {
  const s = new Set(collapsed.value)
  if (s.has(key)) s.delete(key)
  else s.add(key)
  collapsed.value = s
}

function fmtWords(n: number) {
  if (n >= 10000) return (n / 10000).toFixed(1) + ' 万'
  return String(n)
}

/** 状态 → 颜色点 */
function dotClass(status: string) {
  if (status === 'done' || status === 'final') return 'ok'
  if (status === 'draft') return 'warn'
  return ''
}
</script>

<template>
  <aside class="tree" aria-label="章节列表">
    <header class="tree__head">
      <div class="tree__title">
        章节
        <span class="muted tiny">{{ chapters.length }} 章 · {{ fmtWords(totalWords) }} 字</span>
      </div>
      <button class="tree__add" type="button" aria-label="新建章节" title="新建章节" @click="emit('create')">＋</button>
    </header>

    <div v-if="loading" class="tree__loading">
      <div v-for="i in 6" :key="i" class="tree__sk" />
    </div>

    <AppEmpty
      v-else-if="!chapters.length"
      icon="📄"
      title="还没有章节"
      description="和创作主编聊聊，让他帮你起草第一章"
      action-text="＋ 新建章节"
      size="sm"
      @action="emit('create')"
    />

    <nav v-else class="tree__body" role="tree">
      <div v-for="g in groups" :key="g.key" class="tree__group">
        <button
          class="tree__ghead"
          type="button"
          role="treeitem"
          :aria-expanded="!collapsed.has(g.key)"
          @click="toggle(g.key)"
        >
          <span class="tree__caret" :class="{ 'is-closed': collapsed.has(g.key) }">▾</span>
          <span class="truncate">{{ g.name }}</span>
          <span class="muted tiny">{{ g.items.length }}</span>
        </button>

        <ul v-show="!collapsed.has(g.key)" class="tree__list" role="group">
          <li v-for="c in g.items" :key="c.id || c.idx">
            <button
              class="tree__item"
              :class="{ 'is-on': c.idx === activeIdx }"
              type="button"
              role="treeitem"
              :aria-selected="c.idx === activeIdx"
              @click="emit('select', c.idx)"
            >
              <span class="tree__dot" :class="dotClass(c.status)" aria-hidden="true" />
              <span class="tree__idx mono">{{ String(c.idx).padStart(3, '0') }}</span>
              <span class="tree__name truncate">{{ c.title || '未命名' }}</span>
              <span class="tree__w mono">{{ fmtWords(c.words || 0) }}</span>
            </button>
          </li>
        </ul>
      </div>
    </nav>

    <!-- ★ 待审草稿：迁入左栏底部（原 StudioView 头部抽屉） -->
    <section
      v-if="(drafts?.length || draftsLoading) && draftsOpen"
      class="tree__drafts"
      aria-label="待审草稿"
    >
      <header class="tree__drafts-head">
        <span class="tree__drafts-title">
          待审草稿<template v-if="drafts?.length"> · {{ drafts.length }}</template>
        </span>
        <button
          class="tree__drafts-toggle"
          type="button"
          :aria-expanded="draftsOpen"
          aria-label="收起待审草稿"
          @click="emit('update:draftsOpen', false)"
        >▾</button>
      </header>
      <div class="tree__drafts-body">
        <PendingDraftPanel
          :drafts="drafts || []"
          :loading="!!draftsLoading"
          :busy-task-id="draftBusyId || ''"
          hide-header
          @accept="emit('accept', $event)"
          @edit="emit('edit', $event)"
          @discard="emit('discard', $event)"
        />
      </div>
    </section>
  </aside>
</template>

<style scoped>
.tree {
  display: flex; flex-direction: column; min-height: 0;
  background: var(--bg-elevated);
  border-right: 1px solid var(--border);
}
.tree__head {
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-3) var(--sp-2);
  border-bottom: 1px solid var(--border);
}
.tree__title { display: flex; flex-direction: column; gap: 1px; font-size: var(--fs-sm); font-weight: var(--fw-bold); }
.tree__add {
  width: 24px; height: 24px; flex-shrink: 0;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: transparent; color: var(--text-muted); font-size: 14px; line-height: 1;
}
.tree__add:hover { background: var(--brand-50); border-color: var(--brand-200); color: var(--brand-600); }

.tree__body { flex: 1 1 auto; overflow-y: auto; padding: var(--sp-2); }

.tree__loading { padding: var(--sp-3); display: flex; flex-direction: column; gap: var(--sp-2); }
.tree__sk {
  height: 26px; border-radius: var(--r-sm);
  background: linear-gradient(90deg, var(--gray-200) 25%, var(--gray-100) 37%, var(--gray-200) 63%);
  background-size: 400% 100%;
  animation: shimmer 1.4s ease-in-out infinite;
}
@keyframes shimmer { 0% { background-position: 100% 50%; } 100% { background-position: 0 50%; } }

.tree__group + .tree__group { margin-top: var(--sp-3); }

.tree__ghead {
  display: flex; align-items: center; gap: var(--sp-2);
  width: 100%; padding: 5px var(--sp-2);
  border: none; border-radius: var(--r-sm);
  background: transparent; color: var(--text-muted);
  font-size: var(--fs-xs); font-weight: var(--fw-medium); text-align: left;
}
.tree__ghead:hover { background: var(--bg-hover); color: var(--text); }
.tree__caret { font-size: 9px; transition: transform var(--dur-fast) var(--ease); }
.tree__caret.is-closed { transform: rotate(-90deg); }

.tree__list { display: flex; flex-direction: column; gap: 1px; margin-top: 2px; }

.tree__item {
  display: flex; align-items: center; gap: var(--sp-2);
  width: 100%; padding: 6px var(--sp-2) 6px var(--sp-3);
  border: none; border-radius: var(--r-sm);
  background: transparent; color: var(--text);
  font-size: var(--fs-xs); text-align: left;
  transition: background-color var(--dur-fast) var(--ease);
}
.tree__item:hover { background: var(--bg-hover); }
.tree__item.is-on { background: var(--brand-50); color: var(--brand-700); font-weight: var(--fw-medium); }
[data-theme="dark"] .tree__item.is-on { color: var(--text); }
.tree__item:focus-visible { outline: 2px solid var(--brand-500); outline-offset: -2px; }

.tree__dot {
  width: 5px; height: 5px; border-radius: 50%;
  background: var(--gray-300); flex-shrink: 0;
}
.tree__dot.ok { background: var(--success); }
.tree__dot.warn { background: var(--warning); }

.tree__idx { color: var(--text-subtle); flex-shrink: 0; font-size: 10px; }
.tree__name { flex: 1 1 auto; min-width: 0; }
.tree__w { color: var(--text-subtle); flex-shrink: 0; font-size: 10px; }

/* 待审草稿（迁入左栏底部） */
.tree__drafts {
  flex-shrink: 0;
  max-height: 42vh;
  display: flex; flex-direction: column; min-height: 0;
  border-top: 1px solid var(--border);
  background: var(--bg-sunken);
}
.tree__drafts-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3) var(--sp-1);
  flex-shrink: 0;
}
.tree__drafts-title { font-size: var(--fs-xs); font-weight: var(--fw-bold); color: var(--text-muted); }
.tree__drafts-toggle {
  width: 20px; height: 20px;
  border: none; border-radius: var(--r-sm);
  background: transparent; color: var(--text-muted);
  font-size: 10px; line-height: 1; cursor: pointer;
}
.tree__drafts-toggle:hover { background: var(--bg-hover); color: var(--text); }
.tree__drafts-body {
  flex: 1 1 auto; min-height: 0; overflow-y: auto;
  padding: 0 var(--sp-2) var(--sp-2);
}
.tree__drafts-body :deep(.pd) {
  border: none;
  box-shadow: none;
  border-radius: 0;
  background: transparent;
}
</style>
