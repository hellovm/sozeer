<script setup lang="ts">
/**
 * AdminUsersView.vue · 用户管理（v0.26.2 G0-6）
 *
 * ★ 对照旧版 RENDER.users + userAction 逐项核对。
 *
 * ★★ 关键设计（相对旧版的改进）：
 *   ① **操作意图不再靠读按钮文案推断** —— 旧版用
 *      `!btn.textContent.includes('解封')` 判断是封禁还是解封，
 *      这种做法在文案改动后会直接**做反**（把要封禁的人解封了）。
 *      新版把意图作为参数显式传递。
 *   ② 分页边界用真实 total，不用 `offset+50 >= total` 推断
 *   ③ 搜索防抖 + Enter 提交
 *   ④ 明确区分"已注销"（不可操作）与"已封禁"（可解封）
 *
 * ★★ 安全红线：管理员**不可读用户正文**（MEMORY §30）。
 *   本页只展示统计量（作品数、积分），不含任何章节内容。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'
import AppInput from '@/components/AppInput.vue'
import AppTag from '@/components/AppTag.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import AdminConfirm from '@/components/AdminConfirm.vue'
import { useUiStore } from '@/stores/ui'
import { fmtNum, usersApi, type AdminUserRow } from '@/api/admin_modules'

const route = useRoute()
const ui = useUiStore()

const PAGE_SIZE = 50

const loading = ref(true)
const rows = ref<AdminUserRow[]>([])
const total = ref(0)

const q = ref({ keyword: '', role: '', status: '', offset: 0 })

/* ── 确认弹窗状态 ── */
interface PendingAct {
  kind: 'ban' | 'unban' | 'promote' | 'demote'
    // ★ v1.0-E4.13 批量与高危操作
    | 'bulk_ban' | 'bulk_unban' | 'bulk_delete'
    | 'force_delete'
  user?: AdminUserRow
}
const pending = ref<PendingAct | null>(null)
const submitting = ref(false)

const confirmCfg = computed(() => {
  const p = pending.value
  if (!p) return { title: '', body: '', okText: '' }
  const name = p.user?.username || ''
  switch (p.kind) {
    case 'ban':
      return {
        title: `封禁用户「${name}」`,
        body: '<b style="color:var(--danger,#d33)">封禁后该用户所有已签发的登录令牌立即失效。</b>'
          + '<br>用户无法登录，已生成内容保留（不会删除）。',
        okText: '确认封禁',
      }
    case 'unban':
      return {
        title: `解封用户「${name}」`,
        body: '解封后用户可正常登录并继续创作。',
        okText: '确认解封',
      }
    case 'promote':
      return {
        title: `提升「${name}」为管理员`,
        body: '<b style="color:var(--warn,#b8860b)">管理员可封号、调积分、退款 —— 请确认该用户可信。</b>',
        okText: '确认提升',
      }
    case 'demote':
      return {
        title: `将「${name}」降为普通用户`,
        body: '该用户将失去所有后台权限，已登录的后台会话在令牌到期后失效。',
        okText: '确认降级',
      }
    // ★ v1.0-E4.13 批量与高危
    case 'bulk_ban':
      return {
        title: `批量封禁 ${selected.value.length} 个用户`,
        body: '<b style="color:var(--danger,#d33)">所选用户的登录令牌将立即全部失效。</b>',
        okText: '确认批量封禁',
      }
    case 'bulk_unban':
      return {
        title: `批量解封 ${selected.value.length} 个用户`,
        body: '所选用户将恢复登录能力。',
        okText: '确认批量解封',
      }
    case 'bulk_delete':
      return {
        title: `⚠️ 强制删除 ${selected.value.length} 个用户`,
        body: '<b style="color:var(--danger,#d33)">不可恢复！</b>将级联删除所选用户的作品、短剧、任务等全部归属数据；'
          + '订单/发票凭证默认脱敏保留（合规）。',
        okText: '确认强制删除',
      }
    default: // force_delete
      return {
        title: `⚠️ 强制删除用户「${name}」`,
        body: '<b style="color:var(--danger,#d33)">不可恢复！</b>将级联删除该用户的作品、短剧、任务等全部归属数据；'
          + '订单/发票凭证默认脱敏保留（合规）。',
        okText: '确认强制删除',
      }
  }
})

const page = computed(() => Math.floor(q.value.offset / PAGE_SIZE) + 1)
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const hasPrev = computed(() => q.value.offset > 0)
const hasNext = computed(() => q.value.offset + PAGE_SIZE < total.value)

/**
 * ★ AdminConfirm 的 modelValue 是 boolean；这里用计算属性适配。
 *   ★ 关闭时必须清空 pending —— 否则下次打开会残留上一次的目标用户，
 *     导致"给 A 填的原因"被提交到 B 身上（真出过事的一类 bug）。
 */
const confirmOpen = computed({
  get: () => pending.value !== null,
  set: (v: boolean) => { if (!v) pending.value = null },
})

async function load() {
  loading.value = true
  try {
    const d = await usersApi.search({ ...q.value, limit: PAGE_SIZE })
    rows.value = d.items || []
    total.value = Number(d.total || 0)
  } catch (e) {
    rows.value = []
    total.value = 0
    ui.err((e as Error).message || '加载用户失败')
  } finally {
    loading.value = false
  }
}

function search() {
  q.value = { ...q.value, offset: 0 }
  load()
}

function reset() {
  q.value = { keyword: '', role: '', status: '', offset: 0 }
  load()
}

function goPage(delta: number) {
  const next = q.value.offset + delta * PAGE_SIZE
  q.value = { ...q.value, offset: Math.max(0, next) }
  load()
}

/** ★ 意图显式传递（不再靠按钮文案推断 —— 旧版的做法会在改文案时做反） */
function ask(kind: PendingAct['kind'], user: AdminUserRow) {
  pending.value = { kind, user }
}

/* ── ★ v1.0-E4.13 多选 + 批量操作 + 导出 ── */
const selected = ref<string[]>([])
const allChecked = computed(() =>
  rows.value.length > 0 && selected.value.length === rows.value.length)

function toggleAll(v: boolean) {
  selected.value = v ? rows.value.map((u) => u.id) : []
}
function toggleOne(u: AdminUserRow, v: boolean) {
  selected.value = v ? [...selected.value, u.id]
    : selected.value.filter((id) => id !== u.id)
}
function askBulk(kind: 'bulk_ban' | 'bulk_unban' | 'bulk_delete') {
  if (!selected.value.length) { ui.warn('请先勾选用户'); return }
  pending.value = { kind }
}

async function exportOne(u: AdminUserRow) {
  try {
    await usersApi.exportUser(u.id, `sozeer-user-${u.username}.zip`)
    ui.ok('数据已开始下载')
  } catch (e) { ui.err((e as Error).message || '导出失败') }
}

async function doConfirm(reason: string) {
  const p = pending.value
  if (!p) return
  submitting.value = true
  try {
    if (p.kind === 'ban' || p.kind === 'unban') {
      await usersApi.setActive(p.user!.id, p.kind === 'unban', reason)
    } else if (p.kind === 'promote' || p.kind === 'demote') {
      await usersApi.setRole(p.user!.id, p.kind === 'promote' ? 'admin' : 'user', reason)
    } else if (p.kind === 'bulk_ban' || p.kind === 'bulk_unban') {
      const r = await usersApi.bulk(p.kind === 'bulk_unban' ? 'unban' : 'ban',
        [...selected.value], reason)
      ui.ok(`批量操作完成：成功 ${r.succeeded ?? r.total ?? 0}，失败 ${r.failed ?? 0}`)
      selected.value = []
    } else if (p.kind === 'bulk_delete') {
      // 逐个强删（批量删除走同一级联服务，便于单点重试）
      let ok = 0
      for (const id of [...selected.value]) {
        try {
          await usersApi.forceDelete(id, reason)
          ok += 1
        } catch { /* 单个失败不中断其余 */ }
      }
      ui.ok(`强制删除完成：${ok}/${selected.value.length}`)
      selected.value = []
    } else if (p.kind === 'force_delete') {
      await usersApi.forceDelete(p.user!.id, reason)
      ui.ok('已删除')
    }
    pending.value = null
    await load()
  } catch (e) {
    ui.err((e as Error).message || '操作失败')
    // ★ 失败时**不清空**弹窗：让用户可以直接重试（原因还在）
  } finally {
    submitting.value = false
  }
}

function statusOf(u: AdminUserRow) {
  if (u.deleted) return { text: '已注销', kind: 'default' as const }
  if (u.is_active) return { text: '正常', kind: 'success' as const }
  return { text: '已封禁', kind: 'danger' as const }
}

onMounted(load)
watch(() => route.query._r, () => { load() })
</script>

<template>
  <div class="page">
    <!-- ── 筛选栏 ── -->
    <AppCard class="filters">
      <div class="filters__row">
        <div class="filters__kw">
          <AppInput
            v-model="q.keyword"
            placeholder="搜索用户名 / 邮箱 / 昵称 / 手机号"
            clearable
            @keyup.enter="search"
          />
        </div>
        <select v-model="q.role" class="sel" @change="search">
          <option value="">全部角色</option>
          <option value="owner">所有者</option>
          <option value="admin">管理员</option>
          <option value="editor">编辑</option>
          <option value="user">普通用户</option>
        </select>
        <select v-model="q.status" class="sel" @change="search">
          <option value="">全部状态</option>
          <option value="active">正常</option>
          <option value="banned">已封禁</option>
          <option value="deleted">已注销</option>
        </select>
        <AppButton size="sm" variant="primary" @click="search">搜索</AppButton>
        <AppButton size="sm" variant="ghost" @click="reset">重置</AppButton>
        <span class="sp" />
        <span class="count">共 {{ fmtNum(total) }} 条</span>
      </div>
      <!-- ★ v1.0-E4.13 批量操作栏（有勾选才出现） -->
      <div v-if="selected.length" class="bulkbar">
        <span>已选 <b>{{ selected.length }}</b> 人</span>
        <AppButton size="sm" variant="ghost" @click="askBulk('bulk_ban')">批量封禁</AppButton>
        <AppButton size="sm" variant="ghost" @click="askBulk('bulk_unban')">批量解封</AppButton>
        <AppButton size="sm" variant="danger" @click="askBulk('bulk_delete')">强制删除</AppButton>
        <AppButton size="sm" variant="text" @click="selected = []">取消选择</AppButton>
      </div>
    </AppCard>

    <AppSkeleton v-if="loading" variant="table" :count="8" class="mt" />

    <AppCard v-else class="mt">
      <AppEmpty
        v-if="!rows.length"
        icon="☺"
        title="没有匹配的用户"
        description="换个关键词或清空筛选条件试试。"
      />

      <div v-else class="tbl-wrap">
        <table class="tbl">
          <thead>
            <tr>
              <th class="chk">
                <input type="checkbox" :checked="allChecked"
                       :aria-label="'全选本页'" @change="toggleAll(($event.target as HTMLInputElement).checked)" />
              </th>
              <th>用户名</th>
              <th>昵称</th>
              <th>邮箱</th>
              <th>手机</th>
              <th>角色</th>
              <th class="num">积分</th>
              <th class="num">作品</th>
              <th>状态</th>
              <th>最后登录</th>
              <th class="ops">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="u in rows" :key="u.id">
              <td class="chk">
                <input v-if="!u.deleted && u.role !== 'owner'" type="checkbox"
                       :checked="selected.includes(u.id)"
                       :aria-label="`选择 ${u.username}`"
                       @change="toggleOne(u, ($event.target as HTMLInputElement).checked)" />
              </td>
              <td><b>{{ u.username }}</b></td>
              <td>{{ u.nickname || '—' }}</td>
              <td class="mono">{{ u.email || '—' }}</td>
              <td class="mono">{{ u.phone || '—' }}</td>
              <td>
                <AppTag size="sm" :kind="u.role === 'owner' ? 'brand'
                  : u.role === 'admin' ? 'warning' : 'default'">
                  {{ u.role }}
                </AppTag>
              </td>
              <td class="num">
                {{ fmtNum(u.credits) }}
                <span v-if="u.credits_frozen" class="frozen" title="冻结中">
                  (冻 {{ fmtNum(u.credits_frozen) }})
                </span>
              </td>
              <td class="num">{{ fmtNum(u.books) }}</td>
              <td>
                <AppTag size="sm" :kind="statusOf(u).kind">{{ statusOf(u).text }}</AppTag>
                <span v-if="u.totp_enabled" class="tfa" title="已开启二次验证">2FA</span>
              </td>
              <td class="mono dim">{{ u.last_login_at || '—' }}</td>
              <td class="ops">
                <template v-if="u.deleted">
                  <span class="dim">已注销，不可操作</span>
                </template>
                <template v-else>
                  <button
                    class="op"
                    @click="ask(u.is_active ? 'ban' : 'unban', u)"
                  >{{ u.is_active ? '封禁' : '解封' }}</button>
                  <button
                    v-if="u.role !== 'owner'"
                    class="op"
                    @click="ask(u.role === 'admin' ? 'demote' : 'promote', u)"
                  >{{ u.role === 'admin' ? '降为用户' : '升为管理员' }}</button>
                  <span v-else class="dim">所有者</span>
                  <button class="op" @click="exportOne(u)">导出</button>
                  <button v-if="u.role !== 'owner'" class="op danger" @click="ask('force_delete', u)">删除</button>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="rows.length" class="pager">
        <span class="dim">第 {{ page }} / {{ totalPages }} 页</span>
        <AppButton size="sm" variant="ghost" :disabled="!hasPrev" @click="goPage(-1)">
          上一页
        </AppButton>
        <AppButton size="sm" variant="ghost" :disabled="!hasNext" @click="goPage(1)">
          下一页
        </AppButton>
      </div>
    </AppCard>

    <p class="hint">
      管理员不可读取用户作品正文（有测试守卫）。手机号已脱敏展示 —— 后台也不需要全量。
    </p>

    <!-- ★ 二次确认：封禁 / 角色变更都不可轻率 -->
    <AdminConfirm
      v-model="confirmOpen"
      :title="confirmCfg.title"
      :body="confirmCfg.body"
      :ok-text="confirmCfg.okText"
      :loading="submitting"
      :reason-placeholder="pending?.kind === 'ban'
        ? '请填写封禁原因（如：发布违规内容 / 恶意刷积分）'
        : '请填写变更原因（如：岗位调整 / 权限回收）'"
      @confirm="doConfirm"
    />
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; }
.filters__row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.filters__kw { flex: 1 1 240px; min-width: 200px; }
.sel {
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
  font-size: 12px;
  font-family: inherit;
}
.sp { flex: 1 1 auto; }
.count { font-size: 11px; color: var(--text-faint); }
.mt { margin-top: 12px; }
.bulkbar {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin-top: 10px; padding: 8px 10px;
  border: 1px solid color-mix(in srgb, var(--warn, #b8860b) 35%, transparent);
  border-radius: 8px; background: color-mix(in srgb, var(--warn, #b8860b) 8%, transparent);
  font-size: 12px;
}
.chk { width: 32px; text-align: center; }

.tbl-wrap { overflow-x: auto; }
.tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
.tbl th {
  text-align: left;
  font-weight: 500;
  font-size: 11px;
  color: var(--text-faint);
  padding: 7px 8px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.tbl td {
  padding: 9px 8px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
.tbl tr:last-child td { border-bottom: none; }
.tbl .num { text-align: right; font-variant-numeric: tabular-nums; }
.tbl .ops { white-space: nowrap; }
.mono { font-variant-numeric: tabular-nums; font-size: 11px; }
.dim { color: var(--text-faint); font-size: 11px; }
.frozen { color: var(--text-faint); font-size: 10px; }
.tfa {
  display: inline-block;
  margin-left: 4px;
  font-size: 9px;
  padding: 1px 4px;
  border-radius: 3px;
  background: color-mix(in srgb, var(--success, #1a7f4b) 14%, transparent);
  color: var(--success, #1a7f4b);
}
.op {
  font-size: 11px;
  padding: 3px 8px;
  margin-right: 4px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--surface);
  color: var(--text-muted);
  cursor: pointer;
  font-family: inherit;
}
.op:hover { color: var(--text); border-color: var(--brand); }
.op.danger:hover {
  color: var(--danger, #d33);
  border-color: color-mix(in srgb, var(--danger, #d33) 50%, transparent);
}

.pager {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-top: 12px;
}
.hint { font-size: 11px; color: var(--text-faint); margin: 12px 0 0; line-height: 1.7; }
</style>
