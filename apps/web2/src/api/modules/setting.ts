/* ============================================================================
 * API · 设定（人物 / 大纲 / 世界观 / 伏笔 / 质检）
 * 对照源码：apps/api/main.py:2568-2620, 2974-3275
 * ========================================================================== */

import http from '@/api/http'

/* ─────────────────────────── 类型 ─────────────────────────── */

/** 人物（来自 characters.review_queue） */
export interface Character {
  id: string
  name: string
  role: string
  profile: string
  voice: string
  arc: string
  mentions: number
  is_auto: number
  first_chapter: number
  state_now: string
}

/** 世界观设定 */
export interface WorldItem {
  category: string
  key: string
  value: string
}

/** 伏笔（bible 口径） */
export interface Foreshadow {
  content: string
  status: string
  target_vol: string | null
  planted_ch: number
}

/** 卷（大纲） */
export interface VolumeOutline {
  idx: number
  name: string
  outline: string
  start_ch: number
  end_ch: number | null
}

/** 故事圣经（GET /bible） */
export interface Bible {
  meta: Record<string, unknown>
  book: {
    title: string
    genre: string
    platform: string
    chapter_min: number
    chapter_max: number
    target_words: number
    theme: string
    tags: string[]
  }
  world: WorldItem[]
  characters: Character[]
  volumes: VolumeOutline[]
  foreshadows: Foreshadow[]
  chapters: Array<{ idx: number; title: string; words: number
                    gate_score: number | null; status: string }>
  progress: { chapters: number; words: number }
}

/** 故事状态中枢（GET /state） */
export interface BookState {
  volumes: Array<{ idx: number; name: string; start_ch: number
                   end_ch: number | null; outline: string }>
  characters: Array<{ name: string; role: string; state: string; profile: string }>
  foreshadows: Array<{ content: string; planted_ch: number
                       status: string; payoff_ch: number | null }>
  world: WorldItem[]
  progress: { chapters: number; words: number
              last_idx: number; open_foreshadows: number }
}

/* ─────────────────────────── API ─────────────────────────── */

export const settingApi = {
  /** 人物清单（按 mention 数降序） */
  characters(bookId: string) {
    return http.get<{ items: Character[] }>(
      `/api/books/${encodeURIComponent(bookId)}/characters`)
  },

  patchCharacter(bookId: string, cid: string,
                 body: Partial<Pick<Character, 'role' | 'profile' | 'voice'
                   | 'arc' | 'state_now'>>) {
    return http.patch<{ ok: boolean }>(
      `/api/books/${encodeURIComponent(bookId)}/characters/${encodeURIComponent(cid)}`, body)
  },

  /** 从正文重新识别人物 */
  syncCharacters(bookId: string) {
    return http.post<{ ok: boolean; added?: number; updated?: number }>(
      `/api/books/${encodeURIComponent(bookId)}/characters/sync`, {})
  },

  /** ★ #311 删除人物（连带清理衍生表） */
  deleteCharacter(bookId: string, cid: string) {
    return http.del<{ ok: boolean; name?: string }>(
      `/api/books/${encodeURIComponent(bookId)}/characters/${encodeURIComponent(cid)}`)
  },

  /** 故事圣经（大纲/世界观/人物/伏笔一次拉全） */
  bible(bookId: string) {
    return http.get<Bible>(`/api/books/${encodeURIComponent(bookId)}/bible`)
  },

  /** 状态中枢（写作台侧栏用） */
  state(bookId: string) {
    return http.get<BookState>(`/api/books/${encodeURIComponent(bookId)}/state`)
  },

  /** 一致性检查（冲突报告） */
  consistency(bookId: string) {
    return http.get<{
      ok?: boolean
      issues?: Array<{ kind?: string; level?: string; message?: string; advice?: string }>
      [k: string]: unknown
    }>(`/api/books/${encodeURIComponent(bookId)}/consistency`)
  },

  /** 长程记忆概况（含伏笔债务） */
  memory(bookId: string) {
    return http.get<{
      stats?: Record<string, unknown>
      indexed_to?: number
      obligations?: Array<{ kind?: string; content?: string; due_ch?: number
                            overdue?: boolean; advice?: string }>
    }>(`/api/books/${encodeURIComponent(bookId)}/memory`)
  },

  /** 重建记忆索引（升级后跑一次，幂等） */
  reindex(bookId: string) {
    return http.post<{ ok: boolean; chunks: number }>(
      `/api/books/${encodeURIComponent(bookId)}/memory/reindex`, {})
  },

  /** 伏笔列表 */
  foreshadows(bookId: string) {
    return http.get<{ items: Array<{ id: string; content: string; status: string
                                     planted_ch: number; payoff_ch: number | null
                                     due_ch?: number; priority?: string }> }>(
      `/api/books/${encodeURIComponent(bookId)}/foreshadows`)
  },

  createForeshadow(bookId: string, body: { content: string; planted_ch?: number
                                           target_vol?: string }) {
    return http.post<{ ok: boolean }>(
      `/api/books/${encodeURIComponent(bookId)}/foreshadows`, body)
  },

  deleteForeshadow(bookId: string, fid: string) {
    return http.del<{ ok: boolean }>(
      `/api/books/${encodeURIComponent(bookId)}/foreshadows/${encodeURIComponent(fid)}`)
  },

  /** 质检历史 */
  audits(bookId: string, limit = 30) {
    return http.get<{ items: Array<{ id: string; idx: number; score: number
                                     created_at: string; issues?: number }> }>(
      `/api/books/${encodeURIComponent(bookId)}/audits`, { limit })
  },

  /** 运行记录（成本/耗时/质检分） */
  runs(bookId: string, limit = 20) {
    return http.get<Array<{ id: string; kind: string; model: string; cost: number
                            gate_score: number | null; status: string
                            duration_ms: number; time: string }>>(
      `/api/books/${encodeURIComponent(bookId)}/runs`, { limit })
  },
}

export default settingApi
