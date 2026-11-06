<script setup lang="ts">
/**
 * AdminConfirm.vue · 后台二次确认（v0.26.2 G0-6）
 *
 * ★★ 为什么后台每个敏感操作都必须走这里（MEMORY §30/§67）：
 *   ① **不可逆操作要人工把关** —— 封号、开票、作废、退款都不可撤销
 *   ② **必须填原因** —— 审计日志只有动作没有原因的话，事后无法复盘
 *   ③ **失败也要留痕** —— 原因要**先**收集，再调接口；不能等成功才记
 *
 * ★ 交互细节：
 *   · 原因至少 2 字符（1 个字符 = 敷衍，等同于没填）
 *   · 危险操作按钮用红色（视觉警示）
 *   · 确认按钮在原因未填时**禁用**（而不是点了报错）
 *   · Enter 提交 / Esc 取消
 */
import { computed, nextTick, ref, watch } from 'vue'
import AppModal from '@/components/AppModal.vue'
import AppButton from '@/components/AppButton.vue'

const props = withDefaults(defineProps<{
  modelValue: boolean
  title: string
  /** 支持简单 HTML（内部由调用方保证安全，如 <b> 高亮） */
  body?: string
  /** 是否需要填写原因（默认 true —— 敏感操作必须留痕） */
  needReason?: boolean
  okText?: string
  /** 危险操作（红色按钮 + 额外警示） */
  danger?: boolean
  /** 原因框的占位提示 */
  reasonPlaceholder?: string
  /** 确认按钮 loading（提交中禁用重复点击） */
  loading?: boolean
}>(), {
  body: '',
  needReason: true,
  okText: '确认执行',
  danger: true,
  reasonPlaceholder: '请填写原因（审计日志会记录，至少 2 个字符）',
  loading: false,
})

const emit = defineEmits<{
  'update:modelValue': [v: boolean]
  /** 确认：回传原因（needReason=false 时为空串） */
  confirm: [reason: string]
}>()

const reason = ref('')
const touched = ref(false)
const taRef = ref<HTMLTextAreaElement | null>(null)

const open = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

/** ★ 填了原因才允许确认（比"点了报错"体验好，也不容易误触） */
const reasonOk = computed(() => !props.needReason || reason.value.trim().length >= 2)
const canSubmit = computed(() => reasonOk.value && !props.loading)

watch(() => props.modelValue, async (v) => {
  if (!v) {
    reason.value = ''
    touched.value = false
    return
  }
  await nextTick()
  taRef.value?.focus()
})

function submit() {
  touched.value = true
  if (!canSubmit.value) return
  emit('confirm', reason.value.trim())
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey || !props.needReason)) {
    e.preventDefault()
    submit()
  }
}
</script>

<template>
  <AppModal v-model="open" :title="title" width="480px">
    <!-- ★ 操作说明（调用方传入，支持 <b> 高亮风险点） -->
    <div v-if="body" class="confirm__body" v-html="body" />

    <div v-if="needReason" class="confirm__field">
      <label class="confirm__label">
        操作原因 <span class="req">必填</span>
      </label>
      <textarea
        ref="taRef"
        v-model="reason"
        class="confirm__ta"
        rows="3"
        :placeholder="reasonPlaceholder"
        @keydown="onKeydown"
      />
      <div class="confirm__hint">
        <span :class="['confirm__count', { bad: touched && !reasonOk }]">
          {{ reason.trim().length }} / 2 字符起
        </span>
        <span class="confirm__tip">原因会写入审计日志（含失败的操作）</span>
      </div>
    </div>

    <template #footer>
      <AppButton variant="ghost" :disabled="loading" @click="open = false">取消</AppButton>
      <AppButton
        :variant="danger ? 'danger' : 'primary'"
        :disabled="!canSubmit"
        :loading="loading"
        @click="submit"
      >
        {{ okText }}
      </AppButton>
    </template>
  </AppModal>
</template>

<style scoped>
.confirm__body {
  font-size: 13px;
  line-height: 1.85;
  color: var(--text-muted);
  margin-bottom: 14px;
}
.confirm__field { display: flex; flex-direction: column; gap: 6px; }
.confirm__label { font-size: 12px; color: var(--text-muted); }
.req { color: var(--danger, #d33); margin-left: 4px; font-size: 11px; }
.confirm__ta {
  width: 100%;
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  resize: vertical;
}
.confirm__ta:focus { outline: none; border-color: var(--brand); }
.confirm__hint {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: 11px;
  color: var(--text-faint);
}
.confirm__count.bad { color: var(--danger, #d33); }
.confirm__tip { text-align: right; }
</style>
