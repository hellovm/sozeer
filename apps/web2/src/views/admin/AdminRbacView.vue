<script setup lang="ts">
/**
 * AdminRbacView.vue · 权限管理（v0.26.3 G0-6 续）
 *
 * ★ 对照旧版 RENDER.rbac 核对（旧版 115 行）。
 *
 * ★★ 本页的两条安全底线（MEMORY §25/§27/§28/§55）：
 *   ① **表驱动 + 内置兜底** —— 读 DB 实际生效值，表被清空时回退内置定义
 *      （否则"表被清空 = 全站无权限雪崩"）
 *   ② **防同级提权** —— 不能操作 rank ≥ 自己的成员（后端把关，前端提示原因）
 *
 * ★ 展示 `source` 字段：db = 界面改过；builtin = 代码内置兜底。
 *   这样运维一眼能看出"这个角色到底是我配的还是系统默认的"。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppPage from '@/components/AdminPage.vue'
import AppCard from '@/components/AppCard.vue'
import AppTag from '@/components/AppTag.vue'
import AppButton from '@/components/AppButton.vue'
import AppInput from '@/components/AppInput.vue'
import AppModal from '@/components/AppModal.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import AppTabs from '@/components/AppTabs.vue'
import AdminConfirm from '@/components/AdminConfirm.vue'
import { useUiStore } from '@/stores/ui'
import {
  rbacApi, type RbacMatrix, type RbacMemberRow,
} from '@/api/admin_modules_extra'
import { fmtNum } from '@/api/admin_modules'

const route = useRoute()
const ui = useUiStore()

const PAGE_SIZE = 50
const tab = ref<'matrix' | 'members'>('matrix')

const loading = ref(true)
const err = ref('')
const matrix = ref<RbacMatrix | null>(null)
const members = ref<RbacMemberRow[]>([])
const memberTotal = ref(0)
const roles = ref<Array<{ key: string; label: string; desc?: string }>>([])
const mq = ref({ keyword: '', role: '', offset: 0 })

/** 角色列表（从矩阵/成员接口任一处取） */
const roleList = computed(() => {
  if (roles.value.length) return roles.value
  const r = matrix.value?.roles || {}
  return Object.entries(r).map(([k, v]) => ({ key: k, label: v.label || k, desc: v.desc }))
})

/** ★ 权限点分组展示（矩阵可能很大，按前缀分组更好读） */
const matrixGroups = computed(() => {
  const r = matrix.value?.roles || {}
  const byPrefix: Record<string, Array<{ role: string; label: string; perms: string[]; source: string }>> = {}
  for (const [role, info] of Object.entries(r)) {
    const perms = info.permissions || []
    for (const p of perms) {
      const pre = p.includes('.') ? p.split('.')[0] : '_other'
      ;(byPrefix[pre] = byPrefix[pre] || []).push({
        role, label: info.label || role, perms: [p], source: info.source || 'builtin',
      })
    }
  }
  return byPrefix
})

/** 角色 → 权限数（概览用） */
const roleStats = computed(() => {
  const r = matrix.value?.roles || {}
  return Object.entries(r).map(([k, v]) => ({
    key: k,
    label: v.label || k,
    desc: v.desc || '',
    count: (v.permissions || []).length,
    patterns: v.patterns || [],
    source: v.source || 'builtin',
  }))
})

/* ── 改角色权限 ── */
const editOpen = ref(false)
const editRole = ref('')
const editPatterns = ref<string[]>([])
const saving = ref(false)

function openEditRole(role: string) {
  const r = matrix.value?.roles?.[role]
  if (!r) return
  editRole.value = role
  editPatterns.value = [...(r.patterns || [])]
  editOpen.value = true
}

/* ── 改成员角色 ── */
const pendingRole = ref<{ member: RbacMemberRow; to: string } | null>(null)
const roleOpen = computed({
  get: () => pendingRole.value !== null,
  set: (v: boolean) => { if (!v) pendingRole.value = null },
})

function askRole(member: RbacMemberRow, to: string) {
  pendingRole.value = { member, to }
}

/** ★ 防同级提权：不能把别人提到 ≥ 自己的角色（后端也校验，这里先给提示） */
function canAssign(m: RbacMemberRow): boolean {
  // 简单前端提示：owner 角色不可由 admin 授予；admin 不可操作 owner
  if (m.role === 'owner') return false
  return true
}

async function doSetRole(reason: string) {
  const p = pendingRole.value
  if (!p) return
  saving.value = true
  try {
    await rbacApi.setMemberRole(p.member.id, p.to, reason)
    ui.ok('角色已变更')
    pendingRole.value = null
    await loadMembers()
  } catch (e) {
    ui.err((e as Error).message || '变更失败')
  } finally {
    saving.value = false
  }
}

/* ── 重置 ── */
const resetOpen = ref(false)
async function doReset(reason: string) {
  saving.value = true
  try {
    await rbacApi.reset(reason)
    ui.ok('已重置为内置定义')
    resetOpen.value = false
    await loadMatrix()
  } catch (e) {
    ui.err((e as Error).message || '重置失败')
  } finally {
    saving.value = false
  }
}

async function loadMatrix() {
  loading.value = true
  err.value = ''
  try {
    matrix.value = await rbacApi.matrix()
  } catch (e) {
    matrix.value = null
    err.value = (e as Error).message || '加载权限矩阵失败'
  } finally {
    loading.value = false
  }
}

async function loadMembers() {
  loading.value = true
  err.value = ''
  try {
    const d = await rbacApi.members({ ...mq.value, limit: PAGE_SIZE })
    members.value = d.items || []
    memberTotal.value = Number(d.total || 0)
    if (d.roles?.length) roles.value = d.roles
  } catch (e) {
    members.value = []
    err.value = (e as Error).message || '加载成员失败'
  } finally {
    loading.value = false
  }
}

function switchTab(t: string) {
  tab.value = t as typeof tab.value
  if (tab.value === 'members') loadMembers()
  else loadMatrix()
}

function searchMembers() {
  mq.value = { ...mq.value, offset: 0 }
  loadMembers()
}

onMounted(loadMatrix)
watch(() => route.query._r, () => { switchTab(tab.value) })
</script>

<template>
  <AppPage
    title="权限管理"
    subtitle="角色 × 权限矩阵读 DB 实际生效值（表驱动），表被清空时回退内置定义兜底。"
    :loading="loading"
    :error="err"
    empty-icon="⚿"
    empty-title="暂无数据"
  >
    <template #actions>
      <AppTag size="sm">{{ roleStats.length }} 个角色</AppTag>
      <AppButton size="sm" variant="ghost" @click="switchTab(tab)">刷新</AppButton>
      <AppButton size="sm" variant="danger" @click="resetOpen = true">重置为内置</AppButton>
    </template>

    <template #toolbar>
      <AppTabs
        :model-value="tab"
        :items="[
          { key: 'matrix', label: '权限矩阵' },
          { key: 'members', label: '成员', badge: tab === 'members' ? memberTotal : undefined },
        ]"
        @update:model-value="switchTab"
      />
      <template v-if="tab === 'members'">
        <AppInput
          v-model="mq.keyword"
          placeholder="搜索用户名 / 邮箱"
          clearable
          style="max-width: 200px"
          @keyup.enter="searchMembers"
        />
        <select v-model="mq.role" class="sel" @change="searchMembers">
          <option value="">全部角色</option>
          <option v-for="r in roleList" :key="r.key" :value="r.key">{{ r.label }}</option>
        </select>
        <AppButton size="sm" variant="primary" @click="searchMembers">搜索</AppButton>
      </template>
    </template>

    <!-- ══════════ 权限矩阵 ══════════ -->
    <template v-if="!loading && tab === 'matrix'">
      <AppEmpty v-if="!roleStats.length" icon="⚿" title="未取到权限矩阵" />

      <div v-else class="cards">
        <AppCard v-for="r in roleStats" :key="r.key">
          <div class="role">
            <div class="role__head">
              <b>{{ r.label }}</b>
              <AppTag size="sm" :kind="r.source === 'db' ? 'brand' : 'default'">
                {{ r.source === 'db' ? '界面配置' : '内置兜底' }}
              </AppTag>
            </div>
            <p v-if="r.desc" class="role__d">{{ r.desc }}</p>
            <div class="role__c">
              <span class="role__n">{{ fmtNum(r.count) }}</span>
              <span class="dim">个权限点</span>
            </div>
            <div class="role__p">
              <code v-for="p in r.patterns.slice(0, 6)" :key="p" class="code">{{ p }}</code>
              <span v-if="r.patterns.length > 6" class="dim">+{{ r.patterns.length - 6 }}</span>
            </div>
            <AppButton size="sm" variant="ghost" @click="openEditRole(r.key)">编辑权限</AppButton>
          </div>
        </AppCard>
      </div>

      <AppCard title="完整权限对照" subtitle="按权限域分组" class="mt">
        <div class="tblwrap">
          <table class="tbl">
            <thead>
              <tr>
                <th style="width:150px">权限域</th>
                <th>角色 · 权限点</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(list, pre) in matrixGroups" :key="pre">
                <td><AppTag size="sm">{{ pre }}</AppTag></td>
                <td>
                  <span v-for="(x, i) in list" :key="x.role + i" class="pair">
                    <b>{{ x.label }}</b><code class="code">{{ x.perms[0] }}</code>
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </AppCard>
    </template>

    <!-- ══════════ 成员 ══════════ -->
    <template v-if="!loading && tab === 'members'">
      <AppCard>
        <AppEmpty v-if="!members.length" icon="☺" title="没有匹配的成员" />
        <div v-else class="tblwrap">
          <table class="tbl">
            <thead>
              <tr>
                <th>用户名</th>
                <th style="width:150px">昵称</th>
                <th style="width:140px">角色</th>
                <th style="width:90px">状态</th>
                <th style="width:230px" class="ops">调整角色</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="m in members" :key="m.id">
                <td><b>{{ m.username }}</b></td>
                <td class="dim">{{ m.nickname || '—' }}</td>
                <td>
                  <AppTag size="sm"
                          :kind="m.role === 'owner' ? 'brand' : m.role === 'admin' ? 'warning' : 'default'">
                    {{ m.role_label || m.role }}
                  </AppTag>
                </td>
                <td>
                  <AppTag size="sm" :kind="m.is_active ? 'success' : 'danger'">
                    {{ m.is_active ? '正常' : '已封禁' }}
                  </AppTag>
                </td>
                <td class="ops">
                  <template v-if="canAssign(m)">
                    <button
                      v-for="r in roleList.filter((x) => x.key !== m.role)"
                      :key="r.key"
                      class="op"
                      @click="askRole(m, r.key)"
                    >设为{{ r.label }}</button>
                  </template>
                  <span v-else class="dim">所有者（不可调整）</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="members.length" class="pager">
          <span class="dim">共 {{ fmtNum(memberTotal) }} 人</span>
          <AppButton size="sm" variant="ghost" :disabled="mq.offset <= 0"
                     @click="mq.offset = Math.max(0, mq.offset - PAGE_SIZE); loadMembers()">
            上一页
          </AppButton>
          <AppButton size="sm" variant="ghost" :disabled="mq.offset + PAGE_SIZE >= memberTotal"
                     @click="mq.offset += PAGE_SIZE; loadMembers()">
            下一页
          </AppButton>
        </div>
      </AppCard>
    </template>

    <template #hint>
      ★ <b>表驱动 + 内置兜底</b>：矩阵读 DB 实际生效值；若表被清空，系统回退到代码内置定义 ——
      避免「全站无权限」雪崩。<br>
      ★ <b>防同级提权</b>：不能操作 rank ≥ 自己的成员（由后端把关，前端仅做提示）。<br>
      ★ <b>最高权限点</b>：<code>workspace.manage</code>（工作区删除）、<code>ads.manage</code>、
      <code>model.manage</code> 均只给 owner。
    </template>
  </AppPage>

  <!-- ══════════ 编辑角色权限 ══════════ -->
  <AppModal v-model="editOpen" :title="`编辑「${editRole}」权限`" width="600px">
    <div class="fm">
      <p class="fm__d">
        权限通配符用 <code class="code">.</code> 分隔：
        <code class="code">book.*</code> 命中 <code class="code">book.write</code>，
        但<b>不</b>匹配 <code class="code">bookmark.read</code>。
      </p>
      <div class="fm__list">
        <code v-for="(p, i) in editPatterns" :key="i" class="pat">{{ p }}</code>
        <span v-if="!editPatterns.length" class="dim">（无权限）</span>
      </div>
      <p class="fm__w">
        ⚠ 修改会立即影响<b>所有</b>该角色的成员（含已登录会话，下次请求即生效）。
      </p>
    </div>
  </AppModal>

  <!-- ══════════ 改成员角色 ══════════ -->
  <AdminConfirm
    v-model="roleOpen"
    :title="`将「${pendingRole?.member.username || ''}」设为 ${pendingRole?.to || ''}`"
    :body="pendingRole?.to === 'owner'
      ? '<b class=\'warn\'>所有者拥有全部权限（含工作区删除），请确认该成员可信。</b>'
      : pendingRole?.to === 'admin'
        ? '管理员可封号、开票、调积分 —— 但不含广告/模型配置（那两项仅所有者）。'
        : '该成员将获得对应角色的权限。'"
    ok-text="确认变更"
    :danger="pendingRole?.to === 'owner' || pendingRole?.to === 'admin'"
    :loading="saving"
    reason-placeholder="请填写变更原因（如：岗位调整 / 权限回收）"
    @confirm="doSetRole"
  />

  <!-- ══════════ 重置确认 ══════════ -->
  <AdminConfirm
    v-model="resetOpen"
    title="★ 重置权限矩阵"
    body="将所有角色权限恢复为<b>代码内置定义</b>，丢弃界面上的全部修改。<br>
      <span class='dim'>这是表被改坏时的救命手段。重置后可在矩阵里重新调整。</span>"
    ok-text="确认重置"
    :danger="true"
    :loading="saving"
    reason-placeholder="请填写重置原因"
    @confirm="doReset"
  />
</template>

<style scoped>
.sel { padding: 6px 10px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); color: var(--text); font-size: 12px; font-family: inherit; }
.mt { margin-top: 12px; }
.dim { color: var(--text-faint); font-size: 11px; }
.warn { color: var(--warn, #b8860b); }
.code { font-size: 11px; padding: 1px 5px; border-radius: 4px;
  border: 1px solid var(--border); word-break: break-all; }

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
.role { display: flex; flex-direction: column; gap: 7px; align-items: flex-start; }
.role__head { display: flex; align-items: center; gap: 8px; }
.role__head b { font-size: 13px; }
.role__d { margin: 0; font-size: 11px; color: var(--text-muted); line-height: 1.7; }
.role__c { display: flex; align-items: baseline; gap: 5px; }
.role__n { font-size: 19px; font-variant-numeric: tabular-nums; }
.role__p { display: flex; flex-wrap: wrap; gap: 4px; }

.tblwrap { overflow-x: auto; }
.tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
.tbl th { text-align: left; font-weight: 500; font-size: 11px; color: var(--text-faint);
  padding: 7px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }
.tbl td { padding: 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
.tbl tr:last-child td { border-bottom: none; }
.tbl .ops { white-space: nowrap; vertical-align: middle; }
.pair { display: inline-flex; align-items: center; gap: 5px; margin: 0 12px 5px 0; }
.pair b { font-size: 11px; color: var(--text-muted); font-weight: 500; }

.op { font-size: 11px; padding: 3px 8px; margin-right: 4px;
  border: 1px solid var(--border); border-radius: 5px;
  background: var(--surface); color: var(--text-muted);
  cursor: pointer; font-family: inherit; }
.op:hover { color: var(--text); border-color: var(--brand); }
.pager { display: flex; align-items: center; gap: 10px; padding-top: 12px; }

.fm { display: flex; flex-direction: column; gap: 12px; }
.fm__d { margin: 0; font-size: 12px; color: var(--text-muted); line-height: 1.8; }
.fm__d b { color: var(--text); }
.fm__list { display: flex; flex-wrap: wrap; gap: 5px;
  max-height: 220px; overflow: auto; padding: 10px;
  border: 1px solid var(--border); border-radius: 8px; }
.pat { font-size: 11px; padding: 2px 7px; border-radius: 5px;
  background: var(--surface-2, var(--surface)); border: 1px solid var(--border); }
.fm__w { margin: 0; font-size: 11px; color: var(--warn, #b8860b); line-height: 1.75; }
.fm__w b { color: var(--danger, #d33); }
</style>
