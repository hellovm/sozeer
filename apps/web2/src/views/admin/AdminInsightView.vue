<script setup lang="ts">
/**
 * AdminInsightView.vue · 数据洞察（v1.0-E4.7）
 *
 * ★★ 为什么要有这一页：
 *   前面几轮把**数据采到了**（心跳调度 / 功能埋点 / 采纳信号 / 提示词指纹），
 *   但**没有任何界面能看到** —— 数据白采。
 *
 * ★ 判据：**采集的下一步必须是把数据变成可行动的信息**，
 *   否则只是"存了一堆数字"。
 *
 * ★ 本页回答两个问题：
 *   ① 改了提示词，生成质量变好了吗？（提示词版本对比）
 *   ② 哪个环节的产出最容易被丢弃？（采纳漏斗）
 *
 * ★★ 诚实标注数据范围：采纳数据 E4.21 起落库（跨重启累计）；
 *    E4.21 之前部署期间的样本因纯内存计数已丢失（无法回填）。
 *   不标注会让运营误读为"历史累计"，做出错误判断。
 */
import { computed, onMounted, ref } from 'vue'
import AppPage from '@/components/AdminPage.vue'
import AppCard from '@/components/AppCard.vue'
import AppTag from '@/components/AppTag.vue'
import AppButton from '@/components/AppButton.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import { useUiStore } from '@/stores/ui'
import {
  insightApi, type PromptAbResult, type PromptVariant,
  type LessonsResult,
} from '@/api/admin_modules_extra'

const ui = useUiStore()

const loading = ref(true)
const err = ref('')
const data = ref<PromptAbResult | null>(null)
const top = ref(10)
/** ★ 手动展开"为什么这样算"（不占默认视觉空间） */
const showWhy = ref(false)

/** #315：避坑记录（独立加载 —— 它来自数据库，不是内存计数） */
const lessonsLoading = ref(true)
const lessons = ref<LessonsResult | null>(null)

async function loadLessons() {
  lessonsLoading.value = true
  try {
    lessons.value = await insightApi.lessons(90)
  } catch {
    lessons.value = null      // ★ 静默：避坑数据缺失不该拖垮整页
  } finally {
    lessonsLoading.value = false
  }
}

async function load() {
  loading.value = true
  err.value = ''
  try {
    data.value = await insightApi.promptAb(top.value)
  } catch (e) {
    err.value = (e as Error).message || '读取失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  load()
  loadLessons()
})

/** AppTag 支持的 kind（★ 必须是字面量联合，否则类型检查不过） */
type TagKind = 'default' | 'info' | 'danger' | 'brand' | 'success' | 'warning'

/** 采纳率等级（用于配色：高=好） */
function rateKind(r: number): TagKind {
  if (r >= 60) return 'success'
  if (r >= 30) return 'warning'
  return 'danger'
}

/** ★ 判断"是否有足够样本做对比" */
const canCompare = computed(() => (data.value?.variants.length || 0) >= 2)

/** ★ 最佳版本（用于高亮） */
const bestFp = computed(() => data.value?.variants[0]?.fingerprint || '')

/** 漏斗里最大的 gen（用于条形宽度归一化） */
const maxGen = computed(() => {
  const rows = data.value?.funnel || []
  return Math.max(1, ...rows.map((r) => r.gen))
})

/** 格式化分桶分布为字符串 */
function fmtBucket(d: Record<string, number> | undefined): string {
  if (!d || !Object.keys(d).length) return '—'
  return Object.entries(d)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${k}: ${v}`)
    .join(' · ')
}

/** 短指纹（前 12 位，够区分且不占宽度） */
function shortFp(fp: string): string {
  return (fp || '').replace(/^sha256:/, '').slice(0, 12)
}
</script>

<template>
  <AppPage
    title="数据洞察"
    subtitle="从采集数据中提炼可行动的结论。★ 采纳数据持久化到数据库（跨重启累计，v1.0-E4.21 起）；避坑记录按书落库。"
    :loading="loading"
    :error="err"
  >
    <template #actions>
      <select v-model.number="top" class="sel" @change="load">
        <option :value="10">显示前 10 个版本</option>
        <option :value="20">显示前 20 个版本</option>
        <option :value="50">显示前 50 个版本</option>
      </select>
      <AppButton size="sm" variant="ghost" @click="load">刷新</AppButton>
    </template>

    <!-- ══════════ 结论卡片（★ 最先展示"该做什么"）══════════ -->
    <AppCard v-if="data" title="结论" class="mb">
      <p class="concl" :class="{ 'concl--muted': !canCompare }">
        {{ data.suggestion || '暂无数据' }}
      </p>
      <div class="meta">
        <AppTag size="sm">样本版本 {{ data.total_variants }} 个</AppTag>
        <AppTag size="sm" kind="warning">{{ data.scope }}</AppTag>
      </div>
      <p class="hint">
        ★ {{ data.note }}
      </p>
    </AppCard>

    <!-- ══════════ 提示词版本对比 ══════════ -->
    <AppCard title="提示词版本对比" class="mb">
      <template v-if="!data?.variants.length">
        <AppEmpty text="暂无足够样本。每个版本需 ≥3 次生成才纳入比较 —— 先正常使用一段时间。" />
      </template>

      <template v-else>
        <p class="hint">
          ★ 判据：看<b>采纳率</b>，不是看生成量 ——
          生成量大可能只是"用户反复重试"（质量差的表现）。
        </p>

        <table class="tbl">
          <thead>
            <tr>
              <th>提示词版本</th>
              <th>环节</th>
              <th class="num">生成</th>
              <th class="num">采纳</th>
              <th class="num">采纳率</th>
              <th class="num">未采纳</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="v in data.variants" :key="v.fingerprint"
              :class="{ 'is-best': v.fingerprint === bestFp && canCompare }"
            >
              <td>
                <code class="fp">{{ shortFp(v.fingerprint) }}</code>
                <span v-if="v.fingerprint === bestFp && canCompare" class="best">
                  最佳
                </span>
              </td>
              <td><AppTag size="sm">{{ v.role }}</AppTag></td>
              <td class="num">{{ v.gen }}</td>
              <td class="num">{{ v.adopt }}</td>
              <td class="num">
                <AppTag size="sm" :kind="rateKind(v.adopt_rate)">
                  {{ v.adopt_rate }}%
                </AppTag>
              </td>
              <td class="num dim">{{ v.pending }}</td>
            </tr>
          </tbody>
        </table>

        <p class="hint hint--sm">
          ★ 版本只显示哈希前 12 位 —— 我们<b>不存提示词原文</b>，
          哈希足以区分"是不是同一版本"。
        </p>
      </template>
    </AppCard>

    <!-- ══════════ 采纳漏斗（哪个环节最容易被丢弃）══════════ -->
    <AppCard v-if="data?.funnel.length" title="采纳漏斗（按环节）" class="mb">
      <p class="hint">
        ★ 看<b>采纳率</b>哪一环最低 —— 那是最该优化的环节。
      </p>
      <div class="funnel">
        <div v-for="f in data.funnel" :key="f.role" class="frow">
          <div class="frow__name">
            <code>{{ f.role }}</code>
          </div>
          <div class="frow__bars">
            <!-- 生成（总长） -->
            <div class="bar bar--gen" :style="{ width: (f.gen / maxGen * 100) + '%' }">
              <span>生成 {{ f.gen }}</span>
            </div>
            <!-- 采纳（叠加显示） -->
            <div
              class="bar bar--adopt"
              :style="{ width: (f.adopt / maxGen * 100) + '%' }"
            >
              <span v-if="f.adopt">采纳 {{ f.adopt }}</span>
            </div>
          </div>
          <div class="frow__rate">
            <AppTag size="sm" :kind="rateKind(f.adopt_rate)">
              {{ f.adopt_rate }}%
            </AppTag>
          </div>
        </div>
      </div>

      <details class="dist">
        <summary>各环节分布明细（修改幅度 / 重写次数）</summary>
        <table class="tbl tbl--sm">
          <thead>
            <tr><th>环节</th><th>修改幅度分布</th><th>重写次数分布</th></tr>
          </thead>
          <tbody>
            <tr v-for="f in data.funnel" :key="f.role">
              <td><code>{{ f.role }}</code></td>
              <td class="dim">{{ fmtBucket(f.edit) }}</td>
              <td class="dim">{{ fmtBucket(f.regen) }}</td>
            </tr>
          </tbody>
        </table>
      </details>
    </AppCard>

    <!-- ══════════ 避坑记录（#315：用户纠错沉淀的经验）══════════ -->
    <AppCard title="避坑记录（用户纠错沉淀）" class="mb">
      <template v-if="lessonsLoading">
        <p class="hint">加载中…</p>
      </template>
      <template v-else-if="!lessons || !lessons.count">
        <AppEmpty text="暂无避坑记录。当用户在聊天中指出问题并让系统修正后，这里会自动沉淀。" />
      </template>
      <template v-else>
        <p class="hint">
          ★ 用户在聊天里「指出问题 → 系统修正」的过程会沉淀为经验，
          并**自动注入该书后续的生成提示词**——同一个坑不会踩第二次。
        </p>
        <div class="meta">
          <AppTag size="sm" kind="info">共 {{ lessons.count }} 条</AppTag>
          <AppTag size="sm" kind="success">生效 {{ lessons.active }}</AppTag>
          <AppTag v-if="lessons.muted" size="sm" kind="warning">
            已停用 {{ lessons.muted }}
          </AppTag>
          <AppTag size="sm">累计注入 {{ lessons.injected_hits }} 次</AppTag>
        </div>
        <table class="tbl">
          <thead>
            <tr>
              <th style="width: 130px">时间</th>
              <th>用户指出</th>
              <th>系统修正</th>
              <th class="num">注入</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="l in lessons.items" :key="l.id">
              <td class="dim">{{ l.created_at }}</td>
              <td>{{ l.trigger || '—' }}</td>
              <td class="dim">{{ l.fix || '—' }}</td>
              <td class="num">{{ l.hits }}</td>
              <td>
                <AppTag size="sm" :kind="l.status === 'active' ? 'success' : 'warning'">
                  {{ l.status === 'active' ? '生效' : '停用' }}
                </AppTag>
              </td>
            </tr>
          </tbody>
        </table>
        <p class="hint hint--sm">★ {{ lessons.note }}</p>
      </template>
    </AppCard>

    <!-- ══════════ 方法论（可折叠，不占默认空间）══════════ -->
    <AppCard title="这些数字怎么来的" class="mb">
      <p class="hint" @click="showWhy = !showWhy" style="cursor: pointer">
        {{ showWhy ? '▾' : '▸' }} 点击展开说明
      </p>
      <div v-if="showWhy" class="why">
        <p><b>数据来源</b>：<code>apps/api/adoption.py</code>（E4.21 起增量落库 <code>adoption_snapshots</code>，跨重启累计）</p>
        <p><b>采集方式</b>：</p>
        <ul>
          <li>生成时：<code>_track()</code> 统一出口（覆盖全部环节）</li>
          <li>采纳时：<code>accept_draft()</code> 唯一出口（穷举所有采纳路径）</li>
        </ul>
        <p><b>隐私保障</b>：</p>
        <ul>
          <li>只采集<b>计数与分桶</b>（如"改动了 1-10%"），不含正文</li>
          <li>提示词<b>只存哈希</b>（SHA256 前 16 位），原文永不落地</li>
          <li>键名有白名单字符集护栏 —— 非 ASCII 内容进不去</li>
        </ul>
        <p><b>说明与局限（重要）</b>：</p>
        <ul>
          <li>✔ E4.21 起<b>跨重启累计</b>：历史增量落库，部署/重启不再清零</li>
          <li>⚠️ E4.21 之前的样本当时是纯内存计数、已随部署丢失，<b>无法回填</b></li>
          <li>⚠️ 样本 &lt;3 的版本不纳入对比（无统计意义）</li>
          <li>⚠️ 只能区分"是否同一版本"，<b>不能还原改了什么</b></li>
        </ul>
      </div>
    </AppCard>
  </AppPage>
</template>

<style scoped>
.mb { margin-bottom: var(--sp-3); }
.concl {
  margin: 0 0 var(--sp-3);
  font-size: var(--fs-sm); line-height: 1.9; color: var(--text);
}
.concl--muted { color: var(--text-muted); }
.meta { display: flex; gap: var(--sp-2); flex-wrap: wrap; margin-bottom: var(--sp-2); }
.hint {
  margin: 0 0 var(--sp-3); font-size: var(--fs-xs);
  color: var(--text-muted); line-height: 1.8;
}
.hint--sm { margin-top: var(--sp-3); margin-bottom: 0; }

.tbl { width: 100%; border-collapse: collapse; font-size: var(--fs-xs); }
.tbl th, .tbl td {
  padding: 8px 10px; text-align: left;
  border-bottom: 1px solid var(--border);
}
.tbl th { color: var(--text-muted); font-weight: 500; }
.tbl td { color: var(--text); }
.tbl .num { text-align: right; font-variant-numeric: tabular-nums; }
.tbl--sm th, .tbl--sm td { padding: 5px 8px; }
tr.is-best { background: var(--success-bg, #f0fdf4); }
.fp { font-family: var(--font-mono); font-size: 11px; }
.best {
  margin-left: 6px; padding: 0 5px; border-radius: 4px;
  background: var(--success, #38a169); color: #fff; font-size: 10px;
}
.dim { color: var(--text-subtle); }

.funnel { display: flex; flex-direction: column; gap: var(--sp-3); }
.frow { display: flex; align-items: center; gap: var(--sp-3); }
.frow__name { flex: none; width: 140px; font-size: 11px; }
.frow__name code { font-family: var(--font-mono); color: var(--text-muted); }
.frow__bars { flex: 1; position: relative; height: 24px; }
.bar {
  position: absolute; left: 0; height: 100%;
  border-radius: 4px; display: flex; align-items: center;
  padding-left: 8px; font-size: 10.5px; white-space: nowrap;
}
.bar--gen {
  background: var(--bg-sunken); color: var(--text-subtle);
  border: 1px solid var(--border);
}
.bar--adopt {
  top: 4px; height: calc(100% - 8px);
  background: var(--brand-200, #bfdbfe); color: var(--brand-800, #1e40af);
  font-weight: 600;
}
.frow__rate { flex: none; width: 60px; text-align: right; }

.dist { margin-top: var(--sp-4); }
.dist summary {
  cursor: pointer; font-size: var(--fs-xs); color: var(--text-muted);
  padding: 6px 0;
}
.dist summary:hover { color: var(--brand); }

.why {
  font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.9;
  padding-top: var(--sp-3); border-top: 1px solid var(--border);
}
.why p { margin: 0 0 6px; }
.why ul { margin: 0 0 10px; padding-left: 18px; }
.why code {
  padding: 1px 5px; border-radius: 4px;
  background: var(--bg-sunken); font-family: var(--font-mono); font-size: 11px;
}
.sel { height: 28px; padding: 0 8px; border-radius: var(--r-md);
       border: 1px solid var(--border); background: var(--bg-elevated);
       color: var(--text); font-size: var(--fs-xs); }
</style>
