/* ============================================================================
 * Composable · SSE 任务流（长任务标准交互）
 *
 * ★ 方案 §1.5.2 要求：进度百分比 + ETA + 支持后台运行
 * ★ `since` 增量重放：刷新页面后能无缝续上（后端 Task.events_json 留了全量事件）
 *
 * ★ 为什么用 fetch + ReadableStream 而不是 EventSource：
 *   EventSource **不能带 Authorization 头**（只能靠 cookie），
 *   而本项目用 Bearer 令牌鉴权 → 必须自己读流并手工解析 SSE。
 * ========================================================================== */

import { onBeforeUnmount, ref, shallowRef } from 'vue'
import { tokenStore } from '@/api/http'

export interface StreamEvent {
  kind: string
  text?: string
  [k: string]: unknown
}

export interface UseTaskStreamOptions {
  /** 收到事件时回调 */
  onEvent?: (ev: StreamEvent) => void
  /** 流结束时回调（正常或异常） */
  onDone?: (err?: Error) => void
  /** 自动重连次数（断线续传） */
  maxRetries?: number
}

export function useTaskStream(opts: UseTaskStreamOptions = {}) {
  const running = ref(false)
  const error = ref<string>('')
  const lastEventIndex = ref(0)
  const controller = shallowRef<AbortController | null>(null)
  let retries = 0

  /**
   * 消费任务事件流。
   * @param url   SSE 地址（含 `?since=N`）
   */
  async function start(url: string) {
    stop()
    running.value = true
    error.value = ''
    retries = 0
    await _consume(url)
  }

  async function _consume(url: string) {
    const ctrl = new AbortController()
    controller.value = ctrl
    try {
      // ★ 手工带 Authorization（EventSource 做不到）
      const headers: Record<string, string> = { Accept: 'text/event-stream' }
      const tok = tokenStore.get()
      if (tok) headers.Authorization = `Bearer ${tok}`

      const res = await fetch(url, { headers, signal: ctrl.signal })
      if (!res.ok || !res.body) {
        throw new Error(`流连接失败（HTTP ${res.status}）`)
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buf = ''

      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })

        // SSE 以空行分隔事件块
        const chunks = buf.split('\n\n')
        buf = chunks.pop() ?? ''
        for (const chunk of chunks) {
          const ev = _parseSse(chunk)
          if (!ev) continue
          lastEventIndex.value += 1
          opts.onEvent?.(ev)
          if (ev.kind === 'done' || ev.kind === 'error') {
            running.value = false
            if (ev.kind === 'error') error.value = String(ev.text || '生成失败')
            opts.onDone?.(ev.kind === 'error' ? new Error(error.value) : undefined)
            return
          }
        }
      }
      running.value = false
      opts.onDone?.()
    } catch (e) {
      // ★ 主动中止不算错误（组件卸载 / 用户取消）
      if ((e as Error)?.name === 'AbortError') {
        running.value = false
        return
      }
      const err = e as Error
      // ★ 断线重连：带 since 续传（后端支持增量重放）
      const max = opts.maxRetries ?? 2
      if (retries < max) {
        retries += 1
        setTimeout(() => { void _consume(_withSince(url, lastEventIndex.value)) }, 1200)
        return
      }
      running.value = false
      error.value = err.message || '连接中断'
      opts.onDone?.(err)
    }
  }

  /** 把 since 参数替换为最新值 */
  function _withSince(url: string, since: number): string {
    try {
      const u = new URL(url, location.origin)
      u.searchParams.set('since', String(since))
      return u.pathname + '?' + u.searchParams.toString()
    } catch {
      return url
    }
  }

  function _parseSse(chunk: string): StreamEvent | null {
    for (const line of chunk.split('\n')) {
      const t = line.trim()
      if (!t.startsWith('data:')) continue
      const payload = t.slice(5).trim()
      if (!payload || payload === '[DONE]') return null
      try {
        return JSON.parse(payload) as StreamEvent
      } catch {
        // 非 JSON 的 data 行按纯文本处理
        return { kind: 'token', text: payload }
      }
    }
    return null
  }

  function stop() {
    controller.value?.abort()
    controller.value = null
    running.value = false
  }

  // ★ 组件卸载必须中止，否则流不会关（连接泄漏）
  onBeforeUnmount(stop)

  return { running, error, lastEventIndex, start, stop }
}
