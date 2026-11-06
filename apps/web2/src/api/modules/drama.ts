/* ============================================================================
 * API · 短剧工坊（v0.26.1）
 *
 * ★★ 重要契约（对照 apps/api/main.py 逐个核实，不要"合理推测"）：
 *   短剧是**按书挂载**的：`/api/books/{bookId}/drama/projects`
 *   不是顶层的 `/api/drama/*`（顶层只有 retake 三个动作端点）。
 *   所以前端必须知道 bookId —— 短剧列表页要先选书。
 *
 * ★ 列表返回风格（本项目有四种风格，逐个核对过）：
 *   · 项目列表   → `{items: [...]}`
 *   · 详情       → 裸对象
 *   · 资产列表   → `{items: [...]}`
 *   · 音色列表   → `{items: [...]}`
 * ========================================================================== */

import http from '@/api/http'

// ──────────────────────────────────────────── 类型

export interface DramaProject {
  id: string
  title: string
  style: string
  content_mode: string
  generation_mode: string
  aspect_ratio: string
  status: string
  created_at: string
}

export interface DramaAsset {
  id: string
  kind: 'character' | 'scene' | 'prop' | string
  name: string
  description: string
  /** ★ 契约：后端资产列表返回 `image`（非 image_path）+ `image_status`（none|pending|done|error） */
  image?: string
  image_status?: string
  parent_id?: string
  form_name?: string
  voice_id?: string
  voice_ref?: string
}

export interface DramaScene {
  scene_id: string
  scene_description?: string
  characters_in_scene?: string[]
  image_prompt?: Record<string, string>
  image_path?: string
  video_path?: string
  audio_path?: string
  status?: string
  duration_seconds?: number
}

export interface DramaEpisode {
  episode: number
  title?: string
  status?: string
  scenes?: DramaScene[]
}

export interface VoiceCastEntry {
  character: string
  voice_id?: string
  ref_audio?: string
  note?: string
}

// ──────────────────────────────────────────── API

export const dramaApi = {
  /** 项目列表（按书） */
  listProjects(bookId: string) {
    return http.get<{ items: DramaProject[] }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects`)
  },

  createProject(bookId: string, payload: {
    title: string; style?: string; content_mode?: string
    generation_mode?: string; aspect_ratio?: string
  }) {
    return http.post<{ id: string }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects`, payload)
  },

  project(bookId: string, projectId: string) {
    return http.get<DramaProject>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}`)
  },

  updateProject(bookId: string, projectId: string, payload: Partial<DramaProject>) {
    return http.patch<{ ok: boolean }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}`,
      payload)
  },

  deleteProject(bookId: string, projectId: string) {
    return http.del<{ ok: boolean }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}`)
  },

  /** 生成分集（AI 规划） */
  plan(bookId: string, projectId: string, payload: Record<string, unknown> = {}) {
    return http.post<{ ok: boolean; task_id?: string; episodes?: number }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/plan`,
      payload)
  },

  /** 时长预估（导演视角智能分集） */
  durationEstimate(bookId: string, payload: Record<string, unknown> = {}) {
    return http.post<{ estimated_minutes?: number; episodes?: number; detail?: string }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/duration-estimate`, payload)
  },

  // ── 资产 ──
  assets(bookId: string, projectId: string) {
    return http.get<{ items: DramaAsset[] }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/assets`)
  },

  createAsset(bookId: string, projectId: string, payload: Partial<DramaAsset>) {
    return http.post<{ id: string }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/assets`,
      payload)
  },

  /** 从正文/剧本同步角色清单 */
  enrichAssets(bookId: string, projectId: string) {
    return http.post<{ ok: boolean; added?: number }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/assets/enrich`,
      {})
  },

  /** 批量生成定妆图 */
  generateAssetImages(bookId: string, projectId: string, assetIds: string[] = []) {
    return http.post<{ ok: boolean; task_id?: string }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/assets/generate-images`,
      { asset_ids: assetIds })
  },

  /** 生成单个资产图 */
  generateAssetImage(bookId: string, projectId: string, assetId: string) {
    return http.post<{ ok: boolean; path?: string }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/assets/${encodeURIComponent(assetId)}/generate-image`,
      {})
  },

  /** 新增角色形态（如 青年/老年、神兽/黄狗） */
  addAssetForm(bookId: string, projectId: string, assetId: string,
               payload: { form_name: string; description: string }) {
    return http.post<{ id: string }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/assets/${encodeURIComponent(assetId)}/forms`,
      payload)
  },

  // ── 分集 ──
  episode(bookId: string, projectId: string, episode: number) {
    return http.get<DramaEpisode>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/episodes/${episode}`)
  },

  /** 生成剧本 */
  genScript(bookId: string, projectId: string, episode: number) {
    return http.post<{ ok: boolean; task_id?: string }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/episodes/${episode}/script`,
      {})
  },

  /** 生成分镜（八段提示词） */
  genStoryboard(bookId: string, projectId: string, episode: number) {
    return http.post<{ ok: boolean; task_id?: string; enriched?: number }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/episodes/${episode}/storyboard`,
      {})
  },

  confirmEpisode(bookId: string, projectId: string, episode: number) {
    return http.post<{ ok: boolean }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/episodes/${episode}/confirm`,
      {})
  },

  /** 批量生成（target: image | video | audio —— 后端契约为单数，勿用复数） */
  generateAll(bookId: string, projectId: string, episode: number, target: string) {
    return http.post<{ ok: boolean; task_id?: string; count?: number }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/episodes/${episode}/generate-all/${target}`,
      {})
  },

  /** 单镜生成 */
  genScene(bookId: string, projectId: string, sceneId: string, target: string) {
    return http.post<{ ok: boolean; path?: string }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(sceneId)}/generate/${target}`,
      {})
  },

  /** 多宫格分镜图 */
  gridGenerate(bookId: string, projectId: string, episode: number, payload = {}) {
    return http.post<{ ok: boolean; path?: string }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/grid-generate/${episode}`,
      payload)
  },

  // ── 质检 / 导出 ──
  qc(bookId: string, projectId: string, episode: number) {
    return http.get<{ items?: unknown[]; score?: number; issues?: unknown[] }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/episodes/${episode}/qc`)
  },

  exports(bookId: string, projectId: string) {
    return http.get<{ items: Array<{ id: string; name: string; kind: string; created_at: string }> }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/exports`)
  },

  exportJianying(bookId: string, projectId: string, episode: number) {
    return http.post<{ ok: boolean; path?: string }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/export-jianying/${episode}`,
      {})
  },

  /** 成片导出（拼接 + 混音 + 硬字幕） */
  film(bookId: string, projectId: string, episode: number) {
    return http.post<{ ok: boolean; path?: string }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/film/${episode}`,
      {})
  },

  filmCapability(bookId: string, projectId: string) {
    return http.get<{ ffmpeg?: boolean; detail?: string }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/film/capability`)
  },

  // ── 音色（声纹资产）──
  voiceCast(bookId: string, projectId: string) {
    return http.get<{ items: VoiceCastEntry[] }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/voice-cast`)
  },

  saveVoiceCast(bookId: string, projectId: string, items: VoiceCastEntry[]) {
    return http.post<{ ok: boolean }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/voice-cast`,
      { items })
  },

  /** 自动为角色匹配音色 */
  autoVoiceCast(bookId: string, projectId: string) {
    return http.post<{ ok: boolean; items?: VoiceCastEntry[] }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/voice-cast/auto`,
      {})
  },

  audioPlan(bookId: string, projectId: string, sceneId: string) {
    return http.get<{ items?: unknown[]; plan?: unknown }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(sceneId)}/audio-plan`)
  },

  voiceJobs(bookId: string, projectId: string, sceneId: string) {
    return http.get<{ items: unknown[] }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/projects/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(sceneId)}/voice-jobs`)
  },

  // ── 队列 ──
  queue(bookId: string) {
    return http.get<{ items: Array<{ batch_id: string; status: string; total: number; done: number }> }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/queue`)
  },

  cancelBatch(bookId: string, batchId: string) {
    return http.post<{ ok: boolean }>(
      `/api/books/${encodeURIComponent(bookId)}/drama/queue/${encodeURIComponent(batchId)}/cancel`, {})
  },
}

export default dramaApi
