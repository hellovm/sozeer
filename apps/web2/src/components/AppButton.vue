<script setup lang="ts">
/**
 * Button · 基础按钮
 *
 * 变体：primary（主操作）/ ghost（次要）/ danger（危险）/ text（纯文字）
 * 尺寸：sm / md / lg
 * ★ 无障碍：loading 时 aria-busy，禁用时 aria-disabled
 */
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  variant?: 'primary' | 'ghost' | 'danger' | 'text' | 'accent'
  size?: 'sm' | 'md' | 'lg'
  block?: boolean
  disabled?: boolean
  loading?: boolean
  type?: 'button' | 'submit' | 'reset'
  icon?: string
}>(), {
  variant: 'ghost',
  size: 'md',
  block: false,
  disabled: false,
  loading: false,
  type: 'button',
  icon: '',
})

const emit = defineEmits<{ click: [MouseEvent] }>()

const isDisabled = computed(() => props.disabled || props.loading)

function onClick(e: MouseEvent) {
  if (isDisabled.value) {
    e.preventDefault()
    e.stopPropagation()
    return
  }
  emit('click', e)
}
</script>

<template>
  <button
    class="btn"
    :class="[`btn--${variant}`, `btn--${size}`, { 'btn--block': block, 'is-loading': loading }]"
    :type="type"
    :disabled="isDisabled"
    :aria-disabled="isDisabled"
    :aria-busy="loading"
    @click="onClick"
  >
    <span v-if="loading" class="btn__spin" aria-hidden="true" />
    <span v-else-if="icon" class="btn__icon" aria-hidden="true">{{ icon }}</span>
    <span class="btn__label"><slot /></span>
  </button>
</template>

<style scoped>
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  border: 1px solid transparent;
  border-radius: var(--r-md);
  font-weight: var(--fw-medium);
  white-space: nowrap;
  transition: background-color var(--dur-fast) var(--ease),
              border-color var(--dur-fast) var(--ease),
              color var(--dur-fast) var(--ease),
              box-shadow var(--dur-fast) var(--ease);
  user-select: none;
}
.btn:focus-visible { outline: 2px solid var(--brand-500); outline-offset: 2px; }
.btn:disabled { opacity: .5; }

/* ── 尺寸 ── */
.btn--sm { height: 28px; padding: 0 var(--sp-3); font-size: var(--fs-xs); border-radius: var(--r-sm); }
.btn--md { height: 36px; padding: 0 var(--sp-4); font-size: var(--fs-sm); }
.btn--lg { height: 44px; padding: 0 var(--sp-5); font-size: var(--fs-base); }
.btn--block { display: flex; width: 100%; }

/* ── primary ── */
.btn--primary {
  background: var(--brand-500);
  color: #fff;
  box-shadow: var(--sh-sm);
}
.btn--primary:hover:not(:disabled) { background: var(--brand-600); }
.btn--primary:active:not(:disabled) { background: var(--brand-700); }

/* ── accent（仅核心 CTA） ── */
.btn--accent {
  background: var(--accent);
  color: #fff;
  box-shadow: var(--sh-sm);
}
.btn--accent:hover:not(:disabled) { filter: brightness(1.06); }

/* ── ghost ── */
.btn--ghost {
  background: var(--bg-elevated);
  border-color: var(--border);
  color: var(--text);
}
.btn--ghost:hover:not(:disabled) { background: var(--bg-hover); border-color: var(--border-strong); }
.btn--ghost:active:not(:disabled) { background: var(--bg-active); }

/* ── danger ── */
.btn--danger {
  background: var(--danger);
  color: #fff;
}
.btn--danger:hover:not(:disabled) { filter: brightness(.94); }

/* ── text ── */
.btn--text {
  background: transparent;
  color: var(--brand-500);
  padding: 0 var(--sp-2);
}
.btn--text:hover:not(:disabled) { background: var(--brand-50); }

.btn__icon { font-size: 1.05em; line-height: 1; }

/* 加载转圈 */
.btn__spin {
  width: 13px; height: 13px;
  border: 2px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: btnspin .7s linear infinite;
}
@keyframes btnspin { to { transform: rotate(360deg); } }
</style>
