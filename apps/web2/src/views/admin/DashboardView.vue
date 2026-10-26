<script setup lang="ts">
/**
 * DashboardView.vue · 总览看板（v0.26.2 G0-6）
 *
 * ★ 对照旧版 RENDER.dashboard 逐项核对，字段全部对齐 admin_service.dashboard_overview。
 * ★ 改进点：
 *   ① 8 张 KPI 卡按语义分组（营收 / 用户 / 内容 / 风险），旧版是一锅平铺
 *   ② 趋势图在数据全 0 时显示空态（旧版会画一排 2px 灰条，看不出"其实没数据"）
 *   ③ 支持 7/14/30 天切换（旧版写死 14）
 *   ④ 局部失败不整页崩：两个请求分别兜底
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppCard from '@/components/AppCard.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import AppButton from '@/components/AppButton.vue'
import AdminBars from '@/components/AdminBars.vue'
import { useUiStore } from '@/stores/ui'
import {
  dashboardApi, fmtMoney, fmtNum, fmtWords,
  type DashboardOverview, type DashboardTrend,
} from '@/api/admin_modules'

const route = useRoute()
const ui = useUiStore()

const days = ref(14)
const loading = ref(true)
const ov = ref<DashboardOverview | null>(null)
const tr = ref<DashboardTrend | null>(null)
/** 局部错误（某个接口失败仍展示另一半） */
const partialErr = ref('')

/** KPI 分组（语义化，比一锅平铺好定位） */
const kpiGroups = computed(() => {
  const d = ov.value
  if (!d) return []
  const r = d.revenue
  return [
    {
      title: '营收',
      items: [
        {
          k: '累计营收（净额）',
          v: fmtMoney(r.net_cents),
          s: `已收 ${fmtMoney(r.total_cents)} · 退款 ${fmtMoney(r.refunded_cents)}`,
          tone: 'up',
        },
        {
          k: `近 ${d.window_days} 天营收`,
          v: fmtMoney(r.window_cents),
          s: `${fmtNum(r.window_orders)} 笔已支付`,
          tone: 'up',
        },
        {
          k: '待支付 / 失败',
          v: `${fmtNum(r.pending_orders)} / ${fmtNum(d.tasks.failed_in_window)}`,
          s: `已退款 ${fmtNum(r.refunded_orders)} 笔 · ${fmtMoney(r.refunded_cents)}`,
          tone: r.pending_orders > 0 ? 'warn' : '',
        },
      ],
    },
    {
      title: '用户与积分',
      items: [
        {
          k: '用户总数',
          v: fmtNum(d.users.total),
          s: `活跃 ${fmtNum(d.users.active)} · 新增 ${fmtNum(d.users.new_in_window)}`,
          tone: '',
        },
        {
          k: '积分负债（未消费）',
          v: fmtNum(d.credits.outstanding),
          s: `冻结 ${fmtNum(d.credits.frozen)}`,
          tone: '',
        },
      ],
    },
    {
      title: '内容与任务',
      items: [
        {
          k: '作品 / 章节',
          v: `${fmtNum(d.content.books)} / ${fmtNum(d.content.chapters)}`,
          s: `总字数 ${fmtWords(d.content.words)}`,
          tone: '',
        },
        {
          k: '运行中任务',
          v: fmtNum(d.tasks.running),
          s: `近 ${d.window_days} 天失败 ${fmtNum(d.tasks.failed_in_window)}`,
          tone: '',
        },
      ],
    },
    {
      title: '风险',
      items: [
        {
          k: '待处理标记',
          v: fmtNum(d.moderation.open_flags),
          s: `已隐藏作品 ${fmtNum(d.moderation.hidden_books)}`,
          tone: d.moderation.open_flags > 0 ? 'warn' : '',
        },
      ],
    },
  ]
})

/** 趋势是否全是 0（此时显示空态比画一排 2px 条更诚实） */
const revenueAllZero = computed(() =>
  !tr.value || tr.value.revenue_yuan.every((v) => !Number(v)))
const userAllZero = computed(() =>
  !tr.value || (tr.value.new_users.every((v) => !Number(v))
    && tr.value.new_books.every((v) => !Number(v))))

async function load() {
  loading.value = true
  partialErr.value = ''
  // ★ 分别兜底：一个挂了另一个仍要显示（后台容错第一）
  const [a, b] = await Promise.all([
    dashboardApi.overview(days.value).catch((e) => {
      partialErr.value = `概览加载失败：${(e as Error).message}`
      return null
    }),
    dashboardApi.trend(days.value).catch(() => null),
  ])
  ov.value = a
  tr.value = b
  loading.value = false
}

function setDays(n: number) {
  days.value = n
  load()
}

onMounted(load)
// 顶栏"刷新"按钮通过 query 触发（与 AdminLayout.refreshPage 配合）
watch(() => route.query._r, () => { load() })
</script>

<template>
  <div class="dash">
    <div class="dash__bar">
      <div class="seg">
        <button
          v-for="n in [7, 14, 30]"
          :key="n"
          class="seg__btn"
          :class="{ on: days === n }"
          @click="setDays(n)"
        >
          近 {{ n }} 天
        </button>
      </div>
      <span class="sp" />
      <AppButton size="sm" variant="ghost" @click="load">刷新</AppButton>
    </div>

    <p v-if="partialErr" class="dash__err">{{ partialErr }}</p>

    <AppSkeleton v-if="loading" variant="card" :count="4" />

    <template v-else-if="ov">
      <section v-for="g in kpiGroups" :key="g.title" class="dash__group">
        <h2 class="dash__gtitle">{{ g.title }}</h2>
        <div class="kpis">
          <AppCard v-for="it in g.items" :key="it.k">
            <div class="kpi">
              <span class="kpi__k">{{ it.k }}</span>
              <strong class="kpi__v" :class="`kpi__v--${it.tone}`">{{ it.v }}</strong>
              <span class="kpi__s">{{ it.s }}</span>
            </div>
          </AppCard>
        </div>
      </section>

      <AppCard :title="`近 ${ov.window_days} 天营收趋势（元）`" class="mt">
        <AppEmpty
          v-if="revenueAllZero"
          icon="▤"
          title="窗口期内暂无营收"
          description="有已支付订单后这里会显示趋势。"
        />
        <AdminBars
          v-else-if="tr"
          :labels="tr.labels"
          :series="[{ label: '营收（元）', data: tr.revenue_yuan, color: 'var(--brand)' }]"
          :format="(n) => '¥' + n.toFixed(2)"
        />
      </AppCard>

      <AppCard :title="`近 ${ov.window_days} 天新增用户 / 作品`" class="mt">
        <AppEmpty
          v-if="userAllZero"
          icon="▤"
          title="窗口期内暂无新增"
        />
        <AdminBars
          v-else-if="tr"
          :labels="tr.labels"
          :series="[
            { label: '新增用户', data: tr.new_users, color: 'var(--brand)' },
            { label: '新增作品', data: tr.new_books, color: '#2d5fb8' },
          ]"
        />
      </AppCard>

      <p class="dash__foot">
        数据生成时间：{{ ov.generated_at }} · 全部为数据库实时聚合，无缓存
      </p>
    </template>

    <AppEmpty
      v-else
      icon="⚠"
      title="看板加载失败"
      description="请检查后端服务状态后重试。"
    />
  </div>
</template>

<style scoped>
.dash { display: flex; flex-direction: column; }
.dash__bar { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.sp { flex: 1; }
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.seg__btn {
  padding: 5px 12px;
  font-size: 12px;
  border: none;
  background: var(--surface);
  color: var(--text-muted);
  cursor: pointer;
  font-family: inherit;
  border-right: 1px solid var(--border);
}
.seg__btn:last-child { border-right: none; }
.seg__btn.on { background: var(--brand); color: #fff; }

.dash__err {
  font-size: 12px;
  color: var(--danger, #d33);
  margin: 0 0 12px;
  padding: 8px 11px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--danger, #d33) 8%, transparent);
}

.dash__group { margin-bottom: 16px; }
.dash__gtitle {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-faint);
  letter-spacing: .06em;
  margin: 0 0 8px;
}
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(196px, 1fr)); gap: 12px; }
.kpi { display: flex; flex-direction: column; gap: 3px; }
.kpi__k { font-size: 11px; color: var(--text-faint); }
.kpi__v { font-size: 21px; font-variant-numeric: tabular-nums; line-height: 1.25; }
.kpi__v--up { color: var(--success, #1a7f4b); }
.kpi__v--warn { color: var(--warn, #b8860b); }
.kpi__s { font-size: 11px; color: var(--text-muted); }

.mt { margin-top: 14px; }
.dash__foot { font-size: 11px; color: var(--text-faint); margin: 16px 0 0; }
</style>
