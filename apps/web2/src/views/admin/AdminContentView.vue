<script setup lang="ts">
/**
 * AdminContentView.vue · 内容治理（v0.26.3 G0-6 续）
 *
 * ★ 对照旧版 RENDER.content 核对。
 *
 * ★★ 本页最重要的一条红线（MEMORY §30）：
 *   **管理员不可读用户正文** —— 本页只展示作品的统计量与治理状态，
 *   绝不提供"查看全文"入口。有测试守卫这条约束。
 *
 * ★ 治理操作（隐藏/恢复）必须填原因 —— 影响面是"用户的作品消失"。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppPage from '@/components/AdminPage.vue'
import AppCard from '@/components/AppCard.vue'
import AppTag from '@/components/AppTag.vue'
import AppButton from '@/components/AppButton.vue'
import AppInput from '@/components/AppInput.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import AppTabs from '@/components/AppTabs.vue'
import AdminConfirm from '@/components/AdminConfirm.vue'
import { useUiStore } from '@/stores/ui'
import {
  contentApi, type AdminBookRow, type ContentFlagRow,
} from '@/api/admin_modules_extra'
import { fmtNum, fmtWords } from '@/api/admin_modules'

const route = useRoute()
const ui = useUiStore()

const PAGE_SIZE = 50

const tab = ref<'books' | 'flags'>('books')
const loading = ref(true)
const err = ref('')

const books = ref<AdminBookRow[]>([])
const bookTotal = ref(0)
const bookQ = ref({ keyword: '', content_status: '', offset: 0 })

const flags = ref<ContentFlagRow[]>([])
const flagQ = ref({ status: 'open', offset: 0 })

/** ★ 内容状态 → 展示（与后端 content_status 枚举对齐） */
const CS = {
  normal: { t: '正常', kind: 'success' as const },
  review: { t: '待复核', kind: 'warning' as const },
  hidden: { t: '已隐藏', kind: 'danger' as const },
  banned: { t: '已封禁', kind: 'danger' as const },
}

function csOf(s?: string) {
  return CS[(s || 'normal') as keyof typeof CS] || { t: s || '未知', kind: 'default' as const }
}

function sevTag(s?: string) {
  const v = String(s || '').toLowerCase()
  if (v === 'high' || v === 'critical') return 'danger' as const
  if (v === 'medium') return 'warning' as const
  return 'default' as const
}

/* ── 处置确认 ── */
interface PendingAct {
  kind: 'hide' | 'restore' | 'review'
  book?: AdminBookRow
  /** ★ v1.0-E4.13 标记处置：kind = open|hidden|cleared|banned（直接对应后端状态机） */
  flag?: ContentFlagRow
}
const pending = ref<PendingAct | null>(null)
const submitting = ref(false)

const ACT_CFG: Record<string, { title: string; body: string; ok: string; danger: boolean }> = {
  hide: {
    title: '隐藏作品',
    body: '隐藏后该作品对作者与读者均不可见（内容保留）。<br>'
      + '<b class="warn">请确认已完整核对违规事实</b> —— 误判会影响正常创作。',
    ok: '确认隐藏',
    danger: true,
  },
  restore: {
    title: '恢复作品',
    body: '恢复后作品重新对外可见。',
    ok: '确认恢复',
    danger: false,
  },
  review: {
    title: '标记待复核',
    body: '标记为「待复核」—— 作品仍可见，但会进入人工复核队列。',
    ok: '确认标记',
    danger: false,
  },
  // ★ v1.0-E4.13 标记处置（PATCH /api/admin/flags/{id}，联动书籍状态并写审计）
  hidden: {
    title: '按标记隐藏作品',
    body: '该标记关联的作品将被<b class="warn">隐藏</b>（作者与读者均不可见）。',
    ok: '确认隐藏',
    danger: true,
  },
  cleared: {
    title: '澄清标记',
    body: '判定为误报：标记关闭，关联作品<b>恢复可见</b>。',
    ok: '确认澄清',
    danger: false,
  },
  banned: {
    title: '封禁作者',
    body: '<b style="color:var(--danger,#d33)">作品隐藏 + 作者账号封禁</b>（令牌立即失效）。'
      + '<br>这是最重处置 —— 请确认已完整核对违规事实。',
    ok: '确认封禁',
    danger: true,
  },
  open: {
    title: '重开标记',
    body: '标记回到「待处理」状态，关联作品状态不变。',
    ok: '确认重开',
    danger: false,
  },
}

/** 标记处置按钮（只对待处理 open 的标记提供） */
const FLAG_OPS: Array<{ kind: string; label: string }> = [
  { kind: 'cleared', label: '澄清' },
  { kind: 'hidden', label: '隐藏作品' },
  { kind: 'banned', label: '封禁作者' },
]

const confirmOpen = computed({
  get: () => pending.value !== null,
  set: (v: boolean) => { if (!v) pending.value = null },
})
const curCfg = computed(() => (pending.value ? ACT_CFG[pending.value.kind] : ACT_CFG.review))
async function loadBooks() {
  loading.value = true
  err.value = ''
  try {
    const d = await contentApi.books({ ...bookQ.value, limit: PAGE_SIZE })
    books.value = d.items || []
    bookTotal.value = Number(d.total || 0)
  } catch (e) {
    books.value = []
    bookTotal.value = 0
    err.value = (e as Error).message || '加载作品列表失败'
  } finally {
    loading.value = false
  }
}

async function loadFlags() {
  loading.value = true
  err.value = ''
  try {
    const d = await contentApi.flags({ ...flagQ.value, limit: PAGE_SIZE })
    flags.value = d.items || []
  } catch (e) {
    flags.value = []
    err.value = (e as Error).message || '加载标记列表失败'
  } finally {
    loading.value = false
  }
}

function reload() {
  return tab.value === 'books' ? loadBooks() : loadFlags()
}

function searchBooks() {
  bookQ.value = { ...bookQ.value, offset: 0 }
  loadBooks()
}

function ask(kind: PendingAct['kind'], book: AdminBookRow) {
  pending.value = { kind, book }
}

/** ★ v1.0-E4.13 标记处置入口 */
function askFlag(kind: string, flag: ContentFlagRow) {
  pending.value = { kind: kind as PendingAct['kind'], flag }
}

async function doConfirm(reason: string) {
  const p = pending.value
  if (!p) return
  submitting.value = true
  try {
    if (p.flag) {
      // 标记处置（后端联动书籍状态 + 写审计）
      await contentApi.resolveFlag(p.flag.id, p.kind, reason)
    } else if (p.book) {
      const status = p.kind === 'hide' ? 'hidden'
        : p.kind === 'restore' ? 'normal' : 'review'
      await contentApi.setContentStatus(p.book.id, status, reason)
    }
    ui.ok('操作成功')
    pending.value = null
    await reload()
  } catch (e) {
    ui.err((e as Error).message || '操作失败')
  } finally {
    submitting.value = false
  }
}

function switchTab(t: string) {
  tab.value = t as 'books' | 'flags'
  reload()
}

onMounted(loadBooks)
watch(() => route.query._r, () => { reload() })
</script>

<template>
  <AppPage
    title="内容治理"
    subtitle="★ 管理员不可读取作品正文（有测试守卫）。本页只有统计量与治理状态。"
    :loading="loading"
    :error="err"
    empty-icon="▤"
    empty-title="没有匹配的内容"
    empty-desc="换个关键词或状态试试。"
    :columns="tab === 'books'
      ? [
        { key: 'title', label: '作品' },
        { key: 'owner', label: '作者', width: '130px' },
        { key: 'stat', label: '规模', width: '130px' },
        { key: 'content_status', label: '状态', width: '90px' },
        { key: 'created_at', label: '创建时间', width: '150px', hideSm: true },
      ]
      : [
        { key: 'title', label: '类型', width: '120px' },
        { key: 'owner', label: '严重度', width: '90px' },
        { key: 'stat', label: '说明' },
        { key: 'content_status', label: '状态', width: '90px' },
        { key: 'created_at', label: '时间', width: '150px', hideSm: true },
      ]"
    :rows="(tab === 'books' ? books : flags) as unknown as Array<Record<string, unknown>>"
    :total="tab === 'books' ? bookTotal : flags.length"
    :offset="tab === 'books' ? bookQ.offset : 0"
    :page-size="PAGE_SIZE"
    @page="(o: number) => { bookQ.offset = o; loadBooks() }"
  >
    <template #actions>
      <AppButton size="sm" variant="ghost" @click="reload">刷新</AppButton>
    </template>

    <template #toolbar>
      <AppTabs
        :model-value="tab"
        :items="[
          { key: 'books', label: '作品列表', badge: tab === 'books' ? bookTotal : undefined },
          { key: 'flags', label: '内容标记', badge: tab === 'flags' ? flags.length : undefined },
        ]"
        @update:model-value="switchTab"
      />
      <template v-if="tab === 'books'">
        <AppInput
          v-model="bookQ.keyword"
          placeholder="搜索标题 / 作者"
          clearable
          style="max-width: 220px"
          @keyup.enter="searchBooks"
        />
        <select v-model="bookQ.content_status" class="sel" @change="searchBooks">
          <option value="">全部状态</option>
          <option v-for="(v, k) in CS" :key="k" :value="k">{{ v.t }}</option>
        </select>
        <AppButton size="sm" variant="primary" @click="searchBooks">搜索</AppButton>
      </template>
      <template v-else>
        <select v-model="flagQ.status" class="sel" @change="loadFlags">
          <option value="">全部</option>
          <option value="open">待处理</option>
          <option value="handled">已处理</option>
          <option value="ignored">已忽略</option>
        </select>
      </template>
      <span class="sp" />
      <span class="count">
        {{ tab === 'books' ? `共 ${fmtNum(bookTotal)} 部作品` : `${flags.length} 条标记` }}
      </span>
    </template>

    <!-- ═══ 单元格插槽 ═══
         ★ 重要：具名插槽模板**必须**是 AppPage 的直接子节点，
           不能被 `<template v-if>` 包裹 —— 否则 Vue 模板编译报
           "Cannot read properties of undefined (reading 'type')"。
           因此这里把条件判断放进插槽内容里，而不是包在插槽外面。 -->
    <template #cell-title="{ row }">
      <template v-if="tab === 'books'">
        <b>{{ row.title }}</b>
        <div class="dim mono">{{ String(row.id).slice(0, 18) }}</div>
      </template>
      <AppTag v-else size="sm">{{ row.category || '—' }}</AppTag>
    </template>

    <template #cell-owner="{ row }">
      <template v-if="tab === 'books'">
        {{ row.owner_name || String(row.owner_id || '').slice(0, 12) }}
      </template>
      <AppTag v-else size="sm" :kind="sevTag(row.severity as string)">
        {{ row.severity || '—' }}
      </AppTag>
    </template>

    <template #cell-stat="{ row }">
      <template v-if="tab === 'books'">
        <span class="mono">{{ fmtNum(row.chapters as number) }} 章</span>
        <div class="dim">{{ fmtWords(row.words as number) }} 字</div>
      </template>
      <span v-else class="note">{{ row.note || '—' }}</span>
    </template>

    <template #cell-content_status="{ row }">
      <template v-if="tab === 'books'">
        <AppTag size="sm" :kind="csOf(row.content_status as string).kind">
          {{ csOf(row.content_status as string).t }}
        </AppTag>
      </template>
      <AppTag v-else size="sm" :kind="row.status === 'open' ? 'warning' : 'default'">
        {{ row.status === 'open' ? '待处理' : row.status === 'handled' ? '已处理' : row.status || '—' }}
      </AppTag>
    </template>

    <template #cell-created_at="{ row }">
      <span class="mono dim">{{ row.created_at || '—' }}</span>
    </template>

    <!-- ★ rowops 只在作品列表有意义（标记列表是只读的） -->
    <template #rowops="{ row }">
      <template v-if="tab === 'books'">
        <button
          v-if="row.content_status === 'hidden' || row.content_status === 'banned'"
          class="op" @click="ask('restore', row as unknown as AdminBookRow)"
        >恢复</button>
        <template v-else>
          <button class="op" @click="ask('review', row as unknown as AdminBookRow)">待复核</button>
          <button class="op danger" @click="ask('hide', row as unknown as AdminBookRow)">隐藏</button>
        </template>
      </template>
      <template v-else-if="row.status === 'open'">
        <!-- ★ v1.0-E4.13 审核队列处置（联动书籍状态 + 写审计） -->
        <button
          v-for="op in FLAG_OPS" :key="op.kind" class="op"
          :class="{ danger: op.kind === 'banned' }"
          @click="askFlag(op.kind, row as unknown as ContentFlagRow)"
        >{{ op.label }}</button>
      </template>
      <span v-else class="dim">
        {{ row.handled_by ? `已处理 · ${row.handled_by}` : row.status }}
      </span>
    </template>

    <template #hint>
      ★ 本页**只读作品统计量，不提供正文入口** —— 这是产品红线（管理员不可读用户创作内容）。
      自动判定会产生误报，因此所有处置都走**人工确认**，且人工结果不会被重算覆盖。
    </template>
  </AppPage>

  <AdminConfirm
    v-model="confirmOpen"
    :title="curCfg.title"
    :body="curCfg.body"
    :ok-text="curCfg.ok"
    :danger="curCfg.danger"
    :loading="submitting"
    reason-placeholder="请填写处置原因（会写入审计日志，也是申诉依据）"
    @confirm="doConfirm"
  />
</template>

<style scoped>
.sel {
  padding: 6px 10px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); color: var(--text); font-size: 12px; font-family: inherit;
}
.sp { flex: 1; }
.count { font-size: 11px; color: var(--text-faint); }
.dim { color: var(--text-faint); font-size: 11px; }
.mono { font-variant-numeric: tabular-nums; font-size: 11px; }
.note { font-size: 12px; line-height: 1.65; }
.op {
  font-size: 11px; padding: 3px 8px; margin-right: 4px;
  border: 1px solid var(--border); border-radius: 5px;
  background: var(--surface); color: var(--text-muted);
  cursor: pointer; font-family: inherit;
}
.op:hover { color: var(--text); border-color: var(--brand); }
.op.danger:hover {
  color: var(--danger, #d33);
  border-color: color-mix(in srgb, var(--danger, #d33) 50%, transparent);
}
.warn { color: var(--warn, #b8860b); }
</style>
