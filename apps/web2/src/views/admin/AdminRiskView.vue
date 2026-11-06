<script setup lang="ts">
/**
 * AdminRiskView.vue · 风控中心（v0.26.3 G0-6 续）
 *
 * ★ 对照旧版 RENDER.risk 核对。
 * ★ 包含两块：风控告警（risk）+ 限流后端状态（ratelimit）。
 *   两者都在「风控」语义下，旧版也放在同一页。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppPage from '@/components/AdminPage.vue'
import AppCard from '@/components/AppCard.vue'
import AppTag from '@/components/AppTag.vue'
import AppButton from '@/components/AppButton.vue'
import { useUiStore } from '@/stores/ui'
import { riskApi, type RiskOverview, type RiskAlert } from '@/api/admin_modules_extra'
import { fmtNum } from '@/api/admin_modules'

const route = useRoute()
const ui = useUiStore()

const HOURS = [
  { v: 1, t: '近 1 小时' },
  { v: 6, t: '近 6 小时' },
  { v: 24, t: '近 24 小时' },
  { v: 168, t: '近 7 天' },
]

const loading = ref(true)
const hours = ref(24)
const ov = ref<RiskOverview | null>(null)
const alerts = ref<RiskAlert[]>([])
const rl = ref<Record<string, unknown> | null>(null)
const err = ref('')

/** ★ 等级 → 中文 + 标签样式 */
const LEVEL_CN: Record<string, string> = {
  low: '低', medium: '中', high: '高', critical: '严重',
}
function levelTag(lv?: string) {
  const v = String(lv || '').toLowerCase()
  if (v === 'critical' || v === 'high') return 'danger' as const
  if (v === 'medium') return 'warning' as const
  return 'default' as const
}

const byLevel = computed(() =>
  Object.entries(ov.value?.by_level || {}).map(([k, v]) => ({ k, v: Number(v) })))

/** ★ by_rule 按次数降序 —— 排在前面的才是要优先处理的规则 */
const byRule = computed(() =>
  Object.entries(ov.value?.by_rule || {})
    .map(([k, v]) => ({ k, v: Number(v) }))
    .sort((a, b) => b.v - a.v))

const failRate = computed(() => Number(ov.value?.logins?.fail_rate ?? 0))

async function load() {
  loading.value = true
  err.value = ''
  const [o, a, r] = await Promise.all([
    riskApi.overview(hours.value).catch((e) => {
      err.value = (e as Error).message || '加载风控概览失败'
      return null
    }),
    riskApi.alerts(hours.value, 100).catch(() => ({ items: [] as RiskAlert[] })),
    riskApi.ratelimitStatus().catch(() => null),
  ])
  ov.value = o
  alerts.value = a.items || []
  rl.value = r as Record<string, unknown> | null
  loading.value = false
}

function setHours(h: number) {
  hours.value = h
  load()
}

onMounted(load)
watch(() => route.query._r, () => { load() })
</script>

<template>
  <AppPage
    title="风控中心"
    subtitle="登录异常、越权尝试、刷接口等风险的聚合视图。限流后端状态也在本页。"
    :loading="loading"
    :error="err"
    empty-icon="◎"
    empty-title="窗口期内没有风险告警"
    empty-desc="这是好事。有异常登录或越权尝试时会出现在这里。"
    :columns="[
      { key: 'level', label: '等级', width: '72px' },
      { key: 'rule', label: '规则', width: '180px' },
      { key: 'detail', label: '详情' },
      { key: 'ip', label: 'IP', width: '130px', hideSm: true },
      { key: 'user_id', label: '用户', width: '130px', hideSm: true },
      { key: 'created_at', label: '时间', width: '150px', hideSm: true },
    ]"
    :rows="alerts as unknown as Array<Record<string, unknown>>"
  >
    <template #actions>
      <select :value="hours" class="sel" @change="setHours(Number(($event.target as HTMLSelectElement).value))">
        <option v-for="h in HOURS" :key="h.v" :value="h.v">{{ h.t }}</option>
      </select>
      <AppButton size="sm" variant="ghost" @click="load">刷新</AppButton>
    </template>

    <template #cards>
      <AppCard>
        <div class="stat">
          <span class="stat__k">告警总数</span>
          <strong class="stat__v" :class="{ warn: (ov?.alerts_total || 0) > 0 }">
            {{ fmtNum(ov?.alerts_total) }}
          </strong>
          <span class="stat__s">近 {{ ov?.window_hours ?? hours }} 小时</span>
        </div>
      </AppCard>
      <AppCard>
        <div class="stat">
          <span class="stat__k">登录失败率</span>
          <strong class="stat__v" :class="{ danger: failRate >= 30 }">
            {{ failRate.toFixed(1) }}%
          </strong>
          <span class="stat__s">
            成功 {{ fmtNum(ov?.logins?.success) }} · 失败 {{ fmtNum(ov?.logins?.failed) }}
          </span>
        </div>
      </AppCard>
      <AppCard>
        <div class="stat">
          <span class="stat__k">等级分布</span>
          <div class="levels">
            <AppTag v-for="l in byLevel" :key="l.k" size="sm" :kind="levelTag(l.k)">
              {{ LEVEL_CN[l.k] || l.k }} {{ l.v }}
            </AppTag>
            <span v-if="!byLevel.length" class="dim">无</span>
          </div>
        </div>
      </AppCard>
    </template>

    <!-- 规则排行 + 限流状态（表格上方） -->
    <div v-if="!loading" class="extra">
      <AppCard title="高频规则" :subtitle="byRule.length ? '按触发次数降序 —— 排前面的优先处理' : ''">
        <div v-if="!byRule.length" class="dim">窗口期内无规则触发</div>
        <ul v-else class="rules">
          <li v-for="r in byRule.slice(0, 10)" :key="r.k">
            <code class="rules__k">{{ r.k }}</code>
            <span class="rules__bar">
              <i :style="{ width: (r.v / byRule[0].v * 100) + '%' }" />
            </span>
            <b class="rules__v">{{ r.v }}</b>
          </li>
        </ul>
      </AppCard>

      <AppCard title="限流后端" subtitle="★ 多 worker 下必须用 Redis，内存限流会让额度翻 N 倍">
        <ul v-if="rl" class="kv">
          <li v-for="(v, k) in rl" :key="k">
            <span class="kv__k">{{ k }}</span>
            <span class="kv__v mono">{{ typeof v === 'object' ? JSON.stringify(v) : String(v) }}</span>
          </li>
        </ul>
        <div v-else class="dim">限流状态不可用（可能未配置 Redis）</div>
      </AppCard>
    </div>

    <template #cell-level="{ value }">
      <AppTag size="sm" :kind="levelTag(value as string)">
        {{ LEVEL_CN[String(value || '').toLowerCase()] || value || '—' }}
      </AppTag>
    </template>

    <template #cell-rule="{ value }">
      <code class="mono">{{ value || '—' }}</code>
    </template>

    <template #cell-detail="{ value }">
      <span class="detail">{{ value || '—' }}</span>
    </template>

    <template #cell-ip="{ value }">
      <span class="mono dim">{{ value || '—' }}</span>
    </template>

    <template #cell-user_id="{ value }">
      <span class="mono dim">{{ value || '—' }}</span>
    </template>

    <template #cell-created_at="{ value }">
      <span class="mono dim">{{ value || '—' }}</span>
    </template>

    <template #hint>
      ★ 误报是正常的 —— 风控规则宁可多报也不能漏报。
      但**每条告警都应能定位到具体行为**（IP / 用户 / 时间），否则无法判断。
    </template>
  </AppPage>
</template>

<style scoped>
.sel {
  padding: 6px 10px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); color: var(--text); font-size: 12px; font-family: inherit;
}
.stat { display: flex; flex-direction: column; gap: 3px; }
.stat__k { font-size: 11px; color: var(--text-faint); }
.stat__v { font-size: 20px; font-variant-numeric: tabular-nums; }
.stat__v.warn { color: var(--warn, #b8860b); }
.stat__v.danger { color: var(--danger, #d33); }
.stat__s { font-size: 11px; color: var(--text-muted); }
.levels { display: flex; gap: 5px; flex-wrap: wrap; min-height: 24px; align-items: center; }
.dim { color: var(--text-faint); font-size: 11px; }
.mono { font-variant-numeric: tabular-nums; font-size: 11px; }
.detail { font-size: 12px; line-height: 1.6; }

.extra {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 12px;
  margin-top: 12px;
  align-items: start;
}
.rules { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 7px; }
.rules li { display: flex; align-items: center; gap: 9px; }
.rules__k { font-size: 11px; flex: 0 1 auto; max-width: 46%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rules__bar { flex: 1; height: 6px; background: var(--surface-2, var(--border));
  border-radius: 3px; overflow: hidden; min-width: 40px; }
.rules__bar i { display: block; height: 100%; background: var(--brand); border-radius: 3px; }
.rules__v { font-size: 11px; font-variant-numeric: tabular-nums; min-width: 28px; text-align: right; }

.kv { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.kv li { display: flex; gap: 10px; font-size: 12px; align-items: baseline; }
.kv__k { flex: none; width: 110px; color: var(--text-faint); font-size: 11px; }
.kv__v { word-break: break-all; }
</style>
