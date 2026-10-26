<script setup lang="ts">
/**
 * Modal · 模态弹窗
 *
 * ★ 无障碍要点（WCAG）：
 *   · role="dialog" + aria-modal="true"
 *   · 打开时焦点移入，关闭时归还
 *   · Esc 关闭、点击遮罩关闭（可禁用）
 *   · **焦点陷阱**：Tab 在弹窗内循环，不会跑到背后页面
 */
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import AppButton from './AppButton.vue'

const props = withDefaults(defineProps<{
  modelValue: boolean
  title?: string
  width?: string
  closable?: boolean
  maskClosable?: boolean
  /** 危险操作：确认按钮变红 */
  danger?: boolean
  okText?: string
  cancelText?: string
  loading?: boolean
  /**
   * ★ 禁用确定按钮（v1.0-E4.2）。
   *
   * 场景：**危险操作要求用户完成额外确认**才允许提交
   * （如删除作品必须手打书名）。★ 比"点了没反应"好得多 ——
   * 按钮明显置灰 + 上方有说明，用户立刻知道差什么。
   */
  okDisabled?: boolean
  hideFooter?: boolean
}>(), {
  title: '',
  width: '480px',
  closable: true,
  maskClosable: true,
  danger: false,
  okText: '确定',
  cancelText: '取消',
  loading: false,
  okDisabled: false,
  hideFooter: false,
})

const emit = defineEmits<{
  'update:modelValue': [boolean]
  ok: []
  cancel: []
}>()

const cardRef = ref<HTMLElement | null>(null)
let lastActive: HTMLElement | null = null

/**
 * ★ #312 修复：打开后极短时间内忽略「点遮罩关闭」。
 * 否则「打开弹窗的那一下点击」会穿透到全屏遮罩，同一次手势又把弹窗关掉，
 * 表现为「版本历史」等弹窗一闪而逝。
 */
let openGuardTimer: ReturnType<typeof setTimeout> | null = null
const justOpened = ref(false)

function close() {
  emit('update:modelValue', false)
  emit('cancel')
}

function onMaskClick() {
  if (props.maskClosable && !justOpened.value) close()
}

/** ★ 焦点陷阱：拦截 Tab，使其在弹窗内循环 */
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && props.closable) {
    e.preventDefault()
    close()
    return
  }
  if (e.key !== 'Tab' || !cardRef.value) return

  const focusables = cardRef.value.querySelectorAll<HTMLElement>(
    'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
  )
  if (focusables.length === 0) return

  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  const active = document.activeElement as HTMLElement

  if (e.shiftKey && active === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && active === last) {
    e.preventDefault()
    first.focus()
  }
}

watch(() => props.modelValue, async (open) => {
  if (open) {
    lastActive = document.activeElement as HTMLElement
    // ★ 锁定滚动（避免背景滚动）
    document.body.style.overflow = 'hidden'
    await nextTick()
    const firstInput = cardRef.value?.querySelector<HTMLElement>(
      'input, textarea, button:not([disabled])')
    firstInput?.focus()
    // ★ #312：打开瞬间屏蔽遮罩误关（见 onMaskClick 注释）
    justOpened.value = true
    if (openGuardTimer) clearTimeout(openGuardTimer)
    openGuardTimer = setTimeout(() => { justOpened.value = false }, 250)
  } else {
    document.body.style.overflow = ''
    // ★ 焦点归还（无障碍要求）
    lastActive?.focus?.()
  }
})

onBeforeUnmount(() => {
  document.body.style.overflow = ''
  if (openGuardTimer) clearTimeout(openGuardTimer)
})
</script>

<template>
  <Teleport to="body">
    <Transition name="modal">
      <div
        v-if="modelValue"
        class="modal"
        role="dialog"
        aria-modal="true"
        :aria-label="title || '对话框'"
        @keydown="onKeydown"
      >
        <div class="modal__mask" @click="onMaskClick" />
        <div ref="cardRef" class="modal__card" :style="{ width }">
          <header v-if="title || closable" class="modal__head">
            <h3 class="modal__title">{{ title }}</h3>
            <button
              v-if="closable"
              class="modal__close"
              type="button"
              aria-label="关闭"
              @click="close"
            >×</button>
          </header>

          <div class="modal__body">
            <slot />
          </div>

          <footer v-if="!hideFooter" class="modal__foot">
            <slot name="footer">
              <AppButton variant="ghost" @click="close">{{ cancelText }}</AppButton>
              <AppButton
                :variant="danger ? 'danger' : 'primary'"
                :loading="loading"
                :disabled="okDisabled"
                @click="emit('ok')"
              >{{ okText }}</AppButton>
            </slot>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.modal {
  position: fixed; inset: 0;
  z-index: var(--z-modal);
  display: grid; place-items: center;
  padding: var(--sp-4);
}
.modal__mask {
  position: absolute; inset: 0;
  background: rgba(10, 14, 20, .45);
  backdrop-filter: blur(2px);
}
.modal__card {
  position: relative;
  max-width: 100%;
  max-height: calc(100vh - var(--sp-8));
  display: flex; flex-direction: column;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  box-shadow: var(--sh-lg);
  overflow: hidden;
}
.modal__head {
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-4) var(--sp-5);
  border-bottom: 1px solid var(--border);
}
.modal__title { font-size: var(--fs-base); }
.modal__close {
  width: 28px; height: 28px; flex-shrink: 0;
  border: none; border-radius: var(--r-sm);
  background: transparent; color: var(--text-muted);
  font-size: 20px; line-height: 1;
}
.modal__close:hover { background: var(--bg-hover); color: var(--text); }

.modal__body { padding: var(--sp-5); overflow-y: auto; flex: 1 1 auto; }

.modal__foot {
  display: flex; justify-content: flex-end; gap: var(--sp-3);
  padding: var(--sp-4) var(--sp-5);
  border-top: 1px solid var(--border);
  background: var(--bg-sunken);
}

/* 过渡 */
.modal-enter-active, .modal-leave-active { transition: opacity var(--dur-base) var(--ease); }
.modal-enter-from, .modal-leave-to { opacity: 0; }
.modal-enter-active .modal__card { transition: transform var(--dur-base) var(--ease); }
.modal-enter-from .modal__card { transform: translateY(-8px) scale(.98); }
</style>
