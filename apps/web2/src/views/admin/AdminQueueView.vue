<script setup lang="ts">
/**
 * AdminQueueView.vue · 任务队列（v0.26.3 G0-6 续）
 *
 * ★ 对照旧版 RENDER.queue 核对。
 *
 * ★★ 本页的四个维护操作全部是**破坏性**的（MEMORY §10/§12）：
 *   · reap    —— 重排僵尸任务（释放被幽灵任务占满的配额）
 *   · rebuild —— 从 DB 重建 Redis 队列（Redis 数据丢失后）
 *   · reset   —— 清空队列（★ 会丢任务）
 *   · clearDlq—— 清空死信
 *   → 全部走 AdminConfirm 强制填原因，绝不提供"一键执行"。
 *
 * ★ 队列 stats 结构会随演进变化 → 用**通用渲染**（键值对 + 已知字段高亮），
 *   而不是硬编码字段（否则后端加个字段页面就崩）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppPage from '@/components/AdminPage.vue'
import AppCard from '@/components/AppCard.vue'
import AppTag from '@/components/AppTag.vue'
import AppButton from '@/components/AppButton.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import AdminConfirm from '@/components/AdminConfirm.vue'
import { useUiStore } from '@/stores/ui'
import {
  queueApi, type QueueDashboard, type QueueStatus, type QueueAlertRow,
} from '@/api/admin_modules_extra'
import { fmtNum } from '@/api/admin_modules'

const route = useRoute()
const ui = useUiStore()

const loading = ref(true)
const dash = ref<QueueDashboard | null>(null)
const status = ref<QueueStatus | null>(null)
const alerts = ref<QueueAlertRow[]>([])
const err = ref('')

/** 已知的计数键（高亮显示）；其余键走"其他指标"通用渲染 */
const KNOWN_KEYS = new Set([
  'queued', 'running', 'done', 'failed', 'dlq', 'total',
  'finished', 'pending', 'abandoned',
])

const counters = computed(() => {
  const s = dash.value || {}
  return Object.entries(s)
    .filter(([k, v]) => KNOWN_KEYS.has(k) && typeof v === 'number')
    .map(([k, v]) => ({ k, v: Number(v) }))
})

/** 其余标量指标（变化不可预期 → 通用渲染） */
const otherMetrics = computed(() => {
  const s = dash.value || {}
  return Object.entries(s)
    .filter(([k, v]) => !KNOWN_KEYS.has(k)
      && !['peek', 'limits', 'dlq_items', 'lanes'].includes(k)
      && (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean'))
    .map(([k, v]) => ({ k, v: String(v) }))
})

/** 分道积压（哪条道堵了） */
const lanes = computed(() => {
  const l = dash.value?.lanes
  if (!l || typeof l !== 'object') return []
  return Object.entries(l).map(([k, v]) => ({ k, v: Number(v) }))
    .sort((a, b) => b.v - a.v)
})

/** 队首任务（调试用 —— 看"为什么这个任务一直不动"） */
const peekRows = computed(() => (dash.value?.peek || []) as Array<Record<string, unknown>>)
const dlqRows = computed(() => (dash.value?.dlq_items || []) as Array<Record<string, unknown>>)

/** 队首任务的列（动态取前几个键，避免硬编码） */
const peekCols = computed(() => {
  const first = peekRows.value[0]
  if (!first) return []
  return Object.keys(first).slice(0, 6)
})

const alertCount = computed(() => alerts.value.length)

/* ── 维护操作 ── */
type Op = 'reap' | 'rebuild' | 'reset' | 'clearDlq'
const pending = ref<Op | null>(null)
const submitting = ref(false)

const OP_CFG: Record<Op, { title: string; body: string; ok: string; danger: boolean }> = {
  reap: {
    title: '重排僵尸任务',
    body: '扫描超时未释放的任务并归还配额。<br>'
      + '<span class="ok">安全操作</span> —— 只处理已超时的任务（OOM kill 不会走 finally，'
      + '所以配额可能被幽灵任务占满）。',
    ok: '确认重排',
    danger: false,
  },
  rebuild: {
    title: '从数据库重建队列',
    body: '★ Redis 数据丢失后的恢复手段。会按 DB 中 running/pending 的记录重新入队。<br>'
      + '<b class="warn">执行前请确认 Redis 确实异常</b>，否则会与现有队列产生重复。',
    ok: '确认重建',
    danger: true,
  },
  reset: {
    title: '★ 清空队列（危险）',
    body: '<b class="danger">所有排队中的任务将被丢弃，且不可恢复。</b><br>'
      + '仅在队列数据损坏、需要人工介入时使用。正常情况请用「重排僵尸任务」。',
    ok: '确认清空',
    danger: true,
  },
  clearDlq: {
    title: '清空死信队列',
    body: '清空失败任务的记录。<br>'
      + '<span class="warn">建议先确认死信内容已排查完毕</span> —— 清空后无法回溯失败原因。',
    ok: '确认清空',
    danger: true,
  },
}

const confirmOpen = computed({
  get: () => pending.value !== null,
  set: (v: boolean) => { if (!v) pending.value = null },
})

const curOp = computed(() => (pending.value ? OP_CFG[pending.value] : OP_CFG.reap))

async function doOp(reason: string) {
  const op = pending.value
  if (!op) return
  submitting.value = true
  try {
    if (op === 'reap') await queueApi.reap(reason)
    else if (op === 'rebuild') await queueApi.rebuild(reason)
    else if (op === 'reset') await queueApi.reset(reason)
    else await queueApi.clearDlq(reason)
    ui.ok('操作成功')
    pending.value = null
    await load()
  } catch (e) {
    ui.err((e as Error).message || '操作失败')
  } finally {
    submitting.value = false
  }
}

async function resetAlerts() {
  try {
    await queueApi.resetAlerts()
    ui.ok('告警已重置')
    await load()
  } catch (e) {
    ui.err((e as Error).message || '重置失败')
  }
}

async function load() {
  loading.value = true
  err.value = ''
  const [d, s, a] = await Promise.all([
    queueApi.dashboard(30).catch((e) => {
      err.value = (e as Error).message || '加载队列看板失败'
      return null
    }),
    queueApi.status().catch(() => null),
    queueApi.alerts().catch(() => ({ items: [] as QueueAlertRow[] })),
  ])
  dash.value = d
  status.value = s as QueueStatus | null
  alerts.value = a.items || []
  loading.value = false
}

onMounted(load)
watch(() => route.query._r, () => { load() })
</script>

<template>
  <AppPage
    title="任务队列"
    subtitle="分道积压、并发闸门、死信与告警。四个维护操作均为破坏性，会强制记录原因。"
    :loading="loading"
    :error="err"
    empty-icon="▤"
    empty-title="队列无数据"
    empty-desc="Redis 未连接或队列尚未启用。"
  >
    <template #actions>
      <AppTag v-if="alertCount" size="sm" kind="warning">告警 {{ alertCount }}</AppTag>
      <AppButton size="sm" variant="ghost" @click="load">刷新</AppButton>
    </template>

    <template #cards>
      <AppCard v-for="c in counters" :key="c.k">
        <div class="stat">
          <span class="stat__k">{{ c.k }}</span>
          <strong
            class="stat__v"
            :class="{
              warn: (c.k === 'queued' && c.v > 100) || (c.k === 'dlq' && c.v > 0),
              danger: c.k === 'failed' && c.v > 0,
            }"
          >{{ fmtNum(c.v) }}</strong>
        </div>
      </AppCard>
      <AppCard v-if="!counters.length">
        <div class="stat">
          <span class="stat__k">状态</span>
          <strong class="stat__v">—</strong>
          <span class="stat__s">无计数指标</span>
        </div>
      </AppCard>
    </template>

    <div v-if="!loading" class="grid">
      <!-- ★ 告警（节流提示：刷屏会让人关掉通知） -->
      <AppCard title="队列告警">
        <template #actions>
          <AppButton v-if="alertCount" size="sm" variant="ghost" @click="resetAlerts">重置计数</AppButton>
        </template>
        <AppEmpty v-if="!alertCount" icon="✓" title="无告警"
                  description="积压、失败率、DLQ 增长均未超阈值。" />
        <ul v-else class="alerts">
          <li v-for="(a, i) in alerts" :key="i" class="alerts__i">
            <AppTag size="sm" :kind="String(a.level) === 'critical' ? 'danger' : 'warning'">
              {{ a.level || 'warn' }}
            </AppTag>
            <div class="alerts__b">
              <b>{{ a.kind || '告警' }}</b>
              <p>{{ a.message || '' }}</p>
              <span class="dim">
                {{ a.count ? `累计 ${a.count} 次` : '' }}
                {{ a.last_at ? `· ${a.last_at}` : '' }}
              </span>
            </div>
          </li>
        </ul>
      </AppCard>

      <!-- 分道积压 -->
      <AppCard title="分道积压" subtitle="哪条道堵了 —— 值越大越需要关注">
        <AppEmpty v-if="!lanes.length" icon="▤" title="无分道数据" />
        <ul v-else class="rules">
          <li v-for="l in lanes" :key="l.k">
            <code class="rules__k">{{ l.k }}</code>
            <span class="rules__bar">
              <i :class="{ warn: l.v > 50 }"
                 :style="{ width: (l.v / Math.max(1, lanes[0].v) * 100) + '%' }" />
            </span>
            <b class="rules__v">{{ fmtNum(l.v) }}</b>
          </li>
        </ul>
      </AppCard>

      <!-- 维护操作 -->
      <AppCard title="维护操作" subtitle="★ 全部为破坏性操作，需填写原因">
        <div class="ops">
          <div class="ops__i">
            <div class="ops__t">重排僵尸任务</div>
            <p class="ops__d">
              worker 被 OOM kill 时不会走 finally，配额会被幽灵任务占满 →
              用户永久排队。此操作用来释放。
            </p>
            <AppButton size="sm" variant="ghost" @click="pending = 'reap'">执行重排</AppButton>
          </div>
          <div class="ops__i">
            <div class="ops__t">从数据库重建队列</div>
            <p class="ops__d">Redis 数据丢失后的恢复手段。会让运行中的任务重新入队。</p>
            <AppButton size="sm" variant="ghost" @click="pending = 'rebuild'">执行重建</AppButton>
          </div>
          <div class="ops__i">
            <div class="ops__t">清空死信队列</div>
            <p class="ops__d">清空失败记录。清空后无法回溯失败原因，建议先排查。</p>
            <AppButton size="sm" variant="ghost" @click="pending = 'clearDlq'">清空死信</AppButton>
          </div>
          <div class="ops__i danger">
            <div class="ops__t">清空队列</div>
            <p class="ops__d">★ 所有排队任务将被丢弃且不可恢复。仅在数据损坏时使用。</p>
            <AppButton size="sm" variant="danger" @click="pending = 'reset'">清空队列</AppButton>
          </div>
        </div>
      </AppCard>
    </div>

    <!-- 队首任务 -->
    <AppCard v-if="!loading && peekRows.length" title="队首任务" subtitle="调试用：看「为什么这个任务一直不动」" class="mt">
      <div class="tblwrap">
        <table class="tbl">
          <thead>
            <tr><th v-for="c in peekCols" :key="c">{{ c }}</th></tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in peekRows" :key="i">
              <td v-for="c in peekCols" :key="c" class="mono">{{ r[c] ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </AppCard>

    <!-- 死信 -->
    <AppCard v-if="!loading && dlqRows.length" title="死信预览" :subtitle="`${dlqRows.length} 条`" class="mt">
      <pre class="pre">{{ JSON.stringify(dlqRows.slice(0, 10), null, 2) }}</pre>
      <p class="dim">★ 解析失败也会返回原始串 —— 便于排查"任务为什么进了死信"。</p>
    </AppCard>

    <!-- 其他指标 + Redis 状态 -->
    <div v-if="!loading" class="grid mt">
      <AppCard v-if="otherMetrics.length" title="其他指标">
        <ul class="kv">
          <li v-for="m in otherMetrics" :key="m.k">
            <span class="kv__k">{{ m.k }}</span>
            <span class="kv__v mono">{{ m.v }}</span>
          </li>
        </ul>
      </AppCard>

      <AppCard v-if="status" title="Redis 状态">
        <ul class="kv">
          <li v-for="(v, k) in status" :key="k">
            <span class="kv__k">{{ k }}</span>
            <span class="kv__v mono">
              {{ typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v) }}
            </span>
          </li>
        </ul>
      </AppCard>
    </div>

    <template #hint>
      ★ 队列内存策略必须是 <code>noeviction</code> —— 驱逐 key 会静默丢任务。
      分支执行请用「重排僵尸任务」，不要用「清空队列」。
    </template>
  </AppPage>

  <AdminConfirm
    v-model="confirmOpen"
    :title="curOp.title"
    :body="curOp.body"
    :ok-text="curOp.ok"
    :danger="curOp.danger"
    :loading="submitting"
    reason-placeholder="请填写操作原因（会写入审计日志）"
    @confirm="doOp"
  />
</template>

<style scoped>
.stat { display: flex; flex-direction: column; gap: 3px; }
.stat__k { font-size: 11px; color: var(--text-faint); }
.stat__v { font-size: 20px; font-variant-numeric: tabular-nums; }
.stat__v.warn { color: var(--warn, #b8860b); }
.stat__v.danger { color: var(--danger, #d33); }
.stat__s { font-size: 11px; color: var(--text-muted); }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 12px;
  margin-top: 12px;
  align-items: start;
}
.mt { margin-top: 12px; }
.dim { color: var(--text-faint); font-size: 11px; }
.mono { font-variant-numeric: tabular-nums; font-size: 11px; }

.alerts { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.alerts__i { display: flex; gap: 9px; align-items: flex-start; }
.alerts__b { display: flex; flex-direction: column; gap: 2px; }
.alerts__b b { font-size: 12px; }
.alerts__b p { margin: 0; font-size: 11px; color: var(--text-muted); line-height: 1.7; }

.rules { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 7px; }
.rules li { display: flex; align-items: center; gap: 9px; }
.rules__k { font-size: 11px; flex: 0 1 auto; max-width: 44%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rules__bar { flex: 1; height: 6px; background: var(--surface-2, var(--border));
  border-radius: 3px; overflow: hidden; min-width: 40px; }
.rules__bar i { display: block; height: 100%; background: var(--brand); border-radius: 3px; }
.rules__bar i.warn { background: var(--warn, #b8860b); }
.rules__v { font-size: 11px; font-variant-numeric: tabular-nums; min-width: 36px; text-align: right; }

.ops { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; }
.ops__i {
  padding: 11px 12px; border: 1px solid var(--border); border-radius: 9px;
  display: flex; flex-direction: column; gap: 6px; align-items: flex-start;
}
.ops__i.danger { border-color: color-mix(in srgb, var(--danger, #d33) 30%, transparent); }
.ops__t { font-size: 12px; font-weight: 600; }
.ops__d { margin: 0; font-size: 11px; color: var(--text-muted); line-height: 1.7; flex: 1; }

.tblwrap { overflow-x: auto; }
.tbl { width: 100%; border-collapse: collapse; font-size: 11px; }
.tbl th { text-align: left; font-weight: 500; font-size: 11px; color: var(--text-faint);
  padding: 6px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }
.tbl td { padding: 6px 8px; border-bottom: 1px solid var(--border);
  white-space: nowrap; max-width: 220px; overflow: hidden; text-overflow: ellipsis; }
.tbl tr:last-child td { border-bottom: none; }

.pre {
  margin: 0 0 8px; padding: 10px 12px; border-radius: 8px;
  background: var(--surface-2, var(--surface)); border: 1px solid var(--border);
  font-size: 11px; line-height: 1.6; max-height: 260px; overflow: auto;
}

.kv { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.kv li { display: flex; gap: 10px; font-size: 12px; align-items: baseline; }
.kv__k { flex: none; width: 120px; color: var(--text-faint); font-size: 11px; }
.kv__v { word-break: break-all; }

.ok { color: var(--success, #1a7f4b); }
.warn { color: var(--warn, #b8860b); }
.danger { color: var(--danger, #d33); }
</style>
