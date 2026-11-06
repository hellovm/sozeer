<script setup lang="ts">
/**
 * AdminPage.vue · 后台页面通用外壳（v0.26.3）
 *
 * ★ 为什么需要它：10 个待迁移页面有大量重复结构（标题栏 + 工具条 + 卡片 +
 *   表格 + 空态 + 加载态 + 错误态 + 分页）。抽成一个组件后：
 *   ① 每页代码量减少 ~40%
 *   ② **错误处理/空态/加载态自动一致**（手工写必然有页面漏掉某个态）
 *   ③ 改一次样式，10 页同时生效
 *
 * ★ 设计取舍：不用 render props / 复杂 slot 协议 —— 保持"传数组 + 具名插槽"，
 *   心智负担最低，出错最少。
 */
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppEmpty from '@/components/AppEmpty.vue'

interface Column {
  key: string
  label: string
  /** 右对齐（数值列） */
  num?: boolean
  /** 固定宽度 */
  width?: string
  /** 窄屏隐藏（次要列） */
  hideSm?: boolean
}

withDefaults(defineProps<{
  title?: string
  subtitle?: string
  loading?: boolean
  error?: string
  /** 空态（无数据时展示） */
  emptyIcon?: string
  emptyTitle?: string
  emptyDesc?: string
  /** 表格列定义（传了才渲染表格） */
  columns?: Column[]
  /** 行数据（与 columns 配合） */
  rows?: Array<Record<string, unknown>>
  /** 行主键字段 */
  rowKey?: string
  /** 分页 */
  total?: number
  offset?: number
  pageSize?: number
  /** 表格骨架屏行数 */
  skeletonCount?: number
}>(), {
  title: '',
  subtitle: '',
  loading: false,
  error: '',
  emptyIcon: '▤',
  emptyTitle: '暂无数据',
  emptyDesc: '',
  columns: () => [],
  rows: () => [],
  rowKey: 'id',
  total: 0,
  offset: 0,
  pageSize: 50,
  skeletonCount: 8,
})

const emit = defineEmits<{ page: [offset: number] }>()
</script>

<template>
  <div class="ap">
    <!-- ── 标题栏 ── -->
    <header v-if="title || $slots.actions" class="ap__head">
      <div class="ap__titles">
        <h1 v-if="title" class="ap__title">{{ title }}</h1>
        <p v-if="subtitle" class="ap__sub">{{ subtitle }}</p>
      </div>
      <div class="ap__actions">
        <slot name="actions" />
      </div>
    </header>

    <!-- ── 工具条（筛选/搜索） ── -->
    <div v-if="$slots.toolbar" class="ap__toolbar">
      <slot name="toolbar" />
    </div>

    <!-- ── 汇总卡 ── -->
    <div v-if="$slots.cards" class="ap__cards">
      <slot name="cards" />
    </div>

    <!-- ── 错误 ── -->
    <p v-if="error" class="ap__err">{{ error }}</p>

    <!-- ── 主体 ── -->
    <AppSkeleton v-if="loading" variant="table" :count="skeletonCount" class="ap__mt" />

    <template v-else>
      <!-- 表格模式 -->
      <AppCard v-if="columns.length" class="ap__mt">
        <AppEmpty
          v-if="!rows.length"
          :icon="emptyIcon"
          :title="emptyTitle"
          :description="emptyDesc"
        />
        <div v-else class="ap__tblwrap">
          <table class="ap__tbl">
            <thead>
              <tr>
                <th
                  v-for="c in columns"
                  :key="c.key"
                  :class="{ num: c.num, hidesm: c.hideSm }"
                  :style="c.width ? { width: c.width } : undefined"
                >{{ c.label }}</th>
                <th v-if="$slots.rowops" class="ops">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(r, i) in rows" :key="String(r[rowKey] ?? i)">
                <td
                  v-for="c in columns"
                  :key="c.key"
                  :class="{ num: c.num, hidesm: c.hideSm }"
                >
                  <slot :name="`cell-${c.key}`" :row="r" :value="r[c.key]">
                    {{ r[c.key] ?? '—' }}
                  </slot>
                </td>
                <td v-if="$slots.rowops" class="ops">
                  <slot name="rowops" :row="r" />
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 分页 -->
        <div v-if="rows.length && total > pageSize" class="ap__pager">
          <span class="ap__dim">
            第 {{ Math.floor(offset / pageSize) + 1 }} /
            {{ Math.max(1, Math.ceil(total / pageSize)) }} 页 · 共 {{ total }} 条
          </span>
          <AppButton
            size="sm" variant="ghost"
            :disabled="offset <= 0"
            @click="emit('page', Math.max(0, offset - pageSize))"
          >上一页</AppButton>
          <AppButton
            size="sm" variant="ghost"
            :disabled="offset + pageSize >= total"
            @click="emit('page', offset + pageSize)"
          >下一页</AppButton>
        </div>
      </AppCard>

      <!-- 自由内容模式 -->
      <slot v-else />
    </template>

    <p v-if="$slots.hint" class="ap__hint"><slot name="hint" /></p>
  </div>
</template>

<style scoped>
.ap { display: flex; flex-direction: column; }

.ap__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.ap__title { font-size: 16px; font-weight: 600; margin: 0 0 4px; }
.ap__sub { font-size: 12px; color: var(--text-muted); margin: 0; line-height: 1.7; max-width: 720px; }
.ap__actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }

.ap__toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 11px 13px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
}

.ap__cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(184px, 1fr));
  gap: 12px;
  margin-top: 12px;
}

.ap__err {
  margin: 12px 0 0;
  padding: 9px 12px;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--danger, #d33);
  border: 1px solid color-mix(in srgb, var(--danger, #d33) 30%, transparent);
  background: color-mix(in srgb, var(--danger, #d33) 8%, transparent);
}

.ap__mt { margin-top: 12px; }

.ap__tblwrap { overflow-x: auto; }
.ap__tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
.ap__tbl th {
  text-align: left;
  font-weight: 500;
  font-size: 11px;
  color: var(--text-faint);
  padding: 7px 8px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.ap__tbl td {
  padding: 8px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
.ap__tbl tr:last-child td { border-bottom: none; }
.ap__tbl .num { text-align: right; font-variant-numeric: tabular-nums; }
.ap__tbl .ops { white-space: nowrap; vertical-align: middle; }

.ap__pager { display: flex; align-items: center; gap: 10px; padding-top: 12px; }
.ap__dim { font-size: 11px; color: var(--text-faint); }
.ap__hint { font-size: 11px; color: var(--text-faint); margin: 12px 0 0; line-height: 1.8; }
.ap__hint :deep(code) {
  font-size: 11px; padding: 1px 5px; border-radius: 4px;
  border: 1px solid var(--border);
}

@media (max-width: 720px) {
  .ap__tbl .hidesm { display: none; }
}
</style>
