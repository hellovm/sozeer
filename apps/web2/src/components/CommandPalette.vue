<script setup lang="ts">
/**
 * CommandPalette · 命令面板（⌘K / Ctrl+K）
 *
 * ★ 方案 G7：全局搜索 + 跳转 + 执行命令
 * ★ 无障碍：combobox 模式 + 键盘上下选择 + Esc 关闭
 */
import { computed, nextTick, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useUiStore } from '@/stores/ui'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const ui = useUiStore()
const auth = useAuthStore()

interface Cmd {
  id: string
  label: string
  hint?: string
  icon?: string
  keywords?: string
  run: () => void
}

const q = ref('')
const idx = ref(0)
const inputRef = ref<HTMLInputElement | null>(null)

const commands = computed<Cmd[]>(() => {
  const list: Cmd[] = [
    { id: 'home', label: '工作台', icon: '◎', keywords: 'home dashboard', run: () => go({ name: 'home' }) },
    { id: 'books', label: '我的作品', icon: '✎', keywords: 'books novel', run: () => go({ name: 'books' }) },
    { id: 'drama', label: '短剧项目', icon: '▶', keywords: 'drama video', run: () => go({ name: 'drama' }) },
    { id: 'assets', label: '角色资产', icon: '❖', keywords: 'character asset', run: () => go({ name: 'asset-characters' }) },
    { id: 'genres', label: '题材包', icon: '❖', keywords: 'genre pack', run: () => go({ name: 'asset-genres' }) },
    { id: 'usage', label: '用量与成本', icon: '▤', keywords: 'usage cost 积分', run: () => go({ name: 'data-usage' }) },
    { id: 'stats', label: '写作统计', icon: '▤', keywords: 'stats 统计', run: () => go({ name: 'data-stats' }) },
    { id: 'profile', label: '个人资料', icon: '☺', keywords: 'profile 资料', run: () => go({ name: 'account-profile' }) },
    { id: 'security', label: '安全设置', icon: '⚿', keywords: 'security password 密码', run: () => go({ name: 'account-security' }) },
    { id: 'notify', label: '通知中心', icon: '◔', keywords: 'notification 通知', run: () => go({ name: 'account-notifications' }) },
    { id: 'help', label: '帮助中心', icon: '?', keywords: 'help faq', run: () => go({ name: 'help' }) },
    {
      id: 'theme', label: ui.isDark ? '切换到亮色主题' : '切换到暗色主题',
      icon: '◐', keywords: 'theme dark light 主题', run: () => { ui.toggleTheme(); close() },
    },
  ]
  if (auth.can('admin.access')) {
    list.push({
      id: 'admin', label: '打开管理后台', icon: '⚙', keywords: 'admin 后台',
      run: () => { window.open('/admin/', '_blank', 'noopener'); close() },
    })
  }
  return list
})

const results = computed(() => {
  const kw = q.value.trim().toLowerCase()
  if (!kw) return commands.value
  return commands.value.filter(c =>
    c.label.toLowerCase().includes(kw) ||
    (c.keywords || '').toLowerCase().includes(kw) ||
    c.id.includes(kw))
})

watch(results, () => { idx.value = 0 })

watch(() => ui.commandOpen, async (open) => {
  if (open) {
    q.value = ''
    idx.value = 0
    await nextTick()
    inputRef.value?.focus()
  }
})

function go(to: Record<string, unknown>) {
  router.push(to as never)
  close()
}

function close() { ui.closeCommand() }

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') { e.preventDefault(); close(); return }
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    idx.value = Math.min(idx.value + 1, Math.max(0, results.value.length - 1))
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    idx.value = Math.max(idx.value - 1, 0)
  } else if (e.key === 'Enter') {
    e.preventDefault()
    results.value[idx.value]?.run()
  }
}
</script>

<template>
  <Teleport to="body">
    <Transition name="cp">
      <div
        v-if="ui.commandOpen"
        class="cp"
        role="dialog"
        aria-modal="true"
        aria-label="命令面板"
        @keydown="onKeydown"
      >
        <div class="cp__mask" @click="close" />
        <div class="cp__card">
          <div class="cp__input-row">
            <span class="cp__icon" aria-hidden="true">⌕</span>
            <input
              ref="inputRef"
              v-model="q"
              class="cp__input"
              type="text"
              placeholder="搜索页面、命令…"
              role="combobox"
              aria-expanded="true"
              aria-controls="cp-list"
              aria-autocomplete="list"
            >
            <kbd class="cp__kbd">Esc</kbd>
          </div>

          <ul id="cp-list" class="cp__list" role="listbox">
            <li
              v-for="(c, i) in results"
              :key="c.id"
              class="cp__item"
              :class="{ 'is-active': i === idx }"
              role="option"
              :aria-selected="i === idx"
              @mouseenter="idx = i"
              @click="c.run()"
            >
              <span class="cp__item-ico" aria-hidden="true">{{ c.icon }}</span>
              <span class="cp__item-label">{{ c.label }}</span>
            </li>
            <li v-if="!results.length" class="cp__empty">没有匹配的命令</li>
          </ul>

          <div class="cp__foot">
            <span><kbd>↑</kbd><kbd>↓</kbd> 选择</span>
            <span><kbd>↵</kbd> 执行</span>
            <span><kbd>Esc</kbd> 关闭</span>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.cp {
  position: fixed; inset: 0;
  z-index: var(--z-command);
  display: flex; justify-content: center;
  padding-top: 12vh;
}
.cp__mask {
  position: absolute; inset: 0;
  background: rgba(10, 14, 20, .45);
  backdrop-filter: blur(2px);
}
.cp__card {
  position: relative;
  width: min(92vw, 560px);
  max-height: 60vh;
  display: flex; flex-direction: column;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  box-shadow: var(--sh-lg);
  overflow: hidden;
}
.cp__input-row {
  display: flex; align-items: center; gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
  border-bottom: 1px solid var(--border);
}
.cp__icon { color: var(--text-subtle); font-size: 16px; }
.cp__input {
  flex: 1 1 auto; border: none; outline: none; background: transparent;
  font-size: var(--fs-base); color: var(--text);
}
.cp__input::placeholder { color: var(--text-subtle); }
.cp__kbd, .cp__foot kbd {
  font-family: var(--font-mono); font-size: 10px;
  padding: 2px 5px; border-radius: var(--r-sm);
  border: 1px solid var(--border); background: var(--bg-sunken);
  color: var(--text-subtle);
}
.cp__list { flex: 1 1 auto; overflow-y: auto; padding: var(--sp-2); }
.cp__item {
  display: flex; align-items: center; gap: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-md);
  font-size: var(--fs-sm); cursor: pointer;
}
.cp__item.is-active { background: var(--brand-50); color: var(--brand-600); }
[data-theme="dark"] .cp__item.is-active { background: var(--brand-50); color: var(--text); }
.cp__item-ico { width: 18px; text-align: center; opacity: .7; }
.cp__empty { padding: var(--sp-5); text-align: center; color: var(--text-subtle); font-size: var(--fs-sm); }
.cp__foot {
  display: flex; gap: var(--sp-4);
  padding: var(--sp-2) var(--sp-4);
  border-top: 1px solid var(--border);
  background: var(--bg-sunken);
  font-size: 11px; color: var(--text-subtle);
}
.cp__foot span { display: flex; align-items: center; gap: 4px; }

.cp-enter-active, .cp-leave-active { transition: opacity var(--dur-fast) var(--ease); }
.cp-enter-from, .cp-leave-to { opacity: 0; }
</style>
