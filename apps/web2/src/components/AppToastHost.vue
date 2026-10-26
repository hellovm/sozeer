<script setup lang="ts">
/**
 * ToastHost · 全局提示宿主（挂在 App.vue 根部，单例）
 * 消费 ui store 的 toasts
 */
import { useUiStore } from '@/stores/ui'

const ui = useUiStore()

const ICONS: Record<string, string> = {
  ok: '✓', err: '!', warn: '△', info: 'i',
}
</script>

<template>
  <Teleport to="body">
    <div class="toasts" role="status" aria-live="polite">
      <TransitionGroup name="toast">
        <div
          v-for="t in ui.toasts"
          :key="t.id"
          class="toast"
          :class="`toast--${t.kind}`"
        >
          <span class="toast__icon" aria-hidden="true">{{ ICONS[t.kind] }}</span>
          <span class="toast__msg">{{ t.message }}</span>
          <button class="toast__close" type="button" aria-label="关闭" @click="ui.dismiss(t.id)">×</button>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.toasts {
  position: fixed;
  top: var(--sp-5); left: 50%;
  transform: translateX(-50%);
  z-index: var(--z-toast);
  display: flex; flex-direction: column; gap: var(--sp-2);
  pointer-events: none;
  max-width: min(92vw, 480px);
}
.toast {
  display: flex; align-items: flex-start; gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-3) var(--sp-3) var(--sp-4);
  border-radius: var(--r-md);
  border: 1px solid var(--border);
  background: var(--bg-elevated);
  box-shadow: var(--sh-lg);
  font-size: var(--fs-sm);
  pointer-events: auto;
}
.toast__icon {
  flex-shrink: 0;
  width: 18px; height: 18px; margin-top: 1px;
  display: grid; place-items: center;
  border-radius: 50%; font-size: 12px; font-weight: 700;
  color: #fff;
}
.toast--ok .toast__icon   { background: var(--success); }
.toast--err .toast__icon  { background: var(--danger); }
.toast--warn .toast__icon { background: var(--warning); }
.toast--info .toast__icon { background: var(--info); }

.toast--err { border-color: var(--danger); }
.toast--ok { border-color: var(--success); }

.toast__msg { flex: 1 1 auto; line-height: var(--lh-normal); word-break: break-word; }
.toast__close {
  flex-shrink: 0; width: 18px; height: 18px;
  border: none; background: transparent; color: var(--text-subtle);
  font-size: 16px; line-height: 1; border-radius: var(--r-sm);
}
.toast__close:hover { background: var(--bg-hover); color: var(--text); }

.toast-enter-active, .toast-leave-active {
  transition: opacity var(--dur-base) var(--ease),
              transform var(--dur-base) var(--ease);
}
.toast-enter-from, .toast-leave-to {
  opacity: 0; transform: translateY(-10px) scale(.97);
}
</style>
