<script setup lang="ts">
/**
 * AdminBars.vue · 后台柱状图（v0.26.2 G0-6）
 *
 * ★ 为什么不用图表库（ECharts/Chart.js）：
 *   后台只需要"看趋势"，两根柱子的对比用纯 CSS 足够。
 *   引入图表库会带来 200KB+ 体积，而管理页是低频页 —— 不划算。
 *
 * ★ 用 flex + height 百分比绘制：天然自适应宽度，窄屏自动压缩。
 */
import { computed } from 'vue'

interface Series {
  label: string
  data: number[]
  color: string
  /** 是否显示在 tooltip 中（如"金额"） */
  unit?: string
}

const props = withDefaults(defineProps<{
  labels: string[]
  series: Series[]
  height?: number
  /** 数值格式化（tooltip 用） */
  format?: (n: number) => string
}>(), {
  height: 130,
  format: (n: number) => String(n),
})

/** 全局最大值 —— 所有系列共用一个刻度，否则对比失真 */
const maxVal = computed(() => {
  let m = 1
  for (const s of props.series) {
    for (const v of s.data) {
      const n = Number(v) || 0
      if (n > m) m = n
    }
  }
  return m
})

function barHeight(v: number): string {
  const n = Number(v) || 0
  if (n <= 0) return '2px'
  // ★ 最少 3px：否则"有值但很小"与"零"看起来一样
  const px = Math.max(3, Math.round((n / maxVal.value) * props.height))
  return px + 'px'
}

function tip(i: number): string {
  const parts = props.series.map((s) => `${s.label}：${props.format(Number(s.data[i]) || 0)}`)
  return `${props.labels[i]}\n${parts.join('\n')}`
}
</script>

<template>
  <div class="bars">
    <div class="bars__legend">
      <span v-for="s in series" :key="s.label" class="bars__lg">
        <i :style="{ background: s.color }" />{{ s.label }}
      </span>
    </div>

    <div class="bars__chart" :style="{ height: height + 24 + 'px' }">
      <div
        v-for="(lb, i) in labels"
        :key="lb + i"
        class="bars__col"
        :title="tip(i)"
      >
        <div class="bars__stack">
          <span
            v-for="s in series"
            :key="s.label"
            class="bars__bar"
            :style="{ height: barHeight(s.data[i] ?? 0), background: s.color }"
          />
        </div>
        <span class="bars__lb">{{ lb }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.bars { width: 100%; }
.bars__legend { display: flex; gap: 16px; margin-bottom: 10px; }
.bars__lg { display: inline-flex; align-items: center; gap: 5px;
  font-size: 11px; color: var(--text-muted); }
.bars__lg i { width: 9px; height: 9px; border-radius: 2px; display: inline-block; }

.bars__chart {
  display: flex;
  align-items: flex-end;
  gap: 3px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 0;
  overflow-x: auto;
}
.bars__col {
  flex: 1 1 0;
  min-width: 16px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  height: 100%;
}
.bars__stack {
  display: flex;
  align-items: flex-end;
  justify-content: center;
  gap: 2px;
  flex: 1;
  width: 100%;
}
.bars__bar {
  flex: 1 1 0;
  max-width: 11px;
  border-radius: 2px 2px 0 0;
  transition: opacity .12s;
}
.bars__col:hover .bars__bar { opacity: .78; }
.bars__lb {
  font-size: 9px;
  color: var(--text-faint);
  margin-top: 4px;
  white-space: nowrap;
  transform: rotate(-38deg);
  transform-origin: center;
  height: 18px;
}
</style>
