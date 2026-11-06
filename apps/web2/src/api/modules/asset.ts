/* ============================================================================
 * API · 素材库（题材包）（v0.26.1）
 *
 * ★ 契约（对照 apps/api/routers/genres.py）：
 *   · `/api/genres`      → **裸数组**（不是 {items}！）
 *   · `/api/genres/market` → `{items: [...]}`
 *   · `/api/genres/public` → **裸数组**
 * ========================================================================== */

import http from '@/api/http'

export interface GenrePack {
  key: string
  name: string
  desc?: string
  icon?: string
  builtin?: boolean
  source?: string
  version?: string
  metrics?: Record<string, string>
  visual_anchor?: string
  scan_mode?: string
}

export interface MarketPack {
  key: string
  name: string
  author?: string
  desc?: string
  version?: string
  rating?: number
  installs?: number
  installed?: boolean
  updated_at?: string
}

export const assetApi = {
  /**
   * ★★ 题材包列表：返回 `{items: [...], builtin_count}`。
   *   ★ 我一开始按"裸数组"实现 → 页面空白（见 MEMORY §80）。
   *   本项目列表风格有四种，**必须逐个对照源码**，不能靠经验推测。
   */
  genres() {
    return http.get<{ items: GenrePack[]; builtin_count: number }>('/api/genres')
  },

  /** 公开题材（仅启用项，立项下拉用） */
  publicGenres() {
    return http.get<{ items: GenrePack[] }>('/api/genres/public')
  },

  createGenre(payload: Partial<GenrePack>) {
    return http.post<{ key: string }>('/api/genres', payload)
  },

  updateGenre(key: string, payload: Partial<GenrePack>) {
    return http.patch<{ ok: boolean }>(`/api/genres/${encodeURIComponent(key)}`, payload)
  },

  deleteGenre(key: string) {
    return http.del<{ ok: boolean }>(`/api/genres/${encodeURIComponent(key)}`)
  },

  reloadGenres() {
    return http.post<{ ok: boolean; count?: number }>('/api/genres/reload', {})
  },

  /** 导入/导出（JSON 互转） */
  exportGenres() {
    return http.get<{ packs?: GenrePack[] } | GenrePack[]>('/api/genres/export')
  },

  importGenres(packs: unknown) {
    return http.post<{ ok: boolean; imported?: number }>('/api/genres/import', { packs })
  },

  // ── 题材市场 ──
  market() {
    return http.get<{ items: MarketPack[] }>('/api/genres/market')
  },

  publishToMarket(payload: { key: string; desc?: string; version?: string }) {
    return http.post<{ ok: boolean }>('/api/genres/market/publish', payload)
  },

  installPack(packKey: string) {
    return http.post<{ ok: boolean }>(
      `/api/genres/market/${encodeURIComponent(packKey)}/install`, {})
  },

  uninstallPack(packKey: string) {
    return http.post<{ ok: boolean }>(
      `/api/genres/market/${encodeURIComponent(packKey)}/uninstall`, {})
  },

  ratePack(packKey: string, rating: number) {
    return http.post<{ ok: boolean }>(
      `/api/genres/market/${encodeURIComponent(packKey)}/rate`, { rating })
  },

  inspectPack(payload: Record<string, unknown>) {
    return http.post<{ ok: boolean; detail?: unknown }>('/api/genres/market/inspect', payload)
  },
}

export default assetApi
