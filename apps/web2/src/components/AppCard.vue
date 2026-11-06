<script setup lang="ts">
/**
 * Card · 卡片
 */
withDefaults(defineProps<{
  title?: string
  subtitle?: string
  clickable?: boolean
  hoverable?: boolean
  padding?: 'none' | 'sm' | 'md' | 'lg'
  /** 左侧强调色条（用于状态区分） */
  accent?: 'none' | 'brand' | 'success' | 'warning' | 'danger'
}>(), {
  title: '',
  subtitle: '',
  clickable: false,
  hoverable: false,
  padding: 'md',
  accent: 'none',
})

const emit = defineEmits<{ click: [] }>()
</script>

<template>
  <div
    class="card"
    :class="[`card--pad-${padding}`, `card--accent-${accent}`,
             { 'is-clickable': clickable, 'is-hoverable': hoverable }]"
    :role="clickable ? 'button' : undefined"
    :tabindex="clickable ? 0 : undefined"
    @click="clickable && emit('click')"
    @keydown.enter="clickable && emit('click')"
    @keydown.space.prevent="clickable && emit('click')"
  >
    <header v-if="title || $slots.header || $slots.actions" class="card__head">
      <slot name="header">
        <div class="card__titles">
          <h3 class="card__title">{{ title }}</h3>
          <p v-if="subtitle" class="card__subtitle">{{ subtitle }}</p>
        </div>
      </slot>
      <div v-if="$slots.actions" class="card__actions">
        <slot name="actions" />
      </div>
    </header>

    <div class="card__body">
      <slot />
    </div>

    <footer v-if="$slots.footer" class="card__foot">
      <slot name="footer" />
    </footer>
  </div>
</template>

<style scoped>
.card {
  position: relative;
  display: flex; flex-direction: column;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  box-shadow: var(--sh-sm);
  transition: box-shadow var(--dur-base) var(--ease),
              border-color var(--dur-base) var(--ease),
              transform var(--dur-base) var(--ease);
  overflow: hidden;
}
.card--pad-none .card__body { padding: 0; }
.card--pad-sm .card__body { padding: var(--sp-3); }
.card--pad-md .card__body { padding: var(--sp-4); }
.card--pad-lg .card__body { padding: var(--sp-5); }

.is-hoverable:hover { box-shadow: var(--sh-md); border-color: var(--border-strong); }
.is-clickable { cursor: pointer; }
.is-clickable:hover { box-shadow: var(--sh-md); transform: translateY(-1px); }
.is-clickable:focus-visible { outline: 2px solid var(--brand-500); outline-offset: 1px; }

/* 左侧强调条 */
.card--accent-brand::before,
.card--accent-success::before,
.card--accent-warning::before,
.card--accent-danger::before {
  content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
}
.card--accent-brand::before { background: var(--brand-500); }
.card--accent-success::before { background: var(--success); }
.card--accent-warning::before { background: var(--warning); }
.card--accent-danger::before { background: var(--danger); }

.card__head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-4) var(--sp-4) 0;
}
.card--pad-none .card__head { padding: var(--sp-4); }
.card__titles { min-width: 0; }
.card__title { font-size: var(--fs-sm); font-weight: var(--fw-bold); }
.card__subtitle { font-size: var(--fs-xs); color: var(--text-muted); margin-top: 2px; }
.card__actions { display: flex; align-items: center; gap: var(--sp-2); flex-shrink: 0; }

.card__foot {
  padding: var(--sp-3) var(--sp-4);
  border-top: 1px solid var(--border);
  background: var(--bg-sunken);
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
</style>
