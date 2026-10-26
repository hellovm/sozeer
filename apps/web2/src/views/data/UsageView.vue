<script setup lang="ts">
/**
 * UsageView · 用量与成本（v0.26.1）
 *
 * ★ 契约：
 *   GET /api/me/usage  → { books, chapters, balance, total_spent }
 *   GET /api/me/cost   → 成本明细（结构自由，做通用渲染）
 *   GET /api/me/quota  → 额度（部分部署才有）
 *
 * ★ 设计要点：
 *   ① 用量**必须给趋势**，只给总额没法做决策
 *   ② 成本要按"角色/任务"拆分 —— 这是降本的抓手（写正文最贵）
 *   ③ 数字用等宽字体右对齐（可对比）
 */
import { computed, onMounted, ref } from 'vue'
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'
import AppProgress from '@/components/AppProgress.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import { useUiStore } from '@/stores/ui'
import { meApi } from '@/api/modules/me'

const ui = useUiStore()
const loading = ref(true)
const days = ref(30)
const usage = ref<Record<string, number> | null>(null)
const cost = ref<Record<string, unknown> | null>(null)

const overview = computed(() => [
  { label: '作品数', value: usage.value?.books ?? 0 },
  { label: '章节数', value: usage.value?.chapters ?? 0 },
])

async function load() {
  loading.value = true
  try {
    const [u, c] = await Promise.all([
      meApi.usage(days.value).catch(() => null),
      meApi.cost(days.value).catch(() => null),
    ])
    usage.value = u as Record<string, number> | null
    cost.value = c as Record<string, unknown> | null
  } finally { loading.value = false }
}

/** #314：把 /api/me/cost 返回的 user_report 结构化渲染（不再拍平成 "N 项"）。 */
interface CostReport {
  days: number
  today: { input_tokens: number; output_tokens: number; tokens: number; cost: number; calls: number; date: string }
  daily_cap: number
  cap_remain: number
  by_task: Record<string, { tokens: number; cost: number; calls: number }>
  total_cost: number
}
const costReport = computed<CostReport | null>(() => {
  const c = cost.value
  if (!c || typeof c !== 'object') return null
  return c as unknown as CostReport
})

/** 今日 token 额度使用卡（#314：替代原先只看积分账户的 0/0 卡）。 */
const quotaCard = computed(() => {
  const r = costReport.value
  if (!r) return null
  const cap = r.daily_cap || 0
  const used = r.today?.tokens ?? 0
  if (cap > 0) {
    return {
      unlimited: false,
      percent: Math.min(100, Math.round((used / cap) * 100)),
      used, cap,
      remain: r.cap_remain < 0 ? 0 : r.cap_remain,
    }
  }
  return { unlimited: true, percent: 0, used, cap: 0, remain: -1 }
})

/** 按任务拆分的成本明细（#314：任务 / 次数 / tokens / 成本）。 */
const taskRows = computed(() => {
  const r = costReport.value
  if (!r || !r.by_task) return []
  return Object.entries(r.by_task).map(([task, v]) => ({
    task,
    calls: v.calls ?? 0,
    tokens: v.tokens ?? 0,
    cost: v.cost ?? 0,
  }))
})

onMounted(load)
</script>

<template>
  <div class="page">
    <header class="page__head">
      <div>
        <h1 class="page__title">用量与成本</h1>
        <p class="page__sub">按角色拆分成本 —— 写正文用强模型、摘要用便宜模型是降本杠杆点</p>
      </div>
      <div class="head-actions">
        <select v-model.number="days" class="picker" @change="load">
          <option :value="7">近 7 天</option>
          <option :value="30">近 30 天</option>
          <option :value="90">近 90 天</option>
        </select>
        <AppButton variant="ghost" @click="load">刷新</AppButton>
      </div>
    </header>

    <AppSkeleton v-if="loading" variant="card" :count="3" />

    <template v-else>
      <div class="cards">
        <AppCard v-for="o in overview" :key="o.label">
          <div class="stat">
            <span class="stat__label">{{ o.label }}</span>
            <strong class="stat__num">{{ Number(o.value).toLocaleString() }}</strong>
          </div>
        </AppCard>
      </div>

      <AppCard v-if="quotaCard" title="额度使用（今日 Token）" class="mt">
        <template v-if="quotaCard.unlimited">
          <p class="quota-note">本账号今日 Token 额度无限制</p>
        </template>
        <AppProgress v-else
          :percent="quotaCard.percent"
          :label="`已用 ${quotaCard.used.toLocaleString()} / ${quotaCard.cap.toLocaleString()} token`"
          :detail="`剩余 ${quotaCard.remain.toLocaleString()} token`" />
      </AppCard>

      <AppCard :title="`成本明细（近 ${days} 天）`" class="mt">
        <AppEmpty v-if="!costReport" icon="▤" title="暂无成本数据"
                  description="产生模型调用后，这里会按角色/任务展示花费。" />
        <template v-else>
          <div class="sumrow">
            <div class="sumrow__item">
              <span class="sumrow__label">今日用量</span>
              <b class="sumrow__num">{{ (costReport.today?.tokens ?? 0).toLocaleString() }} token</b>
            </div>
            <div class="sumrow__item">
              <span class="sumrow__label">今日花费</span>
              <b class="sumrow__num">¥{{ (costReport.today?.cost ?? 0).toFixed(4) }}</b>
            </div>
            <div class="sumrow__item">
              <span class="sumrow__label">区间总花费</span>
              <b class="sumrow__num">¥{{ (costReport.total_cost ?? 0).toFixed(4) }}</b>
            </div>
          </div>
          <AppEmpty v-if="!taskRows.length" icon="▤" title="暂无成本数据"
                    description="产生模型调用后，这里会按任务展示花费。" />
          <table v-else class="tbl">
            <thead><tr><th>任务</th><th class="num">次数</th><th class="num">tokens</th><th class="num">成本(元)</th></tr></thead>
            <tbody>
              <tr v-for="r in taskRows" :key="r.task">
                <td>{{ r.task }}</td>
                <td class="num">{{ r.calls.toLocaleString() }}</td>
                <td class="num">{{ r.tokens.toLocaleString() }}</td>
                <td class="num">¥{{ r.cost.toFixed(4) }}</td>
              </tr>
            </tbody>
          </table>
        </template>
      </AppCard>

      <AppCard title="降本建议" class="mt">
        <ul class="tips">
          <li><b>按角色路由模型</b>：写正文用强模型；摘要、质检、判定用便宜模型
            （这类调用常占 40% 以上）。</li>
          <li><b>多供应商分流</b>：同一模型挂多个端点，价格或稳定性差异时自动分摊。</li>
          <li><b>批量任务走队列</b>：高峰期排队比失败重试便宜得多。</li>
          <li><b>重拍只重做问题镜头</b>：整段重生成是最大的浪费来源。</li>
        </ul>
      </AppCard>
    </template>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 1000px; margin: 0 auto; }
.page__head { display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--sp-4); margin-bottom: var(--sp-4); flex-wrap: wrap; }
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); max-width: 620px; }
.head-actions { display: flex; gap: var(--sp-2); }
.picker { padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); color: var(--text); font-size: var(--fs-sm);
  font-family: inherit; }
.cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--sp-3); }
.stat { display: flex; flex-direction: column; gap: 4px; }
.stat__label { font-size: var(--fs-xs); color: var(--text-faint); }
.stat__num { font-size: 22px; color: var(--text); font-variant-numeric: tabular-nums; }
.mt { margin-top: var(--sp-4); }
.sumrow { display: flex; gap: var(--sp-5); flex-wrap: wrap; margin-bottom: var(--sp-3); }
.sumrow__item { display: flex; flex-direction: column; gap: 2px; }
.sumrow__label { font-size: var(--fs-xs); color: var(--text-faint); }
.sumrow__num { font-size: 16px; color: var(--text); font-variant-numeric: tabular-nums; }
.quota-note { margin: 0; font-size: var(--fs-sm); color: var(--text-muted); }
.quota-sub { margin: var(--sp-2) 0 0; font-size: var(--fs-xs); color: var(--text-faint); }
.quota-sub-dim { color: var(--text-faint); }
.tbl { width: 100%; border-collapse: collapse; }
.tbl th { text-align: left; font-size: var(--fs-xs); color: var(--text-faint);
  padding: 6px 4px; border-bottom: 1px solid var(--border); font-weight: 500; }
.tbl td { padding: 9px 4px; border-bottom: 1px solid var(--border); font-size: var(--fs-sm); }
.tbl .num { text-align: right; font-variant-numeric: tabular-nums; }
.tips { margin: 0; padding-left: 18px; color: var(--text-muted);
  font-size: var(--fs-sm); line-height: 2; }
.tips b { color: var(--text); }
@media (max-width: 720px) { .cards { grid-template-columns: repeat(2, 1fr); } }
</style>
