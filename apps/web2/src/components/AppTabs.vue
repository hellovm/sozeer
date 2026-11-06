<script setup lang="ts">
/**
 * AppTabs · 标签页（下划线/胶囊两种形态）
 * ★ 无障碍：role="tablist" + aria-selected + 键盘左右切换
 */
import { computed } from 'vue'
import type { TabItem } from '@/types/components'

const props = withDefaults(defineProps<{
  modelValue: string
  items: TabItem[]
  variant?: 'line' | 'pill'
  size?: 'sm' | 'md'
}>(), {
  variant: 'line',
  size: 'md',
})

const emit = defineEmits<{ 'update:modelValue': [string] }>()

const idx = computed(() => props.items.findIndex(i => i.key === props.modelValue))

function move(delta: number) {
  const n = props.items.length
  if (!n) return
  let i = idx.value
  for (let step = 0; step < n; step += 1) {
    i = (i + delta + n) % n
    if (!props.items[i]?.disabled) {
      emit('update:modelValue', props.items[i].key)
      return
    }
  }
}
</script>

<template>
  <div
    class="tabs"
    :class="[`tabs--${variant}`, `tabs--${size}`]"
    role="tablist"
    @keydown.left.prevent="move(-1)"
    @keydown.right.prevent="move(1)"
  >
    <button
      v-for="it in items"
      :key="it.key"
      class="tabs__item"
      :class="{ 'is-on': it.key === modelValue }"
      role="tab"
      type="button"
      :aria-selected="it.key === modelValue"
      :disabled="it.disabled"
      @click="emit('update:modelValue', it.key)"
    >
      <span>{{ it.label }}</span>
      <span v-if="it.badge !== undefined && it.badge !== ''" class="tabs__badge">{{ it.badge }}</span>
    </button>
  </div>
</template>

<style scoped>
.tabs { display: flex; align-items: center; gap: var(--sp-1); }
.tabs--line { border-bottom: 1px solid var(--border); gap: 0; }
.tabs--pill { background: var(--bg-sunken); border-radius: var(--r-md); padding: 2px; }

.tabs__item {
  display: inline-flex; align-items: center; gap: 5px;
  border: none; background: transparent;
  color: var(--text-muted);
  font-size: var(--fs-sm);
  white-space: nowrap;
  transition: color var(--dur-fast) var(--ease),
              background-color var(--dur-fast) var(--ease);
}
.tabs--sm .tabs__item { height: 26px; padding: 0 var(--sp-2); font-size: var(--fs-xs); }
.tabs--md .tabs__item { height: 34px; padding: 0 var(--sp-3); }

/* line */
.tabs--line .tabs__item {
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  border-radius: var(--r-sm) var(--r-sm) 0 0;
}
.tabs--line .tabs__item:hover { color: var(--text); background: var(--bg-hover); }
.tabs--line .tabs__item.is-on {
  color: var(--brand-600); border-bottom-color: var(--brand-500);
  font-weight: var(--fw-medium);
}

/* pill */
.tabs--pill .tabs__item { border-radius: var(--r-sm); }
.tabs--pill .tabs__item:hover { color: var(--text); }
.tabs--pill .tabs__item.is-on {
  background: var(--bg-elevated); color: var(--text);
  font-weight: var(--fw-medium); box-shadow: var(--sh-sm);
}

.tabs__item:disabled { opacity: .45; cursor: not-allowed; }

.tabs__badge {
  min-width: 16px; height: 16px; padding: 0 4px;
  display: inline-grid; place-items: center;
  border-radius: var(--r-full);
  background: var(--gray-200); color: var(--text-muted);
  font-size: 10px; font-variant-numeric: tabular-nums;
}
.tabs__item.is-on .tabs__badge { background: var(--brand-100); color: var(--brand-600); }
</style>
