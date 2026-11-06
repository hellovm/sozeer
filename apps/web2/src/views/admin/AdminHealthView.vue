<script setup lang="ts">
/**
 * AdminHealthView.vue · 系统状态（v0.26.3 G0-6 续）
 *
 * ★ 对照旧版 RENDER.health 核对。
 * ★ 改进：阈值可读化（>85% 红、>70% 黄），旧版只有一个硬编码 danger 阈值；
 *   磁盘/内存条带百分比标注，一眼看出余量。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'
import AppTag from '@/components/AppTag.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import { useUiStore } from '@/stores/ui'
import { healthApi, type HealthResult } from '@/api/admin_modules_extra'
import { fmtNum } from '@/api/admin_modules'

const route = useRoute()
const ui = useUiStore()

const loading = ref(true)
const h = ref<HealthResult | null>(null)

/** 使用率 → 风险等级（一眼看出"该不该管"） */
function level(pct?: number): 'ok' | 'warn' | 'danger' {
  const v = Number(pct ?? 0)
  if (v >= 85) return 'danger'
  if (v >= 70) return 'warn'
  return 'ok'
}

const levelTag = { ok: 'success', warn: 'warning', danger: 'danger' } as const

const diskPct = computed(() => Number(h.value?.disk?.used_pct ?? 0))
const memPct = computed(() => Number(h.value?.memory?.used_pct ?? 0))

async function load() {
  loading.value = true
  try {
    h.value = await healthApi.get()
  } catch (e) {
    h.value = null
    ui.err((e as Error).message || '加载系统状态失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => route.query._r, () => { load() })
</script>

<template>
  <div class="page">
    <header class="head">
      <div>
        <h1 class="head__t">系统状态</h1>
        <p class="head__s">
          数据来源 <code>/api/admin/health</code>（读 /proc 与磁盘用量，无额外依赖）
        </p>
      </div>
      <AppButton size="sm" variant="ghost" @click="load">刷新</AppButton>
    </header>

    <AppSkeleton v-if="loading" variant="card" :count="5" />

    <AppEmpty
      v-else-if="!h"
      icon="⚠"
      title="状态获取失败"
      description="后端服务可能不可用，请检查服务进程。"
    />

    <template v-else>
      <div class="cards">
        <!-- 应用 -->
        <AppCard>
          <div class="stat">
            <span class="stat__k">应用版本</span>
            <strong class="stat__v mono">v{{ h.version }}</strong>
            <span class="stat__s">{{ h.time }}</span>
          </div>
        </AppCard>

        <!-- 数据库 -->
        <AppCard>
          <div class="stat">
            <span class="stat__k">数据库</span>
            <div class="stat__row">
              <AppTag size="sm" :kind="h.db.ok ? 'success' : 'danger'">
                {{ h.db.ok ? '正常' : '异常' }}
              </AppTag>
            </div>
            <span class="stat__s">{{ h.db.url_scheme }} · 延迟 {{ h.db.latency }}</span>
          </div>
        </AppCard>

        <!-- 磁盘 -->
        <AppCard>
          <div class="stat">
            <span class="stat__k">磁盘使用</span>
            <strong class="stat__v" :class="`is-${level(diskPct)}`">
              {{ h.disk?.used_pct != null ? h.disk.used_pct + '%' : '—' }}
            </strong>
            <span class="stat__s">
              剩余 {{ h.disk?.free_gb ?? '—' }} GB / 共 {{ h.disk?.total_gb ?? '—' }} GB
            </span>
          </div>
          <div class="bar">
            <i :class="`is-${level(diskPct)}`" :style="{ width: Math.min(100, diskPct) + '%' }" />
          </div>
        </AppCard>

        <!-- 内存 -->
        <AppCard>
          <div class="stat">
            <span class="stat__k">内存使用</span>
            <strong class="stat__v" :class="`is-${level(memPct)}`">
              {{ h.memory?.used_pct != null ? h.memory.used_pct + '%' : '—' }}
            </strong>
            <span class="stat__s">
              可用 {{ h.memory?.available_mb != null
                ? (h.memory.available_mb / 1024).toFixed(2) : '—' }} GB /
              共 {{ h.memory?.total_mb != null
                ? (h.memory.total_mb / 1024).toFixed(2) : '—' }} GB
            </span>
          </div>
          <div class="bar">
            <i :class="`is-${level(memPct)}`" :style="{ width: Math.min(100, memPct) + '%' }" />
          </div>
        </AppCard>

        <!-- 负载 -->
        <AppCard>
          <div class="stat">
            <span class="stat__k">系统负载</span>
            <strong class="stat__v mono">{{ h.load?.['1m'] ?? '—' }}</strong>
            <span class="stat__s">
              5m {{ h.load?.['5m'] ?? '—' }} · 15m {{ h.load?.['15m'] ?? '—' }}
            </span>
          </div>
        </AppCard>
      </div>

      <!-- ★ 风险提示（只在有需要时出现） -->
      <AppCard v-if="level(diskPct) !== 'ok' || level(memPct) !== 'ok' || !h.db.ok" class="mt">
        <template #header>
          <h3 class="warn__t">需要关注</h3>
        </template>
        <ul class="warn__l">
          <li v-if="!h.db.ok">
            <b>数据库异常</b> —— 检查 PostgreSQL 进程与连接串；应用多数功能会不可用。
          </li>
          <li v-if="level(diskPct) === 'danger'">
            <b>磁盘使用率 {{ h.disk.used_pct }}%（≥85%）</b> ——
            优先清理日志与备份（<code>/opt/novelforge/logs</code>、
            <code>/opt/novelforge/backups</code>），否则服务可能写入失败。
          </li>
          <li v-else-if="level(diskPct) === 'warn'">
            <b>磁盘使用率 {{ h.disk.used_pct }}%</b> —— 建议提前清理旧备份。
          </li>
          <li v-if="level(memPct) === 'danger'">
            <b>内存使用率 {{ h.memory.used_pct }}%（≥85%）</b> ——
            注意批量生成任务的并发上限，避免 OOM 被系统 kill。
          </li>
          <li v-else-if="level(memPct) === 'warn'">
            <b>内存使用率 {{ h.memory.used_pct }}%</b> —— 留意并发任务数。
          </li>
        </ul>
      </AppCard>

      <p class="hint">
        阈值说明：使用率 <b>≥85%</b> 标红、<b>≥70%</b> 标黄。
        负载值为 1/5/15 分钟平均（单核满载 ≈ 1.0，需结合 CPU 核数判断）。
      </p>
    </template>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; }
.head { display: flex; align-items: flex-start; justify-content: space-between;
  gap: 14px; flex-wrap: wrap; margin-bottom: 12px; }
.head__t { font-size: 16px; font-weight: 600; margin: 0 0 4px; }
.head__s { font-size: 12px; color: var(--text-muted); margin: 0; }
.head__s code { font-size: 11px; padding: 1px 5px; border-radius: 4px; border: 1px solid var(--border); }

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(184px, 1fr)); gap: 12px; }
.stat { display: flex; flex-direction: column; gap: 3px; }
.stat__k { font-size: 11px; color: var(--text-faint); }
.stat__v { font-size: 20px; font-variant-numeric: tabular-nums; line-height: 1.3; }
.stat__row { display: flex; align-items: center; gap: 6px; min-height: 26px; }
.stat__s { font-size: 11px; color: var(--text-muted); }
.mono { font-variant-numeric: tabular-nums; }
.is-ok { color: var(--text); }
.is-warn { color: var(--warn, #b8860b); }
.is-danger { color: var(--danger, #d33); }

.bar {
  height: 6px;
  background: var(--surface-2, var(--border));
  border-radius: 3px;
  overflow: hidden;
  margin-top: 8px;
}
.bar i { display: block; height: 100%; border-radius: 3px; transition: width .3s; }
.bar i.is-ok { background: var(--brand); }
.bar i.is-warn { background: var(--warn, #b8860b); }
.bar i.is-danger { background: var(--danger, #d33); }

.mt { margin-top: 12px; }
.warn__t { font-size: 13px; font-weight: 600; margin: 0; }
.warn__l { margin: 0; padding-left: 18px; }
.warn__l li { font-size: 12px; line-height: 1.95; color: var(--text-muted); }
.warn__l b { color: var(--text); }
.warn__l code { font-size: 11px; padding: 1px 5px; border-radius: 4px; border: 1px solid var(--border); }

.hint { font-size: 11px; color: var(--text-faint); margin: 14px 0 0; line-height: 1.8; }
.hint b { color: var(--text-muted); }
</style>
