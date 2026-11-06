<script setup lang="ts">
/**
 * SettingPanel · 设定侧栏（写作台右栏）
 *
 * Tab：人物 / 世界观 / 伏笔 / 进度
 * ★ 数据来自 GET /api/books/{id}/state（一次拉全，避免多次请求）
 */
import { computed, ref } from 'vue'
import type { BookState } from '@/api/modules/book'
import AppTabs from '@/components/AppTabs.vue'
import type { TabItem } from '@/types/components'
import AppEmpty from '@/components/AppEmpty.vue'
import AppTag from '@/components/AppTag.vue'

const props = defineProps<{
  state: BookState | null
  loading?: boolean
}>()

const tab = ref('chars')

const tabs = computed<TabItem[]>(() => [
  { key: 'chars', label: '人物', badge: props.state?.characters.length || '' },
  { key: 'world', label: '世界观', badge: props.state?.world.length || '' },
  { key: 'fs', label: '伏笔', badge: props.state?.progress.open_foreshadows || '' },
  { key: 'progress', label: '进度' },
])

const openFs = computed(() =>
  (props.state?.foreshadows || []).filter(f => f.status === 'open'))
const doneFs = computed(() =>
  (props.state?.foreshadows || []).filter(f => f.status !== 'open'))

/** 世界观按 category 分组 */
const worldGroups = computed(() => {
  const m = new Map<string, Array<{ key: string; value: string }>>()
  for (const w of props.state?.world || []) {
    const c = w.category || '未分类'
    if (!m.has(c)) m.set(c, [])
    m.get(c)!.push({ key: w.key, value: w.value })
  }
  return [...m.entries()].map(([category, items]) => ({ category, items }))
})

function roleKind(role: string) {
  if (role === '主角' || role === 'protagonist') return 'brand'
  if (role === '反派' || role === 'antagonist') return 'danger'
  return 'default'
}
</script>

<template>
  <aside class="panel" aria-label="设定侧栏">
    <div class="panel__tabs">
      <AppTabs v-model="tab" :items="tabs" variant="line" size="sm" />
    </div>

    <div class="panel__body">
      <!-- 加载 -->
      <div v-if="loading" class="panel__sk">
        <div v-for="i in 4" :key="i" class="panel__sk-item" />
      </div>

      <template v-else>
        <!-- ── 人物 ── -->
        <template v-if="tab === 'chars'">
          <AppEmpty
            v-if="!state?.characters.length"
            icon="☺" title="还没有人物" size="sm"
            description="在对话里提到的人物会自动识别并建档"
          />
          <ul v-else class="plist">
            <li v-for="c in state.characters" :key="c.name" class="plist__item">
              <div class="plist__row">
                <span class="plist__avatar" aria-hidden="true">{{ c.name.slice(0, 1) }}</span>
                <strong class="truncate">{{ c.name }}</strong>
                <AppTag v-if="c.role" size="sm" :kind="roleKind(c.role)">{{ c.role }}</AppTag>
              </div>
              <p v-if="c.state" class="plist__state">{{ c.state }}</p>
              <p v-if="c.profile" class="plist__profile">{{ c.profile }}</p>
            </li>
          </ul>
        </template>

        <!-- ── 世界观 ── -->
        <template v-else-if="tab === 'world'">
          <AppEmpty
            v-if="!worldGroups.length"
            icon="🌏" title="还没有设定" size="sm"
            description="境界、门派、地理等设定会自动沉淀在这里"
          />
          <div v-for="g in worldGroups" :key="g.category" class="wgroup">
            <h4 class="wgroup__title">{{ g.category }}</h4>
            <dl class="wlist">
              <div v-for="w in g.items" :key="w.key" class="wlist__row">
                <dt>{{ w.key }}</dt>
                <dd>{{ w.value }}</dd>
              </div>
            </dl>
          </div>
        </template>

        <!-- ── 伏笔 ── -->
        <template v-else-if="tab === 'fs'">
          <AppEmpty
            v-if="!state?.foreshadows.length"
            icon="🪝" title="还没有伏笔" size="sm"
            description="埋下的线索会自动跟踪，超期会提醒"
          />
          <template v-else>
            <h4 class="wgroup__title">待回收（{{ openFs.length }}）</h4>
            <ul class="fslist">
              <li v-for="(f, i) in openFs" :key="`o${i}`" class="fslist__item fslist__item--open">
                <span class="fslist__ch mono">第{{ f.planted_ch }}章</span>
                <span class="fslist__text">{{ f.content }}</span>
              </li>
            </ul>
            <template v-if="doneFs.length">
              <h4 class="wgroup__title" style="margin-top:var(--sp-4)">已回收（{{ doneFs.length }}）</h4>
              <ul class="fslist">
                <li v-for="(f, i) in doneFs" :key="`d${i}`" class="fslist__item">
                  <span class="fslist__ch mono">第{{ f.planted_ch }}章</span>
                  <span class="fslist__text">{{ f.content }}</span>
                  <AppTag v-if="f.payoff_ch" size="sm" kind="success">第{{ f.payoff_ch }}章回收</AppTag>
                </li>
              </ul>
            </template>
          </template>
        </template>

        <!-- ── 进度 ── -->
        <template v-else>
          <div class="prog">
            <div class="prog__row">
              <span class="muted">已写章节</span>
              <strong class="mono">{{ state?.progress.chapters || 0 }}</strong>
            </div>
            <div class="prog__row">
              <span class="muted">总字数</span>
              <strong class="mono">{{ (state?.progress.words || 0).toLocaleString('zh-CN') }}</strong>
            </div>
            <div class="prog__row">
              <span class="muted">最新章节</span>
              <strong class="mono">第 {{ state?.progress.last_idx || 0 }} 章</strong>
            </div>
            <div class="prog__row">
              <span class="muted">待回收伏笔</span>
              <strong class="mono" :style="state?.progress.open_foreshadows
                ? 'color:var(--warning)' : ''">{{ state?.progress.open_foreshadows || 0 }}</strong>
            </div>
          </div>
        </template>
      </template>
    </div>
  </aside>
</template>

<style scoped>
.panel {
  display: flex; flex-direction: column; min-height: 0;
  background: var(--bg-elevated);
  border-left: 1px solid var(--border);
}
.panel__tabs { padding: 0 var(--sp-2); border-bottom: 1px solid var(--border); }
.panel__body { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: var(--sp-3); }

.panel__sk { display: flex; flex-direction: column; gap: var(--sp-3); }
.panel__sk-item {
  height: 48px; border-radius: var(--r-md);
  background: linear-gradient(90deg, var(--gray-200) 25%, var(--gray-100) 37%, var(--gray-200) 63%);
  background-size: 400% 100%; animation: shimmer 1.4s ease-in-out infinite;
}
@keyframes shimmer { 0% { background-position: 100% 50%; } 100% { background-position: 0 50%; } }

/* 人物 */
.plist { display: flex; flex-direction: column; gap: var(--sp-3); }
.plist__item {
  padding: var(--sp-3);
  border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--bg);
}
.plist__row { display: flex; align-items: center; gap: var(--sp-2); }
.plist__avatar {
  width: 22px; height: 22px; flex-shrink: 0;
  display: grid; place-items: center;
  border-radius: 50%; background: var(--brand-100); color: var(--brand-600);
  font-size: 11px; font-weight: 700;
}
.plist__row strong { flex: 1 1 auto; min-width: 0; font-size: var(--fs-sm); }
.plist__state { margin-top: 6px; font-size: var(--fs-xs); color: var(--text-muted); }
.plist__profile {
  margin-top: 4px; font-size: var(--fs-xs); color: var(--text-subtle);
  display: -webkit-box; -webkit-line-clamp: 3; line-clamp: 3;
  -webkit-box-orient: vertical; overflow: hidden;
}

/* 世界观 */
.wgroup + .wgroup { margin-top: var(--sp-4); }
.wgroup__title {
  font-size: var(--fs-xs); color: var(--text-muted);
  text-transform: none; margin-bottom: var(--sp-2);
}
.wlist { display: flex; flex-direction: column; gap: 6px; }
.wlist__row { display: flex; gap: var(--sp-2); font-size: var(--fs-xs); }
.wlist__row dt { flex-shrink: 0; width: 68px; color: var(--text-subtle); }
.wlist__row dd { margin: 0; flex: 1 1 auto; min-width: 0; word-break: break-word; }

/* 伏笔 */
.fslist { display: flex; flex-direction: column; gap: 6px; }
.fslist__item {
  display: flex; align-items: flex-start; gap: var(--sp-2);
  padding: var(--sp-2); border-radius: var(--r-sm);
  background: var(--bg); font-size: var(--fs-xs);
}
.fslist__item--open { border-left: 2px solid var(--warning); }
.fslist__ch { flex-shrink: 0; color: var(--text-subtle); }
.fslist__text { flex: 1 1 auto; min-width: 0; line-height: var(--lh-normal); }

/* 进度 */
.prog { display: flex; flex-direction: column; gap: var(--sp-3); }
.prog__row {
  display: flex; align-items: center; justify-content: space-between;
  padding: var(--sp-3);
  border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--bg); font-size: var(--fs-xs);
}
.prog__row strong { font-size: var(--fs-base); }
</style>
