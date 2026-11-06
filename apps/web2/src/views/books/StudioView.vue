<script setup lang="ts">
/**
 * StudioView · 写作台（G0-5 最核心页面）
 *
 * 三栏布局：左（章节树）+ 中（对话流）+ 右（设定侧栏）
 *
 * ★ 铁律 R-C「一页一验收」—— 与旧版逐项对比：
 *   [x] 章节树（卷分组 / 字数 / 状态 / 当前章高亮）
 *   [x] 对话流（历史消息 / 流式输出 / 停止生成）
 *   [x] 设定侧栏（人物 / 世界观 / 伏笔 / 进度）
 *   [x] 长任务后台运行（SSE + since 增量重放，刷新可续）
 * ★ 相比旧版新增：三栏可折叠、⌘+Enter 发送、上滑暂停自动滚动、
 *   流式打字机、断线自动重连（带 since 续传）
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import bookApi, {
  type BookDetail, type BookState, type ChapterBrief, type ChatMessage,
  type PendingDraft,
} from '@/api/modules/book'
import { useUiStore } from '@/stores/ui'
import { ApiError } from '@/types/api'
import { useTaskStream, type StreamEvent } from '@/composables/useTaskStream'
import AppButton from '@/components/AppButton.vue'
import AppTag from '@/components/AppTag.vue'
import ChapterTree from '@/components/ChapterTree.vue'
import ChatPanel from '@/components/ChatPanel.vue'
import SettingPanel from '@/components/SettingPanel.vue'

const route = useRoute()
const router = useRouter()
const ui = useUiStore()

const bookId = computed(() => String(route.params.bookId || ''))

const book = ref<BookDetail | null>(null)
const chapters = ref<ChapterBrief[]>([])
const state = ref<BookState | null>(null)
const messages = ref<ChatMessage[]>([])

const loading = ref(true)
const chaptersLoading = ref(true)
const stateLoading = ref(true)
const chatLoading = ref(false)

const convId = ref('')
const activeIdx = ref(0)

/** 流式增量文本 */
const streamText = ref('')
/** 当前生成任务 */
const taskId = ref('')

/** ★ 待审草稿（v0.14.10：后台常驻，聊天流找不到草稿卡时的入口） */
const pendingDrafts = ref<PendingDraft[]>([])
const draftsLoading = ref(false)
/** 正在执行采纳/丢弃的草稿 task_id（同一时间只允许一个变更操作） */
const draftBusyId = ref('')
/** 待审抽屉是否展开（有草稿时默认展开） */
const draftsOpen = ref(true)

const leftOpen = ref(true)
const rightOpen = ref(true)

const stream = useTaskStream({
  onEvent: onStreamEvent,
  onDone: () => { void refreshAfterTask() },
})

const generating = computed(() => stream.running.value)

/* ─────────────────────────── 加载 ─────────────────────────── */

onMounted(async () => {
  if (!bookId.value) {
    router.replace({ name: 'books' })
    return
  }
  await loadAll()
})

async function loadAll() {
  loading.value = true
  // ★ 并行加载；单块失败不影响其它（fail-soft）
  await Promise.allSettled([loadBook(), loadChapters(), loadState(), loadChat(), loadPendingDrafts()])
  loading.value = false
}

async function loadBook() {
  try {
    book.value = await bookApi.get(bookId.value)
  } catch (e) {
    if (e instanceof ApiError && e.isNotFound) {
      ui.err('作品不存在或无权访问')
      router.replace({ name: 'books' })
    }
  }
}

async function loadChapters() {
  chaptersLoading.value = true
  try {
    chapters.value = await bookApi.chapters(bookId.value)
    if (!activeIdx.value && chapters.value.length) {
      activeIdx.value = chapters.value[chapters.value.length - 1].idx
    }
  } catch { /* 静默：由 UI 空态兜底 */ } finally {
    chaptersLoading.value = false
  }
}

async function loadState() {
  stateLoading.value = true
  try {
    state.value = await bookApi.state(bookId.value)
  } catch { /* ignore */ } finally {
    stateLoading.value = false
  }
}

async function loadChat() {
  chatLoading.value = true
  try {
    const r = await bookApi.conversations(bookId.value)
    const list = r.conversations || []
    // ★ 优先用服务端记的 active 指针（用户上次停留的会话）
    if (list.length) {
      convId.value = r.active || list[0].id
      await loadMessages()
    } else {
      const c = await bookApi.createConversation(bookId.value)
      convId.value = c.id || ''
    }
  } catch { /* ignore */ } finally {
    chatLoading.value = false
  }
}

async function loadMessages() {
  if (!convId.value) return
  try {
    const r = await bookApi.messages(bookId.value, convId.value)
    messages.value = r.messages || []
  } catch { /* ignore */ }
}

/* ─────────────────────────── 待审草稿 ─────────────────────────── */

/** 拉取待审草稿（后台常驻；失败静默，不阻断主流程） */
async function loadPendingDrafts() {
  if (!bookId.value) return
  draftsLoading.value = true
  try {
    const r = await bookApi.pendingDrafts(bookId.value)
    pendingDrafts.value = r.items || []
  } catch { /* 静默：待审区加载失败不影响正文展示 */ } finally {
    draftsLoading.value = false
  }
}

/** 去掉开头的 `# 第 N 章` 标题行（与后端 accept_draft 的 strip_title 口径对齐） */
function bodyOfDraft(d: PendingDraft): string {
  const t = String(d.text || '')
  const l = t.split('\n')
  return (l[0] && /^#\s*第\s*\d+\s*章/.test(l[0]))
    ? l.slice(1).join('\n').trim()
    : t.trim()
}

/** 标题兜底（后端已给 title，没有就从正文解析） */
function titleOfDraft(d: PendingDraft): string {
  if (d.title) return d.title
  const m = String(d.text || '').match(/^#\s*第\s*\d+\s*章\s*(.*)$/m)
  return m ? m[1].trim() : ''
}

/** 采纳入库：POST /accept（带 task_id，服务端消费该草稿防重复入库） */
async function onAcceptDraft(d: PendingDraft) {
  const score = d.gate_score == null ? '' : String(d.gate_score)
  if (!window.confirm(
    `确认采纳第 ${d.ch_idx} 章草稿${score ? `（质检 ${score} 分）` : ''}入库？\n将替换库内当前版本。`)) return
  draftBusyId.value = d.task_id
  // ★ 采纳是同步链路：落库 + Git 提交 + 登记（调 LLM）+ 记忆索引，
  //   耗时常超默认 30s 超时 → 调到 120s；silent 关掉 http 层全局红
  //   toast（超时时全局+本地会叠两条，由本地 catch 统一提示）。
  ui.info('正在采纳入库并登记，请稍候…', 5000)
  try {
    const r = await bookApi.accept(bookId.value, {
      ch_idx: d.ch_idx,
      title: titleOfDraft(d),
      content: bodyOfDraft(d),
      gate: (d.gate_report as Record<string, unknown>) || null,
      register: true,
      task_id: d.task_id,
    }, { timeout: 120_000, silent: true })
    if (r.warnings?.length) {
      ui.warn(`已采纳，但有质量问题：${r.warnings.join('；')}`, 6000)
    } else {
      ui.ok(`已采纳第 ${d.ch_idx} 章`)
    }
    // 该稿已消费，重拉待审区 + 正文（字数/状态刷新）
    await Promise.allSettled([loadPendingDrafts(), loadChapters(), loadState()])
  } catch (e) {
    if (e instanceof ApiError) ui.err('采纳失败：' + e.message.slice(0, 120))
  } finally {
    draftBusyId.value = ''
  }
}

/** 在编辑器修改：预填草稿正文（写本地 sozeer.draft.{book}.{idx}），跳到章节编辑器 */
function onEditDraft(d: PendingDraft) {
  try {
    localStorage.setItem(
      `sozeer.draft.${bookId.value}.${d.ch_idx}`,
      JSON.stringify({ title: titleOfDraft(d), content: bodyOfDraft(d), at: Date.now() }))
  } catch { /* 隐私模式可能不可写 */ }
  router.push({ name: 'chapter-edit', params: { bookId: bookId.value, idx: d.ch_idx } })
}

/** 丢弃：DELETE /tasks/{id}/draft（清正文打 discarded 标记，任务历史保留，可恢复） */
async function onDiscardDraft(d: PendingDraft) {
  const score = d.gate_score == null ? '' : String(d.gate_score)
  if (!window.confirm(
    `确认丢弃第 ${d.ch_idx} 章这版草稿${score ? `（质检 ${score} 分）` : ''}？\n`
    + '仅从待审区移除，任务历史保留；点错还能恢复。')) return
  draftBusyId.value = d.task_id
  try {
    await bookApi.discardDraft(bookId.value, d.task_id)
    ui.ok('已丢弃（任务历史保留，误删可恢复）')
    await loadPendingDrafts()
  } catch (e) {
    if (e instanceof ApiError) ui.err('丢弃失败：' + e.message.slice(0, 120))
  } finally {
    draftBusyId.value = ''
  }
}

/* ─────────────────────────── 对话 ─────────────────────────── */

async function onSend(text: string) {
  // ★ 先本地乐观追加（不等服务端），输入体验更顺
  messages.value = [...messages.value, { role: 'user', content: text }]
  streamText.value = ''

  try {
    const r = await bookApi.chat(bookId.value, text, convId.value, messages.value)
    convId.value = r.conversation_id || convId.value
    taskId.value = r.task_id
    // ★ 消费事件流（刷新后续上）
    await stream.start(bookApi.taskStreamUrl(bookId.value, r.task_id, 0))
  } catch (e) {
    if (e instanceof ApiError) ui.err(e.message)
    streamText.value = ''
  }
}

function onStreamEvent(ev: StreamEvent) {
  const kind = String(ev.kind || '')
  if (kind === 'token' || kind === 'delta' || kind === 'text') {
    streamText.value += String(ev.text || '')
  } else if (kind === 'error') {
    ui.err(String(ev.text || '生成失败'))
  } else if (kind === 'chapter_draft' || (kind === 'task' && ev.status === 'done')) {
    // ★ 草稿生成完成（或代笔任务 done）：待审区即时亮起，不必等流结束
    void loadPendingDrafts()
  }
}

/** 任务结束后：重新拉消息与状态（正文已落库）+ 待审草稿 */
async function refreshAfterTask() {
  streamText.value = ''
  taskId.value = ''
  await Promise.allSettled([loadMessages(), loadChapters(), loadState(), loadPendingDrafts()])
}

async function onNewConversation() {
  try {
    const c = await bookApi.createConversation(bookId.value)
    convId.value = c.id || ''
    messages.value = []
    streamText.value = ''
    ui.ok('已开启新对话')
  } catch (e) {
    if (e instanceof ApiError) ui.err(e.message)
  }
}

function onStop() {
  stream.stop()
  ui.info('已停止生成')
}

/* ─────────────────────────── 章节 ─────────────────────────── */

function onSelectChapter(idx: number) {
  activeIdx.value = idx
  router.push({ name: 'chapter-edit', params: { bookId: bookId.value, idx } })
}

async function onCreateChapter() {
  const next = chapters.value.length
    ? Math.max(...chapters.value.map(c => c.idx)) + 1
    : 1
  // ★ 不直接建空章；引导用户用对话生成（避免产生大量空章）
  ui.info(`第 ${next} 章可以直接让主编起草`)
  await onSend(`帮我起草第 ${next} 章`)
}

watch(bookId, () => { if (bookId.value) void loadAll() })

/** ★ 切换待审抽屉：若左栏被收起则一并展开（草稿已迁入左栏底部） */
function toggleDrafts() {
  draftsOpen.value = !draftsOpen.value
  if (!leftOpen.value) leftOpen.value = true
}

/** ★ 有草稿进来时自动展开抽屉（从「说了在哪」变成「点得到」） */
watch(pendingDrafts, (list) => {
  if (list.length) draftsOpen.value = true
})
</script>

<template>
  <div class="studio">
    <!-- 顶栏 -->
    <header class="studio__bar">
      <div class="studio__left">
        <button
          class="studio__toggle"
          type="button"
          :aria-label="leftOpen ? '收起章节树' : '展开章节树'"
          :aria-expanded="leftOpen"
          @click="leftOpen = !leftOpen"
        >☰</button>
        <RouterLink class="studio__back" :to="{ name: 'books' }">← 作品</RouterLink>
        <h1 class="studio__title truncate">{{ book?.title || '加载中…' }}</h1>
        <AppTag v-if="book?.genre" size="sm">{{ book.genre }}</AppTag>
        <AppTag v-if="book?.platform" size="sm" kind="info">{{ book.platform }}</AppTag>
      </div>
      <div class="studio__right">
        <span class="muted tiny">
          {{ chapters.length }} 章 · {{ (state?.progress.words || 0).toLocaleString('zh-CN') }} 字
        </span>
        <AppButton
          size="sm"
          variant="ghost"
          class="studio__draftsbtn"
          :class="{ 'is-on': pendingDrafts.length }"
          @click="toggleDrafts"
        >
          待审<template v-if="pendingDrafts.length"> · {{ pendingDrafts.length }}</template>
        </AppButton>
        <AppButton size="sm" variant="ghost" @click="router.push({ name: 'outline', params: { bookId } })">大纲</AppButton>
        <AppButton size="sm" variant="ghost" @click="router.push({ name: 'characters', params: { bookId } })">人物</AppButton>
        <AppButton size="sm" variant="ghost" @click="router.push({ name: 'quality', params: { bookId } })">质检</AppButton>
        <button
          class="studio__toggle"
          type="button"
          :aria-label="rightOpen ? '收起设定栏' : '展开设定栏'"
          :aria-expanded="rightOpen"
          @click="rightOpen = !rightOpen"
        >⚙</button>
      </div>
    </header>

    <!-- 三栏 -->
    <div class="studio__grid" :class="{ 'no-left': !leftOpen, 'no-right': !rightOpen }">
      <ChapterTree
        v-show="leftOpen"
        :chapters="chapters"
        :volumes="state?.volumes || []"
        :active-idx="activeIdx"
        :loading="chaptersLoading"
        :drafts="pendingDrafts"
        :drafts-loading="draftsLoading"
        :draft-busy-id="draftBusyId"
        :drafts-open="draftsOpen"
        @select="onSelectChapter"
        @create="onCreateChapter"
        @accept="onAcceptDraft"
        @edit="onEditDraft"
        @discard="onDiscardDraft"
        @update:drafts-open="draftsOpen = $event"
      />

      <ChatPanel
        :messages="messages"
        :streaming="generating"
        :loading="chatLoading"
        :stream-text="streamText"
        :disabled="!convId"
        @send="onSend"
        @stop="onStop"
        @new-conversation="onNewConversation"
      />

      <SettingPanel v-show="rightOpen" :state="state" :loading="stateLoading" />
    </div>
  </div>
</template>

<style scoped>
.studio {
  display: flex; flex-direction: column;
  height: calc(100vh - var(--topnav-h));
  min-height: 0;
}

.studio__bar {
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-2) var(--sp-4);
  border-bottom: 1px solid var(--border);
  background: var(--bg-elevated);
  flex-shrink: 0;
}
.studio__left, .studio__right { display: flex; align-items: center; gap: var(--sp-3); }
.studio__left { min-width: 0; }
.studio__title { font-size: var(--fs-sm); max-width: 220px; }
.studio__back { font-size: var(--fs-xs); color: var(--text-muted); }
.studio__back:hover { color: var(--brand-500); }
.studio__toggle {
  width: 26px; height: 26px; flex-shrink: 0;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: transparent; color: var(--text-muted); font-size: 12px; line-height: 1;
}
.studio__toggle:hover { background: var(--bg-hover); color: var(--text); }
.studio__toggle[aria-expanded="true"] { background: var(--brand-50); border-color: var(--brand-200); color: var(--brand-600); }

/* ★ 待审入口：有草稿时高亮（提示"这里等你审"） */
.studio__draftsbtn.is-on .btn__label { color: var(--brand-600); font-weight: var(--fw-bold); }
[data-theme="dark"] .studio__draftsbtn.is-on .btn__label { color: var(--brand-200); }

/* 三栏网格 */
.studio__grid {
  flex: 1 1 auto; min-height: 0;
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr) 300px;
}
.studio__grid.no-left { grid-template-columns: minmax(0, 1fr) 300px; }
.studio__grid.no-right { grid-template-columns: 240px minmax(0, 1fr); }
.studio__grid.no-left.no-right { grid-template-columns: minmax(0, 1fr); }

/* 响应式：窄屏收起双侧 */
@media (max-width: 1100px) {
  .studio__grid { grid-template-columns: 200px minmax(0, 1fr); }
  .studio__grid :deep(.panel) { display: none; }
  .studio__grid.no-left { grid-template-columns: minmax(0, 1fr); }
}
</style>
