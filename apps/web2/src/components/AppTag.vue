<script setup lang="ts">
/**
 * Tag · 标签 / 状态徽标
 */
withDefaults(defineProps<{
  kind?: 'default' | 'brand' | 'success' | 'warning' | 'danger' | 'info'
  size?: 'sm' | 'md'
  dot?: boolean
  round?: boolean
}>(), {
  kind: 'default',
  size: 'md',
  dot: false,
  round: false,
})
</script>

<template>
  <span
    class="tag"
    :class="[`tag--${kind}`, `tag--${size}`, { 'tag--round': round }]"
  >
    <span v-if="dot" class="tag__dot" aria-hidden="true" />
    <slot />
  </span>
</template>

<style scoped>
.tag {
  display: inline-flex; align-items: center; gap: var(--sp-1);
  border: 1px solid transparent;
  border-radius: var(--r-sm);
  font-weight: var(--fw-medium);
  white-space: nowrap;
  line-height: 1;
}
.tag--round { border-radius: var(--r-full); }
.tag--sm { height: 18px; padding: 0 6px; font-size: 11px; }
.tag--md { height: 22px; padding: 0 var(--sp-2); font-size: var(--fs-xs); }

.tag--default { background: var(--gray-100); color: var(--text-muted); border-color: var(--border); }
.tag--brand   { background: var(--brand-50); color: var(--brand-600); }
.tag--success { background: var(--success-bg); color: var(--success); }
.tag--warning { background: var(--warning-bg); color: #8a6510; }
.tag--danger  { background: var(--danger-bg); color: var(--danger); }
.tag--info    { background: var(--info-bg); color: var(--info); }

[data-theme="dark"] .tag--warning { color: #e8b64a; }

.tag__dot {
  width: 5px; height: 5px; border-radius: 50%;
  background: currentColor; flex-shrink: 0;
}
</style>
