/* ============================================================================
 * Store · 全局 UI 状态（主题 / Toast / 侧栏折叠 / 命令面板）
 * ========================================================================== */

import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

export type ToastKind = 'ok' | 'err' | 'warn' | 'info'

export interface ToastItem {
  id: number
  kind: ToastKind
  message: string
  /** 毫秒；0 = 不自动消失 */
  duration: number
}

const THEME_KEY = 'sozeer.theme'
export type ThemeMode = 'light' | 'dark'

export const useUiStore = defineStore('ui', () => {
  /* ── 主题 ── */
  const theme = ref<ThemeMode>(readTheme())

  function readTheme(): ThemeMode {
    try {
      const t = localStorage.getItem(THEME_KEY)
      if (t === 'dark' || t === 'light') return t
      return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
    } catch {
      return 'light'
    }
  }

  function applyTheme(t: ThemeMode) {
    document.documentElement.setAttribute('data-theme', t)
    try { localStorage.setItem(THEME_KEY, t) } catch { /* ignore */ }
  }

  function toggleTheme() {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
  }

  watch(theme, applyTheme, { immediate: true })

  /* ── 侧边栏 ── */
  const sidenavCollapsed = ref(readBool('sozeer.sidenav.collapsed', false))
  watch(sidenavCollapsed, v => writeBool('sozeer.sidenav.collapsed', v))
  function toggleSidenav() { sidenavCollapsed.value = !sidenavCollapsed.value }

  /* ── 命令面板 ── */
  const commandOpen = ref(false)
  function openCommand() { commandOpen.value = true }
  function closeCommand() { commandOpen.value = false }

  /* ── Toast ── */
  const toasts = ref<ToastItem[]>([])
  let seq = 0

  function toast(message: string, kind: ToastKind = 'info', duration = 3000) {
    const id = ++seq
    toasts.value.push({ id, kind, message, duration })
    if (duration > 0) {
      setTimeout(() => dismiss(id), duration)
    }
    return id
  }

  function dismiss(id: number) {
    toasts.value = toasts.value.filter(t => t.id !== id)
  }

  const ok = (m: string, d?: number) => toast(m, 'ok', d)
  const err = (m: string, d = 4500) => toast(m, 'err', d)
  const warn = (m: string, d = 4000) => toast(m, 'warn', d)
  const info = (m: string, d?: number) => toast(m, 'info', d)

  /* ── 全局 Banner（额度不足等常驻提示） ── */
  const banners = ref<Array<{ id: number; kind: ToastKind; text: string; action?: { label: string; to: string } }>>([])
  let bseq = 0
  function banner(kind: ToastKind, text: string, action?: { label: string; to: string }) {
    const id = ++bseq
    banners.value.push({ id, kind, text, action })
    return id
  }
  function dismissBanner(id: number) {
    banners.value = banners.value.filter(b => b.id !== id)
  }

  const isDark = computed(() => theme.value === 'dark')

  return {
    theme, isDark, toggleTheme,
    sidenavCollapsed, toggleSidenav,
    commandOpen, openCommand, closeCommand,
    toasts, toast, dismiss, ok, err, warn, info,
    banners, banner, dismissBanner,
  }
})

/* ── localStorage 小工具（隐私模式下会抛，必须包） ── */
function readBool(key: string, fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(key)
    return v === null ? fallback : v === '1'
  } catch { return fallback }
}
function writeBool(key: string, v: boolean): void {
  try { localStorage.setItem(key, v ? '1' : '0') } catch { /* ignore */ }
}
