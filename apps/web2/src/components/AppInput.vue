<script setup lang="ts">
/**
 * Input · 输入框
 *
 * 支持：错误态 / 前后缀 / 字数计数 / 清除按钮
 * ★ 无障碍：label 通过 aria-label 或外部 <label> 关联；错误用 aria-invalid + aria-describedby
 */
import { computed, ref } from 'vue'

const props = withDefaults(defineProps<{
  modelValue: string | number
  type?: string
  placeholder?: string
  label?: string
  hint?: string
  error?: string
  disabled?: boolean
  readonly?: boolean
  maxlength?: number
  showCount?: boolean
  clearable?: boolean
  prefix?: string
  suffix?: string
  autocomplete?: string
  size?: 'sm' | 'md' | 'lg'
  ariaLabel?: string
}>(), {
  type: 'text',
  placeholder: '',
  label: '',
  hint: '',
  error: '',
  disabled: false,
  readonly: false,
  maxlength: 0,
  showCount: false,
  clearable: false,
  prefix: '',
  suffix: '',
  autocomplete: 'off',
  size: 'md',
  ariaLabel: '',
})

const emit = defineEmits<{
  'update:modelValue': [string]
  enter: []
  blur: []
  focus: []
}>()

const uid = `in-${Math.random().toString(36).slice(2, 9)}`
const focused = ref(false)

const hasError = computed(() => Boolean(props.error))
const count = computed(() => String(props.modelValue ?? '').length)

function onInput(e: Event) {
  emit('update:modelValue', (e.target as HTMLInputElement).value)
}

function clear() {
  emit('update:modelValue', '')
}
</script>

<template>
  <div class="field" :class="{ 'is-error': hasError, 'is-disabled': disabled }">
    <label v-if="label" class="field__label" :for="uid">
      {{ label }}
      <span v-if="showCount && maxlength" class="field__count">{{ count }}/{{ maxlength }}</span>
    </label>

    <div class="field__box" :class="[`field__box--${size}`, { 'is-focus': focused }]">
      <span v-if="prefix" class="field__affix" aria-hidden="true">{{ prefix }}</span>
      <input
        :id="uid"
        class="field__input"
        :type="type"
        :value="modelValue"
        :placeholder="placeholder"
        :disabled="disabled"
        :readonly="readonly"
        :maxlength="maxlength || undefined"
        :autocomplete="autocomplete"
        :aria-label="ariaLabel || label || placeholder"
        :aria-invalid="hasError"
        :aria-describedby="(hint || error) ? `${uid}-msg` : undefined"
        @input="onInput"
        @focus="focused = true; emit('focus')"
        @blur="focused = false; emit('blur')"
        @keydown.enter="emit('enter')"
      >
      <button
        v-if="clearable && count > 0 && !disabled"
        class="field__clear"
        type="button"
        aria-label="清空"
        @click="clear"
      >×</button>
      <span v-if="suffix" class="field__affix" aria-hidden="true">{{ suffix }}</span>
    </div>

    <p v-if="error" :id="`${uid}-msg`" class="field__msg field__msg--err" role="alert">{{ error }}</p>
    <p v-else-if="hint" :id="`${uid}-msg`" class="field__msg">{{ hint }}</p>
  </div>
</template>

<style scoped>
.field { display: flex; flex-direction: column; gap: var(--sp-1); }

.field__label {
  display: flex; align-items: center; justify-content: space-between;
  font-size: var(--fs-xs); font-weight: var(--fw-medium); color: var(--text-muted);
}
.field__count { font-weight: var(--fw-normal); color: var(--text-subtle); font-variant-numeric: tabular-nums; }

.field__box {
  display: flex; align-items: center; gap: var(--sp-2);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 0 var(--sp-3);
  transition: border-color var(--dur-fast) var(--ease),
              box-shadow var(--dur-fast) var(--ease);
}
.field__box--sm { height: 30px; }
.field__box--md { height: 36px; }
.field__box--lg { height: 44px; }

.field__box.is-focus { border-color: var(--brand-500); box-shadow: var(--focus-ring); }
.field.is-error .field__box { border-color: var(--danger); }
.field.is-error .field__box.is-focus { box-shadow: 0 0 0 3px rgba(229, 62, 62, .22); }
.field.is-disabled .field__box { background: var(--bg-sunken); opacity: .7; }

.field__input {
  flex: 1 1 auto; min-width: 0;
  border: none; outline: none; background: transparent;
  font-size: var(--fs-sm); color: var(--text);
}
.field__input::placeholder { color: var(--text-subtle); }
.field__input:disabled { cursor: not-allowed; }

.field__affix { color: var(--text-subtle); font-size: var(--fs-sm); flex-shrink: 0; }

.field__clear {
  flex-shrink: 0;
  width: 18px; height: 18px; line-height: 1;
  border: none; border-radius: var(--r-full);
  background: var(--gray-300); color: var(--bg-elevated);
  font-size: 13px; display: grid; place-items: center;
}
.field__clear:hover { background: var(--gray-400); }

.field__msg { font-size: var(--fs-xs); color: var(--text-subtle); }
.field__msg--err { color: var(--danger); }
</style>
