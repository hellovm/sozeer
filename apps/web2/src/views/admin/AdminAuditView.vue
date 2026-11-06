<script setup lang="ts">
/**
 * AdminAuditView.vue · 审计日志（v0.26.3 G0-6 续）
 *
 * ★ 对照旧版 RENDER.audit 核对。
 *
 * ★★ 审计日志的三条产品要求（MEMORY §30）：
 *   ① **append-only**：不可编辑、不可删除（本页**只读**）
 *   ② **失败也留痕**：`result` 字段区分 success/failed，失败的也要显示
 *   ③ **原因可见**：`reason` 必须展示 —— 没有原因的审计等于没审计
 *   → 因此本页**没有任何操作按钮**，这是刻意的。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppPage from '@/components/AdminPage.vue'
import AppTag from '@/components/AppTag.vue'
import AppButton from '@/components/AppButton.vue'
import AppInput from '@/components/AppInput.vue'
import AppModal from '@/components/AppModal.vue'
import { useUiStore } from '@/stores/ui'
import { auditApi, type AuditRow } from '@/api/admin_modules_extra'

const route = useRoute()
const ui = useUiStore()

const PAGE_SIZE = 50

/** ★ 常用动作快捷筛选（比让管理员手输 action 字符串友好得多） */
const QUICK_ACTIONS = [
  { v: '', t: '全部' },
  { v: 'ads.', t: '广告' },
  { v: 'model.', t: '模型' },
  { v: 'config.', t: '配置' },
  { v: 'user', t: '用户' },
  { v: 'rbac', t: '权限' },
  { v: 'invoice', t: '发票' },
  { v: 'queue', t: '队列' },
]

const loading = ref(true)
const rows = ref<AuditRow[]>([])
const total = ref(0)
const err = ref('')
const q = ref({ action: '', operator_id: '', target_id: '', offset: 0 })
/** 详情弹窗（detail 是 JSON 字符串，表格里放不下） */
const detail = ref<AuditRow | null>(null)

const detailOpen = computed({
  get: () => detail.value !== null,
  set: (v: boolean) => { if (!v) detail.value = null },
})

/** ★ detail 是 JSON 字符串 —— 格式化展示，解析失败原样显示（便于排障） */
const detailPretty = computed(() => {
  const raw = detail.value?.detail
  if (!raw) return '（无）'
  try { return JSON.stringify(JSON.parse(raw), null, 2) } catch { return raw }
})

function resultTag(r: string) {
  const v = String(r || '').toLowerCase()
  if (v === 'ok' || v === 'success') return 'success' as const
  if (v === 'failed' || v === 'error') return 'danger' as const
  return 'default' as const
}

function resultText(r: string) {
  const v = String(r || '').toLowerCase()
  if (v === 'ok' || v === 'success') return '成功'
  if (v === 'failed' || v === 'error') return '失败'
  return r || '—'
}

async function load() {
  loading.value = true
  err.value = ''
  try {
    const d = await auditApi.list({ ...q.value, limit: PAGE_SIZE })
    rows.value = d.items || []
    total.value = Number(d.total || 0)
  } catch (e) {
    rows.value = []
    total.value = 0
    err.value = (e as Error).message || '加载审计日志失败'
  } finally {
    loading.value = false
  }
}

function search() {
  q.value = { ...q.value, offset: 0 }
  load()
}

function setAction(v: string) {
  q.value = { ...q.value, action: v, offset: 0 }
  load()
}

function onPage(off: number) {
  q.value = { ...q.value, offset: off }
  load()
}

onMounted(load)
watch(() => route.query._r, () => { load() })
</script>

<template>
  <AppPage
    title="审计日志"
    subtitle="append-only，不可编辑、不可删除。敏感操作（含失败的）全部留痕。"
    :loading="loading"
    :error="err"
    empty-icon="▤"
    empty-title="没有匹配的审计记录"
    empty-desc="换个动作类型或清空筛选条件试试。"
    :columns="[
      { key: 'created_at', label: '时间', width: '150px' },
      { key: 'operator_name', label: '操作人', width: '130px' },
      { key: 'action', label: '动作', width: '170px' },
      { key: 'target', label: '对象', width: '180px', hideSm: true },
      { key: 'reason', label: '原因', hideSm: true },
      { key: 'ip', label: 'IP', width: '120px', hideSm: true },
      { key: 'result', label: '结果', width: '70px' },
    ]"
    :rows="rows as unknown as Array<Record<string, unknown>>"
    :total="total"
    :offset="q.offset"
    :page-size="PAGE_SIZE"
    @page="onPage"
  >
    <template #actions>
      <AppTag size="sm">共 {{ total }} 条</AppTag>
      <AppButton size="sm" variant="ghost" @click="load">刷新</AppButton>
    </template>

    <template #toolbar>
      <AppInput
        v-model="q.operator_id"
        placeholder="操作人 ID"
        clearable
        style="max-width: 190px"
        @keyup.enter="search"
      />
      <AppInput
        v-model="q.target_id"
        placeholder="对象 ID（如订单号/发票号）"
        clearable
        style="max-width: 230px"
        @keyup.enter="search"
      />
      <AppButton size="sm" variant="primary" @click="search">搜索</AppButton>
      <span class="sp" />
      <div class="quick">
        <button
          v-for="a in QUICK_ACTIONS"
          :key="a.v"
          class="quick__b"
          :class="{ on: q.action === a.v }"
          @click="setAction(a.v)"
        >{{ a.t }}</button>
      </div>
    </template>

    <!-- 时间 -->
    <template #cell-created_at="{ value }">
      <span class="mono">{{ value || '—' }}</span>
    </template>

    <!-- 操作人 -->
    <template #cell-operator_name="{ row }">
      {{ row.operator_name || row.operator_id || '—' }}
    </template>

    <!-- 动作 -->
    <template #cell-action="{ value }">
      <code class="act">{{ value }}</code>
    </template>

    <!-- 对象（类型 + ID，可点开详情） -->
    <template #cell-target="{ row }">
      <button
        v-if="row.detail"
        class="link"
        @click="detail = row as unknown as AuditRow"
      >
        {{ row.target_type || '—' }}
        <span class="mono dim">{{ row.target_id ? String(row.target_id).slice(0, 12) : '' }}</span>
        <span class="see">查看</span>
      </button>
      <span v-else class="dim">
        {{ row.target_type || '—' }}
        {{ row.target_id ? String(row.target_id).slice(0, 12) : '' }}
      </span>
    </template>

    <!-- 原因（★ 必须展示） -->
    <template #cell-reason="{ value }">
      <span v-if="value" class="reason">{{ value }}</span>
      <span v-else class="dim warn">未填原因</span>
    </template>

    <template #cell-ip="{ value }">
      <span class="mono dim">{{ value || '—' }}</span>
    </template>

    <template #cell-result="{ value }">
      <AppTag size="sm" :kind="resultTag(value as string)">
        {{ resultText(value as string) }}
      </AppTag>
    </template>

    <template #hint>
      ★ 本页为**只读**视图 —— 审计日志不可编辑、不可删除（append-only）。
      「未填原因」的记录出现在这里说明对应的写接口没有强制校验原因，应当修复。
    </template>
  </AppPage>

  <!-- ★ 详情弹窗：detail 是 JSON 字符串，格式化展示 -->
  <AppModal v-model="detailOpen" title="操作详情" width="620px">
    <div v-if="detail" class="dt">
      <div class="dt__row"><b>动作</b><code>{{ detail.action }}</code></div>
      <div class="dt__row"><b>操作人</b>{{ detail.operator_name || detail.operator_id || '—' }}</div>
      <div class="dt__row"><b>对象</b>{{ detail.target_type }} · {{ detail.target_id || '—' }}</div>
      <div class="dt__row"><b>原因</b>
        <span v-if="detail.reason">{{ detail.reason }}</span>
        <span v-else class="warn">未填</span>
      </div>
      <div class="dt__row"><b>结果</b>
        <AppTag size="sm" :kind="resultTag(detail.result)">{{ resultText(detail.result) }}</AppTag>
      </div>
      <div class="dt__row"><b>IP</b><span class="mono">{{ detail.ip || '—' }}</span></div>
      <div class="dt__row"><b>时间</b><span class="mono">{{ detail.created_at }}</span></div>
      <div class="dt__detail">
        <b>detail</b>
        <pre class="dt__pre">{{ detailPretty }}</pre>
      </div>
    </div>
  </AppModal>
</template>

<style scoped>
.sp { flex: 1; }
.quick { display: inline-flex; gap: 4px; flex-wrap: wrap; }
.quick__b {
  font-size: 11px; padding: 4px 9px; border: 1px solid var(--border);
  border-radius: 6px; background: var(--surface); color: var(--text-muted);
  cursor: pointer; font-family: inherit;
}
.quick__b.on { background: var(--brand); color: #fff; border-color: var(--brand); }
.quick__b:hover:not(.on) { color: var(--text); }

.mono { font-variant-numeric: tabular-nums; font-size: 11px; }
.dim { color: var(--text-faint); font-size: 11px; }
.act { font-size: 11px; padding: 1px 5px; border-radius: 4px;
  border: 1px solid var(--border); background: var(--surface-2, var(--surface)); }
.reason { font-size: 12px; line-height: 1.6; }
.warn { color: var(--warn, #b8860b); }
.link {
  background: none; border: none; padding: 0; cursor: pointer;
  font-family: inherit; font-size: 12px; color: var(--text);
  text-align: left; display: flex; align-items: center; gap: 5px;
}
.link:hover { color: var(--brand); }
.see { font-size: 10px; color: var(--brand); text-decoration: underline; }

.dt { display: flex; flex-direction: column; gap: 9px; font-size: 12px; }
.dt__row { display: flex; gap: 10px; align-items: baseline; line-height: 1.7; }
.dt__row b { flex: none; width: 62px; color: var(--text-faint); font-weight: 500; }
.dt__row code { font-size: 11px; padding: 1px 5px; border-radius: 4px; border: 1px solid var(--border); }
.dt__detail { display: flex; flex-direction: column; gap: 5px; }
.dt__detail b { color: var(--text-faint); font-weight: 500; }
.dt__pre {
  margin: 0; padding: 10px 12px; border-radius: 8px;
  background: var(--surface-2, var(--surface)); border: 1px solid var(--border);
  font-size: 11px; line-height: 1.65; max-height: 320px; overflow: auto;
  white-space: pre-wrap; word-break: break-all;
}
</style>
