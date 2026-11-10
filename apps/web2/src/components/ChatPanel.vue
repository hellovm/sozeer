<script setup lang="ts">
/**
 * ChatPanel · 对话流（写作台中栏）
 *
 * ★ 长任务交互（方案 §1.5.2）：
 *   流式输出 + 进度提示 + 可中途取消
 * ★ 自动滚动到底部，但**用户上滑时暂停**（否则读历史会被拽走）
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import type { ChatMessage } from '@/api/modules/book'
import AppButton from '@/components/AppButton.vue'
import AppEmpty from '@/components/AppEmpty.vue'

const props = withDefaults(defineProps<{
  messages: ChatMessage[]
  streaming?: boolean
  loading?: boolean
  /** 流式增量文本（未落库的部分） */
  streamText?: string
  /** ★ #61 agent 调用/过程活动流（「正在读第 N 章」「已记录人物…」） */
  actions?: Array<{ icon: string; text: string }>
  disabled?: boolean
}>(), {
  streaming: false,
  loading: false,
  streamText: '',
  actions: () => [],
  disabled: false,
})

const emit = defineEmits<{
  send: [text: string]
  stop: []
  newConversation: []
}>()

const draft = ref('')
const scroller = ref<HTMLElement | null>(null)
/** ★ 用户上滑后暂停自动滚动 */
const pinned = ref(true)

/** ★ 上滑后显示"回到最新"悬浮按钮（再点一次强制回底） */
const showJump = computed(() => !pinned.value && props.messages.length > 0)

const canSend = computed(() => Boolean(draft.value.trim()) && !props.streaming && !props.disabled)

function onScroll() {
  const el = scroller.value
  if (!el) return
  // 距底部 < 60px 视为"在底部"
  pinned.value = el.scrollHeight - el.scrollTop - el.clientHeight < 60
}

async function scrollToBottom(force = false) {
  if (!force && !pinned.value) return
  await nextTick()
  const el = scroller.value
  if (el) el.scrollTop = el.scrollHeight
}

/** ★ 点击悬浮按钮：强制落到底部并恢复"跟随" */
function jumpToBottom() {
  pinned.value = true
  scrollToBottom(true)
}

watch(() => props.messages.length, (n, o) => {
  // ★ v0.27.2：历史消息首次加载（0 → N，重新进入对话）时强制落底，
  //   否则滚动条会停在最老的聊天记录（用户需手动翻）。
  if (o === 0 && n > 0) scrollToBottom(true)
  else scrollToBottom()
})
watch(() => props.streamText, () => scrollToBottom())

// ★ v0.27.2：组件挂载（含已加载的历史消息）时直接落到底部
onMounted(() => {
  scrollToBottom(true)
})

function send() {
  const t = draft.value.trim()
  if (!t || props.streaming) return
  emit('send', t)
  draft.value = ''
  pinned.value = true
  scrollToBottom(true)
}

/** ★ Ctrl/⌘ + Enter 发送（普通 Enter 换行，长文本更友好） */
function onKeydown(e: KeyboardEvent) {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
    e.preventDefault()
    send()
  }
}

const QUICK = [
  '帮我起草下一章',
  '梳理一下目前的人物关系',
  '这章节奏有点慢，帮我看看怎么改',
  '检查有没有设定冲突',
]

function roleLabel(r: string) {
  return r === 'user' ? '我' : r === 'assistant' ? '主编' : '系统'
}
</script>

<template>
  <section class="chat" aria-label="创作对话">
    <!-- 头部 -->
    <header class="chat__head">
      <div class="chat__who">
        <span class="chat__avatar" aria-hidden="true">编</span>
        <div>
          <strong>创作主编</strong>
          <span class="muted tiny" style="display:block">
            {{ streaming ? '正在思考…' : '随时待命' }}
          </span>
        </div>
      </div>
      <AppButton size="sm" variant="text" @click="emit('newConversation')">＋ 新对话</AppButton>
    </header>

    <!-- 消息流 -->
    <div ref="scroller" class="chat__body" @scroll="onScroll">
      <div v-if="loading" class="chat__loading">
        <div v-for="i in 3" :key="i" class="chat__sk" :style="{ width: `${60 + i * 8}%` }" />
      </div>

      <AppEmpty
        v-else-if="!messages.length && !streamText"
        icon="💬"
        title="和主编聊聊你的想法"
        description="描述你想写什么，或者直接让他起草下一章。所有对话都会自动保存。"
        size="md"
      >
        <template #action>
          <div class="quick">
            <button
              v-for="q in QUICK"
              :key="q"
              class="quick__btn"
              type="button"
              @click="draft = q; send()"
            >{{ q }}</button>
          </div>
        </template>
      </AppEmpty>

      <template v-else>
        <article
          v-for="(m, i) in messages"
          :key="m.id || i"
          class="msg"
          :class="`msg--${m.role}`"
        >
          <div class="msg__avatar" aria-hidden="true">{{ m.role === 'user' ? '我' : '编' }}</div>
          <div class="msg__bubble">
            <div class="msg__meta">
              <span class="msg__role">{{ roleLabel(m.role) }}</span>
              <span v-if="m.created_at" class="muted tiny">{{ m.created_at.slice(11, 16) }}</span>
            </div>
            <div class="msg__text">{{ m.content }}</div>
          </div>
        </article>

        <!-- ★ 流式增量（打字机效果） -->
        <article v-if="streamText" class="msg msg--assistant">
          <div class="msg__avatar" aria-hidden="true">编</div>
          <div class="msg__bubble">
            <div class="msg__meta"><span class="msg__role">主编</span></div>
            <div class="msg__text">{{ streamText }}<span class="caret" aria-hidden="true" /></div>
          </div>
        </article>

        <!-- ★ #61 agent 调用/过程活动流：实时显示「正在读第 N 章」「已记录人物…」 -->
        <details v-if="actions.length" class="chat__acts">
          <summary class="chat__acts-sum">
            <span class="chat__acts-ico" aria-hidden="true">⚙</span>
            过程<template v-if="streaming">（进行中 · {{ actions.length }} 步）</template>
            <template v-else>（已完成 · {{ actions.length }} 步）</template>
          </summary>
          <ul class="chat__acts-list">
            <li v-for="(a, i) in actions" :key="i" class="chat__acts-item">
              <span class="chat__acts-icn" aria-hidden="true">{{ a.icon }}</span>
              <span class="chat__acts-txt">{{ a.text }}</span>
            </li>
          </ul>
        </details>
      </template>

      <!-- 停止生成的悬浮按钮 -->
      <div v-if="streaming" class="chat__stop">
        <AppButton size="sm" variant="ghost" @click="emit('stop')">■ 停止生成</AppButton>
      </div>

      <!-- ★ 上滑后出现的"回到最新"悬浮按钮（v0.27.2） -->
      <transition name="jump-fade">
        <button
          v-if="showJump"
          type="button"
          class="chat__jump"
          aria-label="跳到最新消息"
          @click="jumpToBottom"
        >
          <span aria-hidden="true">↓</span> 回到最新
        </button>
      </transition>
    </div>

    <!-- 输入区 -->
    <footer class="chat__foot">
      <div class="chat__input-wrap">
        <textarea
          v-model="draft"
          class="chat__input"
          rows="2"
          placeholder="说点什么…（⌘/Ctrl + Enter 发送）"
          :disabled="disabled"
          aria-label="消息输入框"
          @keydown="onKeydown"
        />
        <div class="chat__actions">
          <span class="muted tiny">{{ draft.length }} 字</span>
          <AppButton
            variant="primary"
            size="sm"
            :disabled="!canSend"
            :loading="streaming"
            @click="send"
          >发送</AppButton>
        </div>
      </div>
      <p class="muted tiny chat__tip">
        ★ 长任务会在后台运行，可以切到别的页面，回来后自动续上
      </p>
    </footer>
  </section>
</template>

<style scoped>
.chat { display: flex; flex-direction: column; min-height: 0; background: var(--bg); }

.chat__head {
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
  border-bottom: 1px solid var(--border);
  background: var(--bg-elevated);
}
.chat__who { display: flex; align-items: center; gap: var(--sp-2); font-size: var(--fs-sm); }
.chat__avatar {
  width: 28px; height: 28px; flex-shrink: 0;
  display: grid; place-items: center;
  border-radius: 50%; background: var(--brand-500); color: #fff;
  font-size: var(--fs-xs); font-weight: 700;
}

.chat__body {
  flex: 1 1 auto; min-height: 0; overflow-y: auto;
  padding: var(--sp-4);
  display: flex; flex-direction: column; gap: var(--sp-4);
  scroll-behavior: smooth;
}

.chat__loading { display: flex; flex-direction: column; gap: var(--sp-3); }
.chat__sk {
  height: 14px; border-radius: var(--r-sm);
  background: linear-gradient(90deg, var(--gray-200) 25%, var(--gray-100) 37%, var(--gray-200) 63%);
  background-size: 400% 100%; animation: shimmer 1.4s ease-in-out infinite;
}
@keyframes shimmer { 0% { background-position: 100% 50%; } 100% { background-position: 0 50%; } }

/* 消息 */
.msg { display: flex; gap: var(--sp-3); max-width: 100%; }
.msg--user { flex-direction: row-reverse; }
.msg__avatar {
  width: 28px; height: 28px; flex-shrink: 0;
  display: grid; place-items: center;
  border-radius: 50%; font-size: var(--fs-xs); font-weight: 700;
  background: var(--gray-200); color: var(--text-muted);
}
.msg--assistant .msg__avatar { background: var(--brand-500); color: #fff; }
.msg--user .msg__avatar { background: var(--accent); color: #fff; }

.msg__bubble {
  max-width: min(76ch, 84%);
  padding: var(--sp-3) var(--sp-4);
  border-radius: var(--r-lg);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
}
.msg--user .msg__bubble { background: var(--brand-50); border-color: var(--brand-200); }
[data-theme="dark"] .msg--user .msg__bubble { background: var(--brand-50); }

.msg__meta { display: flex; align-items: baseline; gap: var(--sp-2); margin-bottom: 4px; }
.msg__role { font-size: var(--fs-xs); font-weight: var(--fw-bold); color: var(--text-muted); }
.msg__text {
  font-size: var(--fs-sm); line-height: var(--lh-relaxed);
  white-space: pre-wrap; word-break: break-word;
}

.caret {
  display: inline-block; width: 7px; height: 15px;
  margin-left: 2px; vertical-align: text-bottom;
  background: var(--brand-500);
  animation: blink 1s steps(2) infinite;
}
@keyframes blink { 50% { opacity: 0; } }

.chat__stop { display: flex; justify-content: center; padding-bottom: var(--sp-2); }

/* ★ #61 agent 调用/过程活动流 */
.chat__acts {
  margin-top: var(--sp-1);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--bg-sunken);
  overflow: hidden;
}
.chat__acts-sum {
  display: flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--fs-xs); font-weight: var(--fw-medium);
  color: var(--text-muted); cursor: pointer; list-style: none;
}
.chat__acts-sum::-webkit-details-marker { display: none; }
.chat__acts-sum:hover { background: var(--bg-hover); }
.chat__acts-ico { font-size: 13px; }
.chat__acts-list {
  margin: 0; padding: 0 var(--sp-2) var(--sp-2); list-style: none;
  display: flex; flex-direction: column; gap: 2px;
  max-height: 180px; overflow-y: auto;
}
.chat__acts-item { display: flex; align-items: flex-start; gap: var(--sp-2); }
.chat__acts-icn { flex-shrink: 0; font-size: 12px; line-height: 1.4; }
.chat__acts-txt { font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.4; }
.chat__acts-item:last-child .chat__acts-txt { color: var(--text); }

/* ★ 回到最新（上滑后出现的悬浮按钮，v0.27.2） */
.chat__jump {
  position: sticky;
  bottom: 8px;
  align-self: flex-end;
  z-index: 5;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-top: var(--sp-2);
  padding: 6px 14px;
  border: 1px solid var(--brand-200);
  border-radius: var(--r-full);
  background: var(--brand-500);
  color: #fff;
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.18);
  cursor: pointer;
  user-select: none;
}
.chat__jump:hover { background: var(--brand-600); }
.chat__jump:active { transform: translateY(1px); }
.chat__jump span { font-size: 14px; line-height: 1; }

.jump-fade-enter-active, .jump-fade-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}
.jump-fade-enter-from, .jump-fade-leave-to {
  opacity: 0;
  transform: translateY(6px);
}

/* 快捷问题 */
.quick { display: flex; flex-wrap: wrap; gap: var(--sp-2); justify-content: center; }
.quick__btn {
  padding: 6px var(--sp-3);
  border: 1px solid var(--border); border-radius: var(--r-full);
  background: var(--bg-elevated); color: var(--text-muted);
  font-size: var(--fs-xs);
}
.quick__btn:hover { border-color: var(--brand-200); color: var(--brand-600); background: var(--brand-50); }

/* 输入 */
.chat__foot {
  padding: var(--sp-3) var(--sp-4);
  border-top: 1px solid var(--border);
  background: var(--bg-elevated);
}
.chat__input-wrap {
  border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--bg);
  transition: border-color var(--dur-fast) var(--ease), box-shadow var(--dur-fast) var(--ease);
}
.chat__input-wrap:focus-within { border-color: var(--brand-500); box-shadow: var(--focus-ring); }
.chat__input {
  width: 100%; padding: var(--sp-3);
  border: none; outline: none; background: transparent; resize: none;
  font-family: inherit; font-size: var(--fs-sm); line-height: var(--lh-normal);
  color: var(--text); max-height: 200px;
}
.chat__actions {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 var(--sp-3) var(--sp-2);
}
.chat__tip { margin-top: var(--sp-2); text-align: center; }
</style>
