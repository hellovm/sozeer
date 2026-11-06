<script setup lang="ts">
/**
 * Progress · 进度条
 *
 * ★ 方案 §1.5.2：长任务必须显示进度百分比 + 预计剩余时间（不能模糊的"处理中"）
 */
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  /** 0-100 */
  percent: number
  variant?: 'line' | 'circle'
  size?: 'sm' | 'md'
  kind?: 'brand' | 'success' | 'warning' | 'danger'
  /** 显示百分比文字 */
  showText?: boolean
  /** 预计剩余时间描述，如"约 3~6 分钟" */
  eta?: string
  /** 已完成的细分描述，如"已完成 2/5 章" */
  detail?: string
  label?: string
  striped?: boolean
}>(), {
  variant: 'line',
  size: 'md',
  kind: 'brand',
  showText: true,
  eta: '',
  detail: '',
  label: '',
  striped: false,
})

const safePercent = computed(() =>
  Math.max(0, Math.min(100, Number(props.percent) || 0)))

/** ★ 环形进度用 SVG（避免 canvas 的响应式麻烦） */
const R = 26
const CIRC = 2 * Math.PI * R
const dashOffset = computed(() => CIRC * (1 - safePercent.value / 100))
</script>

<template>
  <div class="prog" :class="[`prog--${size}`]">
    <div v-if="label || showText" class="prog__head">
      <span class="prog__label">{{ label }}</span>
      <span v-if="showText" class="prog__pct">{{ Math.round(safePercent) }}%</span>
    </div>

    <!-- 线形 -->
    <div
      v-if="variant === 'line'"
      class="prog__track"
      role="progressbar"
      :aria-valuenow="Math.round(safePercent)"
      aria-valuemin="0"
      aria-valuemax="100"
      :aria-label="label || '进度'"
    >
      <div
        class="prog__fill"
        :class="[`prog__fill--${kind}`, { 'is-striped': striped }]"
        :style="{ width: safePercent + '%' }"
      />
    </div>

    <!-- 环形 -->
    <div v-else class="prog__circle">
      <svg width="64" height="64" viewBox="0 0 64 64" aria-hidden="true">
        <circle cx="32" cy="32" :r="R" fill="none"
                stroke="var(--gray-200)" stroke-width="5" />
        <circle cx="32" cy="32" :r="R" fill="none"
                :stroke="`var(--${kind === 'brand' ? 'brand-500' : kind})`"
                stroke-width="5" stroke-linecap="round"
                :stroke-dasharray="CIRC" :stroke-dashoffset="dashOffset"
                transform="rotate(-90 32 32)"
                class="prog__ring" />
      </svg>
      <span class="prog__circle-text">{{ Math.round(safePercent) }}%</span>
    </div>

    <div v-if="detail || eta" class="prog__meta">
      <span v-if="detail">{{ detail }}</span>
      <span v-if="eta" class="prog__eta">· 预计还需 {{ eta }}</span>
    </div>
  </div>
</template>

<style scoped>
.prog { display: flex; flex-direction: column; gap: var(--sp-2); }

.prog__head {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: var(--sp-3); font-size: var(--fs-xs);
}
.prog__label { color: var(--text-muted); }
.prog__pct {
  color: var(--text); font-weight: var(--fw-bold);
  font-variant-numeric: tabular-nums;
}

.prog__track {
  height: 6px; border-radius: var(--r-full);
  background: var(--gray-200); overflow: hidden;
}
.prog--sm .prog__track { height: 4px; }

.prog__fill {
  height: 100%; border-radius: var(--r-full);
  transition: width var(--dur-slow) var(--ease);
}
.prog__fill--brand   { background: var(--brand-500); }
.prog__fill--success { background: var(--success); }
.prog__fill--warning { background: var(--warning); }
.prog__fill--danger  { background: var(--danger); }

.prog__fill.is-striped {
  background-image: linear-gradient(45deg,
    rgba(255,255,255,.22) 25%, transparent 25%,
    transparent 50%, rgba(255,255,255,.22) 50%,
    rgba(255,255,255,.22) 75%, transparent 75%, transparent);
  background-size: 12px 12px;
  animation: prog-stripe .8s linear infinite;
}
@keyframes prog-stripe { to { background-position: 12px 0; } }

.prog__circle { position: relative; width: 64px; height: 64px; }
.prog__ring { transition: stroke-dashoffset var(--dur-slow) var(--ease); }
.prog__circle-text {
  position: absolute; inset: 0;
  display: grid; place-items: center;
  font-size: var(--fs-xs); font-weight: var(--fw-bold);
  font-variant-numeric: tabular-nums;
}

.prog__meta { display: flex; gap: var(--sp-1); font-size: var(--fs-xs); color: var(--text-subtle); }
.prog__eta { color: var(--text-muted); }
</style>
