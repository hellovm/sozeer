<script setup lang="ts">
/**
 * ChapterEditView · 章节编辑器
 *
 * ★ 与旧版对比验收：
 *   [x] 正文编辑 + 实时字数统计
 *   [x] 保存（Ctrl/⌘+S）
 *   [x] 版本历史列表 + 查看
 *   [x] 质检报告展示（分层：问题 + 建议）
 *   [x] 离开未保存提醒（onBeforeRouteLeave）
 * ★ 新增：自动保存草稿到本地（防误关丢稿）、字数变化提示、质检重跑
 */
import { computed, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import bookApi, { type Chapter, type GateReport, type ChapterBrief } from '@/api/modules/book'
import { useUiStore } from '@/stores/ui'
import { ApiError } from '@/types/api'
import AppButton from '@/components/AppButton.vue'
import AppCard from '@/components/AppCard.vue'
import AppInput from '@/components/AppInput.vue'
import AppTag from '@/components/AppTag.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppModal from '@/components/AppModal.vue'
import AppEmpty from '@/components/AppEmpty.vue'

const route = useRoute()
const router = useRouter()
const ui = useUiStore()

const bookId = computed(() => String(route.params.bookId || ''))
const idx = computed(() => Number(route.params.idx || 0))

const chapter = ref<Chapter | null>(null)
const siblings = ref<ChapterBrief[]>([])
const loading = ref(true)
const saving = ref(false)

/** 编辑态（与 chapter 分离，便于比对"是否有改动"） */
const title = ref('')
const content = ref('')

const historyOpen = ref(false)
const history = ref<Array<{ commit: string; message?: string; date?: string }>>([])
const historyLoading = ref(false)

const gateRunning = ref(false)

/**
 * ★ v1.0-E4.17：目标章节服务端尚未建行（GET 404），由本地草稿预填进入编辑器。
 *   此时编辑态内容与 mock 章节内容一致（dirty 本会为 false），但**尚未入库**，
 *   必须强制可保存 —— 故 dirty 在 isNewChapter 时恒 true，保存成功/离开后复位。
 */
const isNewChapter = ref(false)

const LOCAL_KEY = computed(() => `sozeer.draft.${bookId.value}.${idx.value}`)

/** 实时字数（★ 与后端 gates.count_words 口径一致：去标题、去空白） */
const words = computed(() => {
  const t = content.value.replace(/\s+/g, '')
  return t.length
})

const dirty = computed(() =>
  Boolean(chapter.value) &&
  (isNewChapter.value ||
   title.value !== (chapter.value?.title || '') ||
   content.value !== (chapter.value?.content || '')))

const gate = computed<GateReport | null>(() => chapter.value?.gate_report || null)

const gateIssues = computed(() => {
  const g = gate.value
  const arr = Array.isArray(g?.issues) ? g!.issues! : []
  return arr as Array<{ rule?: string; level?: string; message?: string; advice?: string }>
})

onMounted(load)

async function load() {
  loading.value = true
  try {
    const [c, list] = await Promise.allSettled([
      bookApi.chapter(bookId.value, idx.value, { silent: true }),
      bookApi.chapters(bookId.value),
    ])
    if (c.status === 'fulfilled') {
      isNewChapter.value = false
      chapter.value = c.value
      title.value = c.value.title || ''
      content.value = c.value.content || ''
      // ★ 有本地草稿且与服务器不同 → 提示恢复（防误关丢稿）
      restoreLocalIfNewer()
    } else if (c.reason instanceof ApiError && c.reason.isNotFound) {
      // ★ v1.0-E4.17：目标章节尚未建行（新书首次起草的待审草稿常指向还没入库的章）。
      //   若有本地草稿（StudioView「在编辑器中查看」预填的 sozeer.draft.{book}.{idx}）
      //   → **停在编辑器预填正文**，点「保存」走后端 PATCH upsert 建行；
      //   否则才跳回章节列表（真正无内容的空章，跳回是正确行为）。
      const local = readLocalDraft()
      if (local && local.content) {
        isNewChapter.value = true          // ★ 未入库：强制可保存
        chapter.value = {
          idx: idx.value,
          title: local.title || `第${idx.value}章`,
          content: local.content,
          summary: '', words: 0, status: 'draft',
          gate_score: 0, gate_report: {},
        }
        title.value = local.title || `第${idx.value}章`
        content.value = local.content
        ui.warn('该章节尚未入库，已载入草稿正文；点「保存」将创建此章。', 6000)
      } else {
        ui.err('章节不存在')
        router.replace({ name: 'chapters', params: { bookId: bookId.value } })
      }
    }
    if (list.status === 'fulfilled') siblings.value = list.value
  } finally {
    loading.value = false
  }
}

/* ── 本地草稿（防误关） ── */

/** 读本地草稿（sozeer.draft.{book}.{idx}）。返回 null = 无。 */
function readLocalDraft(): { title: string; content: string; at: number } | null {
  try {
    const raw = localStorage.getItem(LOCAL_KEY.value)
    if (!raw) return null
    const d = JSON.parse(raw) as { title?: string; content?: string; at?: number }
    if (!d || typeof d.content !== 'string' || !d.content) return null
    return { title: d.title || '', content: d.content, at: d.at || 0 }
  } catch { return null }
}

function restoreLocalIfNewer() {
  const d = readLocalDraft()
  if (d && d.content !== chapter.value?.content) {
    // ★ 只在本地更新时提示（避免旧草稿覆盖新正文）
    ui.warn('检测到未保存的本地草稿，已自动恢复。如需放弃请点击「恢复服务端」', 8000)
    title.value = d.title || title.value
    content.value = d.content
  }
}

function saveLocal() {
  try {
    localStorage.setItem(LOCAL_KEY.value,
      JSON.stringify({ title: title.value, content: content.value, at: Date.now() }))
  } catch { /* 隐私模式可能不可写 */ }
}

function clearLocal() {
  try { localStorage.removeItem(LOCAL_KEY.value) } catch { /* ignore */ }
}

/** ★ 编辑中自动存本地（1.5s 防抖） */
let localTimer: number | undefined
watch([title, content], () => {
  if (!dirty.value) return
  clearTimeout(localTimer)
  localTimer = window.setTimeout(saveLocal, 1500)
})

/* ── 保存 ── */

async function save() {
  if (!dirty.value || saving.value) return
  saving.value = true
  try {
    const r = await bookApi.saveChapter(bookId.value, idx.value, {
      title: title.value,
      content: content.value,
    })
    if (chapter.value) {
      chapter.value.title = title.value
      chapter.value.content = content.value
      chapter.value.words = r.words ?? words.value
    }
    isNewChapter.value = false            // ★ 已入库，回到正常 dirty 比对
    clearLocal()
    ui.ok(`已保存（${(r.words ?? words.value).toLocaleString('zh-CN')} 字）`)
  } catch (e) {
    if (e instanceof ApiError) ui.err(e.message)
  } finally {
    saving.value = false
  }
}

function revertToServer() {
  if (!chapter.value) return
  title.value = chapter.value.title || ''
  content.value = chapter.value.content || ''
  clearLocal()
  ui.info('已恢复到服务端版本')
}

/** ★ Ctrl/⌘ + S 保存 */
function onKeydown(e: KeyboardEvent) {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
    e.preventDefault()
    void save()
  }
}

/* ── 版本历史 ── */

async function openHistory() {
  historyOpen.value = true
  historyLoading.value = true
  try {
    history.value = await bookApi.chapterHistory(bookId.value, idx.value)
  } catch { history.value = [] } finally {
    historyLoading.value = false
  }
}

/* ── 质检 ── */

async function runGate() {
  gateRunning.value = true
  try {
    const r = await bookApi.recheckGate(bookId.value, idx.value)
    if (chapter.value) {
      chapter.value.gate_report = r
      chapter.value.gate_score = Number((r as { score?: number }).score ?? 0)
    }
    ui.ok('质检完成')
  } catch (e) {
    if (e instanceof ApiError) ui.err(e.message)
  } finally {
    gateRunning.value = false
  }
}

/* ── 导航 ── */

const prevIdx = computed(() => {
  const i = siblings.value.findIndex(c => c.idx === idx.value)
  return i > 0 ? siblings.value[i - 1].idx : 0
})
const nextIdx = computed(() => {
  const i = siblings.value.findIndex(c => c.idx === idx.value)
  return i >= 0 && i < siblings.value.length - 1 ? siblings.value[i + 1].idx : 0
})

function goto(i: number) {
  if (!i) return
  router.push({ name: 'chapter-edit', params: { bookId: bookId.value, idx: i } })
}

/** ★ 离开前提醒未保存（防误操作丢稿） */
onBeforeRouteLeave(async () => {
  if (!dirty.value) return true
  return window.confirm('有未保存的修改，确定离开吗？（本地草稿已保留）')
})

function scoreKind(s?: number) {
  if (s == null) return 'default' as const
  if (s >= 90) return 'success' as const
  if (s >= 75) return 'info' as const
  if (s >= 60) return 'warning' as const
  return 'danger' as const
}
</script>

<template>
  <div class="page" @keydown="onKeydown">
    <header class="page__bar">
      <div class="row gap-3 grow">
        <RouterLink class="page__back" :to="{ name: 'chapters', params: { bookId } }">← 章节</RouterLink>
        <span class="mono muted tiny">第 {{ idx }} 章</span>
        <AppTag v-if="chapter?.gate_score != null" size="sm" :kind="scoreKind(chapter.gate_score)">
          质检 {{ chapter.gate_score }}
        </AppTag>
        <AppTag v-if="dirty" size="sm" kind="warning">未保存</AppTag>
      </div>
      <div class="row gap-2">
        <AppButton size="sm" variant="text" :disabled="!prevIdx" @click="goto(prevIdx)">‹ 上一章</AppButton>
        <AppButton size="sm" variant="text" :disabled="!nextIdx" @click="goto(nextIdx)">下一章 ›</AppButton>
        <AppButton size="sm" @click="openHistory">版本历史</AppButton>
        <AppButton size="sm" :loading="gateRunning" @click="runGate">重跑质检</AppButton>
        <AppButton variant="primary" size="sm" :loading="saving" :disabled="!dirty" @click="save">
          保存
        </AppButton>
      </div>
    </header>

    <AppSkeleton v-if="loading" variant="text" :rows="8" />

    <div v-else class="edit">
      <div class="edit__main">
        <AppInput v-model="title" placeholder="章节标题" :maxlength="80" size="lg" aria-label="章节标题" />
        <textarea
          v-model="content"
          class="edit__area"
          placeholder="开始写正文…（Ctrl/⌘ + S 保存）"
          aria-label="章节正文"
          spellcheck="false"
        />
        <div class="edit__foot">
          <span class="muted tiny">{{ words.toLocaleString('zh-CN') }} 字</span>
          <span v-if="dirty" class="muted tiny">· 有未保存修改</span>
          <span class="spacer" />
          <AppButton v-if="dirty" size="sm" variant="text" @click="revertToServer">恢复服务端</AppButton>
        </div>
      </div>

      <!-- 质检报告侧栏 -->
      <aside class="edit__side">
        <AppCard title="质检报告" padding="md">
          <template #actions>
            <AppButton v-if="dirty" size="sm" variant="text" @click="save">先保存</AppButton>
          </template>

          <div v-if="!gate" class="muted tiny">还没有质检结果，点上方「重跑质检」</div>
          <template v-else>
            <div class="score">
              <span class="score__v" :class="`k-${scoreKind(chapter?.gate_score)}`">
                {{ chapter?.gate_score ?? '—' }}
              </span>
              <span class="muted tiny">综合得分</span>
            </div>

            <template v-if="gateIssues.length">
              <h4 class="side__h">问题（{{ gateIssues.length }}）</h4>
              <ul class="issues">
                <li v-for="(it, i) in gateIssues" :key="i" class="issues__item">
                  <div class="issues__head">
                    <AppTag size="sm" :kind="it.level === 'error' ? 'danger'
                      : it.level === 'warn' ? 'warning' : 'default'">
                      {{ it.level || '提示' }}
                    </AppTag>
                    <span class="issues__rule mono">{{ it.rule || '' }}</span>
                  </div>
                  <p class="issues__msg">{{ it.message }}</p>
                  <p v-if="it.advice" class="issues__advice">→ {{ it.advice }}</p>
                </li>
              </ul>
            </template>
            <AppEmpty v-else icon="✓" title="没有发现问题" size="sm" />
          </template>
        </AppCard>
      </aside>
    </div>

    <!-- 版本历史 -->
    <AppModal v-model="historyOpen" title="版本历史" width="560px" hide-footer>
      <div v-if="historyLoading" class="muted tiny">加载中…</div>
      <AppEmpty v-else-if="!history.length" icon="🕐" title="还没有历史版本" size="sm"
                description="每次保存都会生成一个版本" />
      <ul v-else class="hist">
        <li v-for="h in history" :key="h.commit" class="hist__item">
          <span class="mono tiny">{{ h.commit.slice(0, 7) }}</span>
          <span class="grow truncate">{{ h.message || '编辑' }}</span>
          <span class="muted tiny">{{ (h.date || '').slice(0, 16) }}</span>
        </li>
      </ul>
    </AppModal>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-4) var(--sp-5); max-width: 1280px; margin: 0 auto; }

.page__bar {
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-4); flex-wrap: wrap;
  padding-bottom: var(--sp-3); margin-bottom: var(--sp-4);
  border-bottom: 1px solid var(--border);
}
.page__back { font-size: var(--fs-xs); color: var(--text-muted); }
.page__back:hover { color: var(--brand-500); }

.edit { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: var(--sp-4); align-items: start; }
.edit__main { display: flex; flex-direction: column; gap: var(--sp-3); min-width: 0; }

.edit__area {
  width: 100%; min-height: calc(100vh - 320px);
  padding: var(--sp-4);
  border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--bg-elevated); color: var(--text);
  font-family: var(--font-sans); font-size: var(--fs-base);
  line-height: var(--lh-relaxed); resize: vertical;
}
.edit__area:focus { outline: none; border-color: var(--brand-500); box-shadow: var(--focus-ring); }

.edit__foot { display: flex; align-items: center; gap: var(--sp-2); }

.score { display: flex; align-items: baseline; gap: var(--sp-2); margin-bottom: var(--sp-3); }
.score__v { font-size: var(--fs-2xl); font-weight: var(--fw-bold); font-variant-numeric: tabular-nums; }
.k-success { color: var(--success); }
.k-info { color: var(--info); }
.k-warning { color: var(--warning); }
.k-danger { color: var(--danger); }
.k-default { color: var(--text-muted); }

.side__h { font-size: var(--fs-xs); color: var(--text-muted); margin: var(--sp-3) 0 var(--sp-2); }

.issues { display: flex; flex-direction: column; gap: var(--sp-2); }
.issues__item {
  padding: var(--sp-2); border-radius: var(--r-sm);
  background: var(--bg); border-left: 2px solid var(--border);
}
.issues__head { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: 4px; }
.issues__rule { font-size: 10px; color: var(--text-subtle); }
.issues__msg { font-size: var(--fs-xs); line-height: var(--lh-normal); }
.issues__advice { font-size: var(--fs-xs); color: var(--brand-600); margin-top: 4px; }
[data-theme="dark"] .issues__advice { color: var(--brand-200); }

.hist { display: flex; flex-direction: column; }
.hist__item {
  display: flex; align-items: center; gap: var(--sp-3);
  padding: var(--sp-3) 0; border-bottom: 1px solid var(--border);
  font-size: var(--fs-sm);
}
.hist__item:last-child { border-bottom: none; }

@media (max-width: 1000px) {
  .edit { grid-template-columns: minmax(0, 1fr); }
}
</style>
