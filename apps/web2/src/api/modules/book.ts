/* ============================================================================
 * API · 作品（books）· 章节 · 对话 · 设定
 *
 * ★★ 类型必须与后端**真实返回**对齐（不是"合理猜测"）：
 *   · `GET /chapters` 返回**裸数组**（不是 `{items}`）
 *   · `GET /state` 返回 volumes/characters/foreshadows/world/progress
 *   · 时间字段是 `YYYY-MM-DD HH:MM:SS` 字符串（无时区后缀）
 *   对照源码：apps/api/main.py:1136-1250
 * ========================================================================== */

import http, { type RequestOptions } from '@/api/http'

/* ─────────────────────────── 类型 ─────────────────────────── */

export interface Book {
  id: string
  title: string
  author?: string
  status?: string
  theme?: string
  tags?: string
  chapters?: number
  words?: number
  quality_score?: number
  created_at?: string
  updated_at?: string
}

/** 书籍详情（含文风档案） */
export interface BookDetail {
  id: string
  title: string
  author: string
  genre: string
  platform: string
  synopsis: string
  target_words: number
  chapter_min: number
  chapter_max: number
  style: StyleProfile
}

export interface StyleProfile {
  pov: string
  tone: string
  rules: string
  sample: string
  max_para_chars: number
  banned_words: string[]
}

/** 章节列表项（★ 后端返回裸数组） */
export interface ChapterBrief {
  id: string
  idx: number
  title: string
  words: number
  status: string
  gate_score: number
  volume: string
}

/** 章节详情 */
export interface Chapter {
  idx: number
  title: string
  content: string
  summary: string
  words: number
  status: string
  gate_score: number
  gate_report: GateReport
}

export interface GateReport {
  score?: number
  issues?: Array<{ rule?: string; level?: string; message?: string; advice?: string }>
  [k: string]: unknown
}

/** 待审草稿（v0.13.18：扫代笔任务，列「比正文新」的待采纳稿） */
export interface PendingDraft {
  task_id: string
  ch_idx: number
  title: string
  gate_score: number | null
  words: number
  /** 正文当前版本质检分（用于判断草稿是否比库里新） */
  chapter_score: number
  /** 草稿质检分是否高于正文当前版本 */
  newer: boolean
  created_at: string
  /** 草稿正文（可能带 `# 第 N 章` 标题行） */
  text: string
  gate_report: Record<string, unknown> | null
}

/** 故事状态中枢 */
export interface BookState {
  volumes: Volume[]
  characters: StateCharacter[]
  foreshadows: StateForeshadow[]
  world: WorldSetting[]
  progress: {
    chapters: number
    words: number
    last_idx: number
    open_foreshadows: number
  }
}

export interface Volume {
  idx: number
  name: string
  start_ch: number
  end_ch: number | null
  outline: string
}

export interface StateCharacter {
  name: string
  role: string
  state: string
  profile: string
}

export interface StateForeshadow {
  content: string
  planted_ch: number
  status: string
  payoff_ch: number | null
}

export interface WorldSetting {
  category: string
  key: string
  value: string
}

export interface Conversation {
  id: string
  title: string
  created_at?: string
  updated_at?: string
}

export interface ChatMessage {
  id?: string
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at?: string
  /** ★ 关联任务 id（用于重建草稿卡） */
  task_id?: string
  /** ★ 未消费的草稿（刷新后仍能显示「采纳 / 重写」卡片） */
  draft?: Record<string, unknown> | null
}

/* ─────────────────────────── 请求体 ─────────────────────────── */

export interface CreateBookPayload {
  title: string
  theme?: string
  tags?: string
  author?: string
  premise?: string
}

export interface GeneratePayload {
  ch_idx?: number | null
  instruction?: string
  /** ★ v0.25.7 J11：优先级（服务端会限幅 ±1000 防插队） */
  priority?: number
  /** ★ 是否接受排队（false = 满了直接失败，不挂起） */
  allow_queue?: boolean
}

/** 生成被排队时的 202 响应 */
export interface QueuedResponse {
  ok: true
  queued: true
  reason?: string
  text?: string
  position?: number
  retry_after?: number
  hint?: string
  limits?: Record<string, unknown>
}

/* ─────────────────────────── API ─────────────────────────── */

export const bookApi = {
  /**
   * ★★ 作品列表：后端返回**裸数组**（对照 main.py:999 `list_books` → `return out`）。
   *   不是 `{items: [...]}` —— 本项目四种列表风格里最容易猜错的一种。
   *   ★ 猜错**不报错**，只表现为"页面空白"（见 MEMORY §80）。
   */
  list() {
    return http.get<Book[]>('/api/books')
  },

  create(payload: CreateBookPayload) {
    return http.post<{ ok: boolean; book: Book }>('/api/books', payload)
  },

  get(bookId: string) {
    return http.get<BookDetail>(`/api/books/${encodeURIComponent(bookId)}`)
  },

  remove(bookId: string) {
    return http.del<{ ok: boolean }>(`/api/books/${encodeURIComponent(bookId)}`)
  },

  /**
   * 书稿导出（v1.0-E4.11：支持 txt / md / epub / docx / pdf / publish 投稿包）。
   * 返回 Blob（需带鉴权头下载），调用方负责触发浏览器保存。
   */
  exportBook(bookId: string, fmt: string) {
    return http.download(
      `/api/books/${encodeURIComponent(bookId)}/export`, { fmt })
  },

  /** 文风档案（部分更新） */
  patchStyle(bookId: string, patch: Partial<StyleProfile>) {
    return http.patch<{ ok: boolean }>(
      `/api/books/${encodeURIComponent(bookId)}/style`, patch)
  },

  /* ── 章节 ── */

  /** ★ 返回裸数组（不是 {items}）—— 与后端对齐，别"想当然"包一层 */
  chapters(bookId: string) {
    return http.get<ChapterBrief[]>(`/api/books/${encodeURIComponent(bookId)}/chapters`)
  },

  /**
   * 取单章详情。★ 可选传 `opts`（如 `{ silent: true }`）：
   *   探测型调用（如编辑器查看待审草稿目标章）期望 404 是正常的，
   *   传 silent 让 http 层不弹全局红 toast，由调用方自行分支处理。
   */
  chapter(bookId: string, idx: number, opts: RequestOptions = {}) {
    return http.get<Chapter>(`/api/books/${encodeURIComponent(bookId)}/chapters/${idx}`, undefined, opts)
  },

  saveChapter(bookId: string, idx: number,
              patch: { title?: string; content?: string; summary?: string }) {
    return http.patch<{ ok: boolean; words: number }>(
      `/api/books/${encodeURIComponent(bookId)}/chapters/${idx}`, patch)
  },

  chapterHistory(bookId: string, idx: number) {
    return http.get<Array<{ commit: string; message?: string; date?: string; author?: string }>>(
      `/api/books/${encodeURIComponent(bookId)}/chapters/${idx}/history`)
  },

  chapterDiff(bookId: string, idx: number, a: string, b: string) {
    return http.get<{ ops?: unknown[] }>(
      `/api/books/${encodeURIComponent(bookId)}/chapters/${idx}/diff`, { a, b })
  },

  /** 重跑质检门禁 */
  recheckGate(bookId: string, idx: number) {
    return http.post<GateReport>(
      `/api/books/${encodeURIComponent(bookId)}/chapters/${idx}/gate`, {})
  },

  /* ── 状态中枢 ── */

  state(bookId: string) {
    return http.get<BookState>(`/api/books/${encodeURIComponent(bookId)}/state`)
  },

  bible(bookId: string) {
    return http.get<Record<string, unknown>>(
      `/api/books/${encodeURIComponent(bookId)}/bible`)
  },

  /* ── 对话 ── */

  /**
   * ★★ 会话列表：返回 `{conversations: [...], active: id}`。
   *   ★ 注意：**不是** `items`、**也不是**裸数组 ——
   *     本项目三种写法都出现过，必须逐个对照 main.py 确认（对照 2673-2693）。
   */
  conversations(bookId: string) {
    return http.get<{ conversations: Conversation[]; active: string | null }>(
      `/api/books/${encodeURIComponent(bookId)}/conversations`)
  },

  /**
   * ★★ 创建会话：后端返回**扁平 `{id}`**（不是 `{ok, conversation}`）。
   *   对照 main.py:2695-2702。这类"想当然包一层"的假设正是页面空白的主因，
   *   有 tests/test_g05_page_migration.py 守着。
   */
  createConversation(bookId: string, title = '新对话') {
    return http.post<{ id: string }>(
      `/api/books/${encodeURIComponent(bookId)}/conversations`, { title })
  },

  deleteConversation(bookId: string, cid: string) {
    return http.del<{ ok: boolean }>(
      `/api/books/${encodeURIComponent(bookId)}/conversations/${encodeURIComponent(cid)}`)
  },

  /**
   * ★★ 消息列表：返回 `{messages: [...]}`（**不是** `items`）。
   *   每条还含 `task_id` / `draft`（未消费草稿），供前端重建草稿卡。
   */
  messages(bookId: string, cid: string) {
    return http.get<{ messages: ChatMessage[] }>(
      `/api/books/${encodeURIComponent(bookId)}/conversations/${encodeURIComponent(cid)}/messages`)
  },

  /** ★ 发消息：立即返回 task_id，正文通过 SSE 流式获取 */
  chat(bookId: string, message: string, conversationId = '', history: ChatMessage[] = []) {
    return http.post<{ task_id: string; conversation_id: string; status: string }>(
      `/api/books/${encodeURIComponent(bookId)}/chat`,
      { message, conversation_id: conversationId,
        history: history.slice(-12).map(m => ({ role: m.role, content: m.content })) })
  },

  /* ── 生成（SSE） ── */

  /** 触发章节生成；★ 满额时返回（status 202, queued=true） */
  generate(bookId: string, payload: GeneratePayload) {
    return http.post<{ task_id?: string } | QueuedResponse>(
      `/api/books/${encodeURIComponent(bookId)}/generate`, payload,
      { timeout: 20_000 })
  },

  /** 任务事件流（SSE；支持 since 增量重放，刷新可续上） */
  taskStreamUrl(bookId: string, taskId: string, since = 0) {
    return `/api/books/${encodeURIComponent(bookId)}/tasks/${encodeURIComponent(taskId)}/stream?since=${since}`
  },

  task(bookId: string, taskId: string) {
    return http.get<{
      id: string; kind: string; status: string; progress?: string
      events?: unknown[]; error?: string; result?: string
    }>(`/api/books/${encodeURIComponent(bookId)}/tasks/${taskId}`)
  },

  tasks(bookId: string) {
    return http.get<{ items: Array<{ id: string; kind: string; status: string }> }>(
      `/api/books/${encodeURIComponent(bookId)}/tasks`)
  },

  /** 采纳草稿进正文（★ 与后端 AcceptIn 对齐：gate / register / task_id） */
  accept(bookId: string, payload: {
    ch_idx: number
    title: string
    content: string
    gate?: Record<string, unknown> | null
    register?: boolean
    /** ★ 来源任务 id；采纳成功后服务端会消费该草稿（防重复入库） */
    task_id?: string | null
  }, opts: RequestOptions = {}) {
    return http.post<{
      ok: boolean
      idx: number
      words: number
      summary?: string
      /** ★ 字数/质检低于阈值时回传的警告（不阻断，前端弹提示） */
      warnings: string[]
      /** 该任务草稿是否被消费 */
      consumed: boolean
    }>(
      `/api/books/${encodeURIComponent(bookId)}/accept`, payload, opts)
  },

  /** 待审草稿（v0.13.18：扫全部代笔任务，列比正文新的稿） */
  pendingDrafts(bookId: string) {
    return http.get<{ items: PendingDraft[] }>(
      `/api/books/${encodeURIComponent(bookId)}/pending-drafts`)
  },

  /** 取回单条草稿（刷新恢复用） */
  draft(bookId: string, taskId: string) {
    return http.get<{
      task_id: string; status: string; ch_idx: number
      text: string; gate_score: number | null; gate_report: unknown
      title: string; words: number
      discarded: boolean; accepted: boolean
    }>(
      `/api/books/${encodeURIComponent(bookId)}/tasks/${encodeURIComponent(taskId)}/draft`)
  },

  /** 丢弃草稿（清正文打 discarded 标记，任务历史保留，可恢复） */
  discardDraft(bookId: string, taskId: string) {
    return http.del<{ ok: boolean; already?: boolean; ch_idx?: number }>(
      `/api/books/${encodeURIComponent(bookId)}/tasks/${encodeURIComponent(taskId)}/draft`)
  },

  /** 误删反悔：恢复被丢弃的草稿正文 */
  restoreDraft(bookId: string, taskId: string) {
    return http.post<{ ok: boolean; ch_idx: number }>(
      `/api/books/${encodeURIComponent(bookId)}/tasks/${encodeURIComponent(taskId)}/draft/restore`)
  },
}

export default bookApi
