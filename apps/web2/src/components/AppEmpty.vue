<script setup lang="ts">
/**
 * Empty · 空状态
 *
 * ★ 方案 P5：空状态要教人，不是"暂无数据"
 *   → 必须含：插画/图标 + 标题 + 说明 + 主 CTA
 */
import AppButton from './AppButton.vue'

withDefaults(defineProps<{
  icon?: string
  title?: string
  description?: string
  actionText?: string
  size?: 'sm' | 'md' | 'lg'
}>(), {
  icon: '📄',
  title: '还没有内容',
  description: '',
  actionText: '',
  size: 'md',
})

const emit = defineEmits<{ action: [] }>()
</script>

<template>
  <div class="empty" :class="`empty--${size}`">
    <div class="empty__icon" aria-hidden="true">{{ icon }}</div>
    <h3 class="empty__title">{{ title }}</h3>
    <p v-if="description" class="empty__desc">{{ description }}</p>
    <div v-if="actionText || $slots.action" class="empty__action">
      <slot name="action">
        <AppButton variant="primary" @click="emit('action')">{{ actionText }}</AppButton>
      </slot>
    </div>
  </div>
</template>

<style scoped>
.empty {
  display: flex; flex-direction: column; align-items: center;
  text-align: center;
  padding: var(--sp-8) var(--sp-5);
  color: var(--text-muted);
}
.empty--sm { padding: var(--sp-5) var(--sp-4); }
.empty--lg { padding: var(--sp-10) var(--sp-5); }

.empty__icon { font-size: 40px; line-height: 1; opacity: .55; margin-bottom: var(--sp-3); }
.empty--sm .empty__icon { font-size: 28px; }

.empty__title { font-size: var(--fs-base); color: var(--text); margin-bottom: var(--sp-2); }
.empty__desc {
  font-size: var(--fs-sm); max-width: 380px;
  line-height: var(--lh-normal); margin-bottom: var(--sp-4);
}
.empty__action { margin-top: var(--sp-2); }
</style>
