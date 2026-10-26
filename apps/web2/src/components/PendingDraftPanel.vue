<script setup lang="ts">
/**
 * PendingDraftPanel · 待审草稿区（写作台）
 *
 * ★ 背景（v0.14.10 bug 实录）：草稿卡原本只活在聊天流气泡里，
 *   刷新 / 切对话 / 聊天气泡滚走后，用户就找不到草稿入口，
 *   只能看到主编说「已起草第 N 章」，但页面上看不到草稿。
 *
 * 这里把待审稿做成一等公民：后端 GET /pending-drafts 扫全部代笔任务，
 *   列出「比正文新」的待采纳稿，常驻展示，支持：
 *     · 采纳入库（POST /accept，带 task_id 消费草稿）
 *     · 在编辑器修改（跳章节编辑器并预填草稿）
 *     · 丢弃（DELETE /tasks/{id}/draft，清正文打 discarded 标记，可恢复）
 *
 * ★ 文案对齐旧版 apps/web/app.js 的 renderPendingDrafts（同一批接口）。
 */
import type { PendingDraft } from '@/api/modules/book'
import AppButton from '@/components/AppButton.vue'
import AppTag from '@/components/AppTag.vue'
import AppEmpty from '@/components/AppEmpty.vue'

const props = withDefaults(defineProps<{
  drafts: PendingDraft[]
  loading?: boolean
  /** ★ 正在执行采纳/丢弃的草稿 task_id（同一时间只允许一个变更操作，防重复点击） */
  busyTaskId?: string
  /** 嵌入到其它容器（如章节树底部）时隐藏自带头部，避免标题重复 */
  hideHeader?: boolean
}>(), {
  loading: false,
  busyTaskId: '',
  hideHeader: false,
})

const emit = defineEmits<{
  accept: [draft: PendingDraft]
  edit: [draft: PendingDraft]
  discard: [draft: PendingDraft]
}>()

function isBusy(d: PendingDraft) { return d.task_id === props.busyTaskId }

/** 取正文用于展示 / 提交（★ 去掉开头的 `# 第 N 章` 标题行，与后端 accept_draft 对齐） */
function bodyOf(d: PendingDraft): string {
  const t = String(d.text || '')
  const l = t.split('\n')
  return (l[0] && /^#\s*第\s*\d+\s*章/.test(l[0]))
    ? l.slice(1).join('\n').trim()
    : t.trim()
}

/** 预览（最多 160 字，去标题行） */
function previewOf(d: PendingDraft): string {
  const b = bodyOf(d)
  return b.length > 160 ? b.slice(0, 160) + '…' : b
}

/** 标题（后端已给 d.title；兜底从正文解析） */
function titleOf(d: PendingDraft): string {
  if (d.title) return d.title
  const m = String(d.text || '').match(/^#\s*第\s*\d+\s*章\s*(.*)$/m)
  return m ? m[1].trim() : ''
}

/** 质检分 → 配色档（与章节编辑器一致） */
function scoreKind(s?: number | null) {
  if (s == null) return 'default' as const
  if (s >= 90) return 'success' as const
  if (s >= 75) return 'info' as const
  if (s >= 60) return 'warning' as const
  return 'danger' as const
}
</script>

<template>
  <section class="pd" aria-label="待审草稿">
    <header v-if="!hideHeader" class="pd__head">
      <div class="pd__title">
        <span class="pd__icon" aria-hidden="true">📝</span>
        <b>待审草稿<template v-if="drafts.length">（{{ drafts.length }}）</template></b>
      </div>
      <span class="pd__hint">代笔稿在这里等采纳——聊天流里找不到时，来这。</span>
    </header>

    <!-- 加载骨架 -->
    <div v-if="loading && !drafts.length" class="pd__sk">
      <div v-for="i in 2" :key="i" class="pd__sk-item" />
    </div>

    <!-- 空态 -->
    <AppEmpty
      v-else-if="!drafts.length"
      icon="✓"
      title="没有待采纳的草稿"
      description="让主编起草章节后，草稿会出现在这里等你审核。"
      size="sm"
    />

    <!-- 草稿卡列表 -->
    <ul v-else class="pd__list">
      <li
        v-for="d in drafts"
        :key="d.task_id"
        class="pd__card"
        :class="{ 'pd__card--newer': d.newer }"
      >
        <div class="pd__c-head">
          <span class="pd__c-ch mono">第 {{ d.ch_idx }} 章</span>
          <span class="pd__c-title truncate">{{ titleOf(d) || '（无题）' }}</span>
          <AppTag
            v-if="d.gate_score != null"
            size="sm"
            :kind="scoreKind(d.gate_score)"
            round
          >质检 {{ d.gate_score }}</AppTag>
          <AppTag v-if="d.newer" size="sm" kind="success" round>比正文新</AppTag>
        </div>

        <div class="pd__c-body">{{ previewOf(d) }}</div>

        <div class="pd__c-foot">
          <span class="muted tiny">{{ (d.words || 0).toLocaleString('zh-CN') }} 字</span>
          <span class="spacer" />
          <div class="pd__c-actions">
            <AppButton
              size="sm"
              variant="primary"
              :loading="isBusy(d)"
              :disabled="isBusy(d)"
              @click="emit('accept', d)"
            >采纳入库</AppButton>
            <AppButton
              size="sm"
              variant="ghost"
              :disabled="isBusy(d)"
              @click="emit('edit', d)"
            >在编辑器修改</AppButton>
            <AppButton
              size="sm"
              variant="text"
              :disabled="isBusy(d)"
              title="不要这版了，从待审区移除（任务历史保留，误删可恢复）"
              @click="emit('discard', d)"
            >丢弃</AppButton>
          </div>
        </div>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.pd {
  display: flex; flex-direction: column;
  min-height: 0;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
}
.pd__head {
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-3); flex-wrap: wrap;
  padding: var(--sp-3) var(--sp-3) var(--sp-2);
  border-bottom: 1px solid var(--border);
}
.pd__title { display: flex; align-items: center; gap: var(--sp-2); font-size: var(--fs-sm); }
.pd__icon { font-size: 15px; }
.pd__hint { font-size: var(--fs-xs); color: var(--text-subtle); }

.pd__sk { display: flex; flex-direction: column; gap: var(--sp-3); padding: var(--sp-3); }
.pd__sk-item {
  height: 64px; border-radius: var(--r-md);
  background: linear-gradient(90deg, var(--gray-200) 25%, var(--gray-100) 37%, var(--gray-200) 63%);
  background-size: 400% 100%; animation: shimmer 1.4s ease-in-out infinite;
}
@keyframes shimmer { 0% { background-position: 100% 50%; } 100% { background-position: 0 50%; } }

.pd__list {
  list-style: none; margin: 0; padding: var(--sp-2);
  display: flex; flex-direction: column; gap: var(--sp-2);
  overflow-y: auto; flex: 1 1 auto; min-height: 0;
}

.pd__card {
  display: flex; flex-direction: column; gap: var(--sp-2);
  padding: var(--sp-3);
  border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--bg);
  position: relative;
}
.pd__card--newer { border-color: var(--brand-200); background: var(--brand-50); }
[data-theme="dark"] .pd__card--newer { background: var(--brand-50); }

.pd__c-head { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.pd__c-ch { font-size: var(--fs-xs); font-weight: var(--fw-bold); color: var(--text); flex-shrink: 0; }
.pd__c-title { font-size: var(--fs-xs); color: var(--text-muted); flex: 1 1 auto; min-width: 60px; }

.pd__c-body {
  font-size: var(--fs-xs); color: var(--text-muted); line-height: var(--lh-normal);
  display: -webkit-box; -webkit-line-clamp: 3; line-clamp: 3;
  -webkit-box-orient: vertical; overflow: hidden;
}

.pd__c-foot {
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-2); flex-wrap: wrap;
}
.pd__c-actions { display: flex; align-items: center; gap: var(--sp-1); flex-wrap: wrap; }

.spacer { flex: 1 1 auto; }
</style>
