<script setup lang="ts">
/**
 * Skeleton · 骨架屏
 *
 * ★ 方案 §1.5.1：加载中要骨架屏，不要转圈
 *   （骨架屏让用户预判布局，感知等待更短）
 */
withDefaults(defineProps<{
  variant?: 'text' | 'card' | 'table' | 'chart'
  rows?: number
  count?: number
}>(), {
  variant: 'text',
  rows: 3,
  count: 1,
})
</script>

<template>
  <div class="sk" role="status" aria-busy="true" aria-label="加载中">
    <!-- 文本行 -->
    <template v-if="variant === 'text'">
      <div v-for="i in count" :key="i" class="sk__block">
        <div v-for="r in rows" :key="r" class="sk__line"
             :style="{ width: r === rows ? '62%' : '100%' }" />
      </div>
    </template>

    <!-- 卡片 -->
    <template v-else-if="variant === 'card'">
      <div v-for="i in count" :key="i" class="sk__card">
        <div class="sk__line sk__line--lg" style="width: 40%" />
        <div class="sk__line" style="width: 100%" />
        <div class="sk__line" style="width: 78%" />
      </div>
    </template>

    <!-- 表格 -->
    <template v-else-if="variant === 'table'">
      <div class="sk__table">
        <div v-for="r in rows" :key="r" class="sk__tr">
          <div class="sk__line" style="width: 22%" />
          <div class="sk__line" style="width: 34%" />
          <div class="sk__line" style="width: 16%" />
          <div class="sk__line" style="width: 20%" />
        </div>
      </div>
    </template>

    <!-- 图表 -->
    <template v-else>
      <div class="sk__chart">
        <div v-for="r in rows" :key="r" class="sk__bar"
             :style="{ height: `${28 + ((r * 37) % 62)}%` }" />
      </div>
    </template>

    <span class="sr-only">加载中…</span>
  </div>
</template>

<style scoped>
.sk { display: flex; flex-direction: column; gap: var(--sp-3); }
.sk__block { display: flex; flex-direction: column; gap: var(--sp-2); }
.sk__card {
  display: flex; flex-direction: column; gap: var(--sp-3);
  padding: var(--sp-4);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
}
.sk__table { display: flex; flex-direction: column; gap: var(--sp-3); }
.sk__tr { display: flex; gap: var(--sp-3); align-items: center; }

.sk__line {
  height: 12px;
  border-radius: var(--r-sm);
  background: linear-gradient(90deg,
    var(--gray-200) 25%, var(--gray-100) 37%, var(--gray-200) 63%);
  background-size: 400% 100%;
  animation: shimmer 1.4s ease-in-out infinite;
}
.sk__line--lg { height: 18px; }

.sk__chart {
  display: flex; align-items: flex-end; gap: var(--sp-2);
  height: 160px; padding: var(--sp-3);
  border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--bg-elevated);
}
.sk__bar {
  flex: 1 1 0; min-width: 8px;
  border-radius: var(--r-sm) var(--r-sm) 0 0;
  background: linear-gradient(90deg,
    var(--gray-200) 25%, var(--gray-100) 37%, var(--gray-200) 63%);
  background-size: 400% 100%;
  animation: shimmer 1.4s ease-in-out infinite;
}

@keyframes shimmer {
  0% { background-position: 100% 50%; }
  100% { background-position: 0 50%; }
}
</style>
