<script setup lang="ts">
/**
 * SecurityView · 安全设置（v0.26.1）
 *
 * ★ 契约：
 *   POST /api/me/password          { old_password, new_password }
 *   GET  /api/me/sessions          → { items: [{id, ip, ua, current, ...}], count }
 *   POST /api/me/sessions/revoke   { session_id }
 *   POST /api/me/sessions/revoke-all
 *   GET  /api/me/login-records     → { items: [{ip, user_agent, success, reason, created_at}] }
 *   POST /api/me/2fa/setup         → { secret, otpauth_url, qr_svg? }
 *   POST /api/me/2fa/enable        { code } → { ok, recovery_codes? }
 *   POST /api/me/2fa/disable       { code }
 *   GET/POST /api/me/delete-status|delete-request|delete-cancel
 *
 * ★ 安全设计要点：
 *   ① 改密码后**旧令牌立即失效**（后端 token_valid_after 机制）→ 提示用户重新登录
 *   ② 高危操作（吊销全部会话 / 注销账号）走二次确认
 *   ③ 恢复码只显示一次 → 必须让用户当场保存
 */
import { computed, onMounted, ref } from 'vue'
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'
import AppInput from '@/components/AppInput.vue'
import AppTag from '@/components/AppTag.vue'
import AppModal from '@/components/AppModal.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import { useUiStore } from '@/stores/ui'
import { useAuthStore } from '@/stores/auth'
import { meApi, type LoginRecord, type LoginSession } from '@/api/modules/me'

const ui = useUiStore()
const auth = useAuthStore()

const loading = ref(true)
const sessions = ref<LoginSession[]>([])
const records = ref<LoginRecord[]>([])

// 改密码
const pw = ref({ old: '', neu: '', confirm: '' })
const pwBusy = ref(false)
const pwErr = ref('')

// 2FA
const tfa = ref<{ enabled: boolean; setup?: { secret: string; otpauth_url: string; qr_svg?: string } }>(
  { enabled: false })
const tfaCode = ref('')
const tfaBusy = ref(false)
const recoveryCodes = ref<string[]>([])

// 注销
const delStatus = ref<{ pending: boolean; execute_at?: string }>({ pending: false })
const delReason = ref('')

// 弹窗
const confirmBox = ref<{ open: boolean; title: string; body: string; danger: boolean; onOk: () => void }>({
  open: false, title: '', body: '', danger: false, onOk: () => {},
})

const pwOk = computed(() =>
  pw.value.old.length >= 1 &&
  pw.value.neu.length >= 8 &&
  pw.value.neu === pw.value.confirm)

async function load() {
  loading.value = true
  try {
    const [s, r, p] = await Promise.all([
      meApi.sessions().catch(() => ({ items: [], count: 0 })),
      meApi.loginRecords(20).catch(() => ({ items: [] })),
      meApi.profile().catch(() => null),
    ])
    sessions.value = s.items || []
    records.value = r.items || []
    tfa.value.enabled = Boolean(p?.totp_enabled)
    delStatus.value = await meApi.deleteStatus().catch(() => ({ pending: false }))
  } finally {
    loading.value = false
  }
}

async function changePassword() {
  pwErr.value = ''
  if (pw.value.neu.length < 8) { pwErr.value = '新密码至少 8 位'; return }
  if (pw.value.neu !== pw.value.confirm) { pwErr.value = '两次输入的新密码不一致'; return }
  pwBusy.value = true
  try {
    await meApi.changePassword(pw.value.old, pw.value.neu)
    pw.value = { old: '', neu: '', confirm: '' }
    ui.ok('密码已修改，请重新登录')
    // ★ 后端会让旧令牌立即失效 → 主动登出，避免用户遇到"点了没反应"
    setTimeout(() => auth.logout(), 1200)
  } catch (e) {
    pwErr.value = (e as Error).message || '修改失败'
  } finally {
    pwBusy.value = false
  }
}

async function revokeOne(s: LoginSession) {
  if (s.current) { ui.warn('当前设备请用「退出登录」'); return }
  try {
    await meApi.revokeSession(s.id)
    ui.ok('已下线该设备')
    await load()
  } catch (e) { ui.err((e as Error).message || '操作失败') }
}

function revokeAll() {
  confirmBox.value = {
    open: true,
    title: '下线所有其他设备',
    body: '除当前设备外的所有登录会话都会被吊销。<br>'
      + '<b>如果怀疑账号被盗，建议同时修改密码。</b>',
    danger: true,
    onOk: async () => {
      try {
        await meApi.revokeAllSessions()
        ui.ok('已下线所有其他设备')
        await load()
      } catch (e) { ui.err((e as Error).message || '操作失败') }
    },
  }
}

async function setup2fa() {
  tfaBusy.value = true
  try {
    const r = await meApi.setup2fa()
    tfa.value.setup = r
    tfaCode.value = ''
  } catch (e) {
    ui.err((e as Error).message || '初始化失败')
  } finally { tfaBusy.value = false }
}

async function enable2fa() {
  if (tfaCode.value.trim().length < 6) { ui.warn('请输入 6 位验证码'); return }
  tfaBusy.value = true
  try {
    const r = await meApi.enable2fa(tfaCode.value.trim())
    recoveryCodes.value = r.recovery_codes || []
    tfa.value.enabled = true
    tfa.value.setup = undefined
    ui.ok('二次验证已开启')
    await load()
  } catch (e) {
    ui.err((e as Error).message || '验证码不正确')
  } finally { tfaBusy.value = false }
}

async function disable2fa() {
  if (tfaCode.value.trim().length < 6) { ui.warn('请输入 6 位验证码'); return }
  tfaBusy.value = true
  try {
    await meApi.disable2fa(tfaCode.value.trim())
    tfa.value.enabled = false
    tfaCode.value = ''
    ui.ok('二次验证已关闭')
  } catch (e) {
    ui.err((e as Error).message || '验证码不正确')
  } finally { tfaBusy.value = false }
}

function requestDelete() {
  confirmBox.value = {
    open: true,
    title: '注销账号',
    body: '注销后有 <b>冷静期</b>，期间可随时取消。<br>'
      + '冷静期结束后：作品将<b>匿名化保留</b>，账号数据<b>不可恢复</b>。<br><br>'
      + '<span class="muted">建议先导出一份个人数据备份。</span>',
    danger: true,
    onOk: async () => {
      try {
        const r = await meApi.requestDelete(delReason.value)
        ui.ok('已提交注销申请')
        delStatus.value = { pending: true, execute_at: r.execute_at }
      } catch (e) { ui.err((e as Error).message || '提交失败') }
    },
  }
}

async function cancelDelete() {
  try {
    await meApi.cancelDelete()
    ui.ok('已取消注销申请')
    delStatus.value = { pending: false }
  } catch (e) { ui.err((e as Error).message || '取消失败') }
}

async function exportData() {
  try {
    const r = await meApi.exportData()
    if (r.download_url) {
      window.open(r.download_url, '_blank')
    } else {
      // 后端返回 zip 流，用带鉴权的下载
      const blob = await (await import('@/api/http')).default.download('/api/me/export')
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `sozeer-data-${new Date().toISOString().slice(0, 10)}.zip`
      a.click()
      URL.revokeObjectURL(url)
    }
    ui.ok('数据已开始下载')
  } catch (e) { ui.err((e as Error).message || '导出失败') }
}

function fmt(s?: string) {
  return s ? s.replace('T', ' ').slice(0, 16) : '—'
}

onMounted(load)
</script>

<template>
  <div class="page">
    <header class="page__head">
      <h1 class="page__title">安全设置</h1>
      <p class="page__sub">密码 · 登录设备 · 二次验证 · 账号注销</p>
    </header>

    <AppSkeleton v-if="loading" variant="card" :count="3" />

    <template v-else>
      <!-- 修改密码 -->
      <AppCard title="修改密码" subtitle="修改后所有设备需重新登录">
        <div class="grid">
          <AppInput v-model="pw.old" label="当前密码" type="password"
                    autocomplete="current-password" />
          <AppInput v-model="pw.neu" label="新密码" type="password"
                    autocomplete="new-password" hint="至少 8 位" />
          <AppInput v-model="pw.confirm" label="确认新密码" type="password"
                    autocomplete="new-password" />
          <p v-if="pwErr" class="err">{{ pwErr }}</p>
        </div>
        <template #footer>
          <AppButton variant="primary" :loading="pwBusy" :disabled="!pwOk"
                     @click="changePassword">修改密码</AppButton>
          <span v-if="!pwOk && (pw.old || pw.neu || pw.confirm)" class="muted tiny">
            请确认新密码至少 8 位且两次一致
          </span>
        </template>
      </AppCard>

      <!-- 二次验证 -->
      <AppCard title="二次验证（TOTP）" class="mt">
        <template v-if="!tfa.enabled && !tfa.setup">
          <p class="muted">开启后，登录时需额外输入验证器 App 生成的 6 位动态码。</p>
          <AppTag kind="warning" dot>当前未开启</AppTag>
          <div class="actions" style="margin-top:12px">
            <AppButton variant="primary" :loading="tfaBusy" @click="setup2fa">
              开始设置
            </AppButton>
          </div>
        </template>

        <template v-else-if="tfa.setup">
          <p class="muted">① 用验证器 App（Google Authenticator / 1Password 等）扫描：</p>
          <div class="qr">
            <div v-if="tfa.setup.qr_svg" v-html="tfa.setup.qr_svg" />
            <code v-else class="secret">{{ tfa.setup.secret }}</code>
          </div>
          <p class="muted tiny">无法扫码？手动输入密钥：<code>{{ tfa.setup.secret }}</code></p>
          <p class="muted" style="margin-top:12px">② 输入 App 显示的 6 位验证码：</p>
          <div class="inline">
            <AppInput v-model="tfaCode" placeholder="000000" :maxlength="6"
                      style="max-width:160px" />
            <AppButton variant="primary" :loading="tfaBusy" @click="enable2fa">确认开启</AppButton>
            <AppButton variant="ghost" @click="tfa.setup = undefined">取消</AppButton>
          </div>
        </template>

        <template v-else>
          <AppTag kind="success" dot>已开启</AppTag>
          <p class="muted" style="margin-top:12px">
            如需关闭，请输入当前验证码：
          </p>
          <div class="inline">
            <AppInput v-model="tfaCode" placeholder="000000" :maxlength="6"
                      style="max-width:160px" />
            <AppButton variant="danger" :loading="tfaBusy" @click="disable2fa">关闭二次验证</AppButton>
          </div>
        </template>

        <div v-if="recoveryCodes.length" class="recovery">
          <h4>⚠️ 请立即保存恢复码（只显示这一次）</h4>
          <p class="muted tiny">丢失验证器时用它们登录，每个只能用一次。</p>
          <div class="codes">
            <code v-for="c in recoveryCodes" :key="c">{{ c }}</code>
          </div>
          <AppButton size="sm" @click="recoveryCodes = []">我已保存</AppButton>
        </div>
      </AppCard>

      <!-- 登录设备 -->
      <AppCard title="登录设备" :subtitle="`共 ${sessions.length} 台`" class="mt">
        <AppEmpty v-if="!sessions.length" title="暂无登录会话" />
        <ul v-else class="sessions">
          <li v-for="s in sessions" :key="s.id" class="sess">
            <div class="sess__main">
              <div class="sess__ua">
                {{ (s.user_agent || s.ua || '未知设备').slice(0, 60) }}
                <AppTag v-if="s.current" kind="brand" size="sm">当前设备</AppTag>
              </div>
              <div class="muted tiny">
                IP {{ s.ip || '—' }} · 登录 {{ fmt(s.created_at) }}
              </div>
            </div>
            <AppButton v-if="!s.current" size="sm" @click="revokeOne(s)">下线</AppButton>
          </li>
        </ul>
        <template #footer>
          <AppButton variant="danger" size="sm" :disabled="sessions.length <= 1"
                     @click="revokeAll">下线所有其他设备</AppButton>
        </template>
      </AppCard>

      <!-- 登录记录 -->
      <AppCard title="登录记录" subtitle="最近 20 条（IP 已脱敏）" class="mt">
        <AppEmpty v-if="!records.length" title="暂无记录" />
        <table v-else class="tbl">
          <thead><tr><th>时间</th><th>IP</th><th>设备</th><th>结果</th></tr></thead>
          <tbody>
            <tr v-for="(r, i) in records" :key="r.id || i">
              <td class="muted tiny">{{ fmt(r.created_at) }}</td>
              <td class="muted tiny">{{ r.ip || '—' }}</td>
              <td class="muted tiny">{{ (r.user_agent || '').slice(0, 40) || '—' }}</td>
              <td>
                <AppTag :kind="r.success ? 'success' : 'danger'" size="sm">
                  {{ r.success ? '成功' : (r.reason || '失败') }}
                </AppTag>
              </td>
            </tr>
          </tbody>
        </table>
      </AppCard>

      <!-- 数据与注销 -->
      <AppCard title="数据与账号" class="mt">
        <div class="row">
          <div>
            <h4>导出我的数据</h4>
            <p class="muted tiny">打包下载你的作品、章节与账号信息（个人信息可携带权）。</p>
          </div>
          <AppButton @click="exportData">导出</AppButton>
        </div>

        <div class="row danger-row">
          <div>
            <h4>注销账号</h4>
            <p class="muted tiny">
              <template v-if="delStatus.pending">
                已提交申请，预计执行时间：{{ fmt(delStatus.execute_at) }}。
                冷静期内可随时取消。
              </template>
              <template v-else>
                注销后不可恢复。作品会被匿名化保留。
              </template>
            </p>
          </div>
          <AppButton v-if="delStatus.pending" @click="cancelDelete">取消注销</AppButton>
          <AppButton v-else variant="danger" @click="requestDelete">申请注销</AppButton>
        </div>
      </AppCard>
    </template>

    <!-- 二次确认 -->
    <AppModal v-model="confirmBox.open" :title="confirmBox.title"
              :danger="confirmBox.danger"
              ok-text="确认执行" @ok="confirmBox.onOk()">
      <div v-html="confirmBox.body" />
    </AppModal>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 820px; margin: 0 auto; }
.page__head { margin-bottom: var(--sp-5); }
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); }
.grid { display: flex; flex-direction: column; gap: var(--sp-3); }
.mt { margin-top: var(--sp-4); }
.actions { display: flex; gap: var(--sp-2); }
.inline { display: flex; gap: var(--sp-2); align-items: flex-start; }
.err { color: var(--danger); font-size: var(--fs-sm); margin: 0; }
.qr { margin: var(--sp-3) 0; display: inline-block; padding: var(--sp-3);
  background: #fff; border-radius: 8px; }
.secret { font-size: var(--fs-md); letter-spacing: .1em; }
code { background: var(--surface-2); padding: 2px 6px; border-radius: 4px;
  font-size: var(--fs-xs); }
.recovery { margin-top: var(--sp-4); padding: var(--sp-3);
  border: 1px solid var(--warning); border-radius: 8px; background: var(--surface-2); }
.recovery h4 { margin: 0 0 var(--sp-2); font-size: var(--fs-sm); }
.codes { display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px; margin: var(--sp-3) 0; }
.sessions { list-style: none; margin: 0; padding: 0; }
.sess { display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-3); padding: var(--sp-3) 0; border-bottom: 1px solid var(--border); }
.sess:last-child { border-bottom: none; }
.sess__ua { font-size: var(--fs-sm); display: flex; align-items: center; gap: 8px; }
.tbl { width: 100%; border-collapse: collapse; }
.tbl th { text-align: left; font-size: var(--fs-xs); color: var(--text-faint);
  padding: 6px 4px; border-bottom: 1px solid var(--border); }
.tbl td { padding: 8px 4px; border-bottom: 1px solid var(--border); }
.row { display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-4); padding: var(--sp-3) 0; }
.row + .row { border-top: 1px solid var(--border); }
.row h4 { margin: 0 0 4px; font-size: var(--fs-sm); }
.danger-row h4 { color: var(--danger); }
@media (max-width: 520px) {
  .codes { grid-template-columns: 1fr; }
  .inline { flex-direction: column; }
}
</style>
