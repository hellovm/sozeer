<script setup lang="ts">
/**
 * App · 根组件
 *
 * ★ 全局事件桥：http 层通过 bus 广播（避免循环依赖），这里统一转成 toast / 跳转
 */
import { onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { bus, EVT } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import AppToastHost from '@/components/AppToastHost.vue'
import CommandPalette from '@/components/CommandPalette.vue'

const router = useRouter()
const auth = useAuthStore()
const ui = useUiStore()

let offs: Array<() => void> = []


  // ★ 会话失效 → 清态 + 跳登录（只在这里跳，http 层不直接操作路由）
  offs.push(bus.on(EVT.AUTH_EXPIRED, () => {
    auth.clearSession()
    ui.warn('登录已过期，请重新登录')
    const cur = router.currentRoute.value
    if (cur.name !== 'login') {
      router.push({ name: 'login', query: { redirect: cur.fullPath } })
    }
  }))

  offs.push(bus.on(EVT.FORBIDDEN, (e: unknown) => {
    const msg = (e as { message?: string })?.message || '没有权限执行此操作'
    ui.err(msg)
  }))

  offs.push(bus.on(EVT.RATE_LIMITED, (e: unknown) => {
    const err = e as { message?: string; body?: { retry_after?: number } }
    const wait = err?.body?.retry_after
    ui.warn(wait ? `操作过于频繁，请 ${wait} 秒后重试` : (err?.message || '操作过于频繁，请稍后再试'))
  }))

  // 全局错误提示（http 层已过滤 401/403）
  offs.push(bus.on(EVT.ERROR, (e: unknown) => {
    const msg = (e as { message?: string })?.message || '请求失败'
    ui.err(msg)
  }))

  // ★ 全局快捷键：⌘K / Ctrl+K 打开命令面板
  const onKey = (e: KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault()
      ui.commandOpen ? ui.closeCommand() : ui.openCommand()
    }
  }
  window.addEventListener('keydown', onKey)
  offs.push(() => window.removeEventListener('keydown', onKey))
})

onBeforeUnmount(() => {
  offs.forEach(fn => fn())
  offs = []
})
</script>

<template>
  <RouterView />
  <AppToastHost />
  <CommandPalette />
</template>
