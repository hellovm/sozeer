/* ============================================================================
 * API · 队列（排队可见性）
 * ========================================================================== */

import http from '@/api/http'

export interface QueuePosition {
  queued: boolean
  position?: number
  ahead?: number
  eta_low_sec?: number
  eta_high_sec?: number
  hint?: string
  lane?: string
  /** ★ 后端在满额时返回 202 + retry_after，而不是 4xx */
  retry_after?: number
}

export interface MyTask {
  id: string
  book_id: string
  kind: string
  status: string
  lane: string
  priority: number
  progress?: number
  created_at: string
  started_at?: string
}

export const queueApi = {
  /** 队列健康（公开只读） */
  health() {
    return http.get<{
      ok: boolean
      backend: string
      queued: number
      running: number
      lanes?: Record<string, number>
    }>('/api/queue/health', undefined, { skipAuth: true, silent: true })
  },

  /** 我的排队任务 */
  mine(limit = 20) {
    return http.get<{ items: MyTask[] }>('/api/queue/mine', { limit })
  },

  /** 某任务的排队位置 */
  position(taskId: string) {
    return http.get<QueuePosition>('/api/queue/position',
      { task_id: taskId }, { silent: true })
  },
}

export default queueApi
