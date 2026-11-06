<script setup lang="ts">
/**
 * AdminConfigView.vue · 配置中心（v0.26.3 G0-6 续）
 *
 * ★ 对照旧版 RENDER.config + renderConfigPage 核对（旧版 341 行，最复杂之一）。
 *
 * ★★ 配置中心的难点不是「能改」而是「改得可控」（MEMORY §53）：
 *   ① **diff 先行** —— 保存前让管理员看清「会改什么」
 *   ② **历史 + 回滚** —— 改错能退回
 *   ③ **高危项填原因** —— 审计留痕
 *   ④ **注册表是唯一声明处** —— 界面/校验/文档都从它派生
 *
 * ★ 优先级必须显式展示：**DB（界面）> 环境变量 > 注册表默认值**
 *   —— 否则运维改了 .env 却不生效会查很久。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppPage from '@/components/AdminPage.vue'
import AppCard from '@/components/AppCard.vue'
import AppTag from '@/components/AppTag.vue'
import AppButton from '@/components/AppButton.vue'
import AppInput from '@/components/AppInput.vue'
import AppModal from '@/components/AppModal.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import AppTabs from '@/components/AppTabs.vue'
import { useUiStore } from '@/stores/ui'
import { useAdminStore } from '@/stores/admin'
import {
  configApi, payChannelsApi,
  type ConfigItem, type ConfigMeta, type ConfigHistoryRow,
  type PayChannel, type PayChannelTestResult,
} from '@/api/admin_modules_extra'

const route = useRoute()
const router = useRouter()
const ui = useUiStore()
const adm = useAdminStore()

const canEdit = computed(() => adm.can('config.update'))

const loading = ref(true)
const err = ref('')
// ★ v1.0-E4.4：新增 overview（能力总览）与 payment（支付渠道）两个标签页
const tab = ref<'overview' | 'values' | 'mail' | 'payment' | 'history'>('overview')

const items = ref<ConfigItem[]>([])
const meta = ref<ConfigMeta | null>(null)
const history = ref<ConfigHistoryRow[]>([])
const group = ref('')

/** 本地编辑草稿（key → 新值）；只有改动过的才提交 */
const draft = ref<Record<string, string>>({})

const groups = computed(() => meta.value?.groups || [])

/**
 * ★★ v1.0-E4.4：高级项折叠状态。
 *
 * ★ 为什么需要（这是"配置太复杂"的直接解法）：
 *   运营打开配置中心看到 `model.role_writer = writer` 会一脸茫然 ——
 *   它们不是"可调的业务参数"，而是"改错了会出事的技术常量"。
 *
 *   → 默认**只显示运营能理解的项**，技术项折叠在「显示高级项」里。
 *     ★ 折叠不是隐藏 —— 需要时仍然能展开，且给出"该去哪儿改"的提示。
 */
const showAdvanced = ref(false)

/** 当前分组下的项（按折叠状态过滤） */
const visible = computed(() => {
  const base = group.value
    ? items.value.filter((i) => i.group === group.value)
    : items.value
  return showAdvanced.value ? base : base.filter((i) => !i.advanced)
})

/** 被折叠起来的项数（用于提示"还有 N 项高级配置"） */
const hiddenAdvancedCount = computed(() => {
  const base = group.value
    ? items.value.filter((i) => i.group === group.value)
    : items.value
  return base.filter((i) => i.advanced).length
})

/**
 * ★★ 能力入口卡片（v1.0-E4.4 核心改动）。
 *
 * 为什么需要：配置中心此前是"56 项扁平表格"，运营看不到**全貌** ——
 * 「模型配置」「套餐管理」「支付渠道」这些**真正的业务能力**
 * 要么在别的专页（没有入口），要么根本不存在（支付）。
 *
 * ★ 判据：**配置中心应该是"能配什么"的总入口**，而不是"所有配置项的堆砌"。
 */
interface Capability {
  key: string
  icon: string
  title: string
  desc: string
  /** 跳转目标（vue-router name 或 tab key） */
  to?: { name: string } | null
  /** 站内 tab（不跳路由，直接切标签页） */
  tabKey?: string
  /** 状态文字（如"4 个套餐"） */
  status?: string
  /** 是否需要关注（如未配置） */
  warn?: boolean
}

const capabilities = computed<Capability[]>(() => [
  {
    key: 'models', icon: '🤖', title: '模型配置',
    desc: '接入云端模型（OpenAI 兼容 / ComfyUI），配置密钥与按角色路由',
    to: { name: 'admin-models' },
    status: '含套餐路由',
  },
  {
    key: 'plans', icon: '💳', title: '套餐与价格',
    desc: '定义套餐价格、周期、积分额度与功能对比（价格页自动同步）',
    to: { name: 'admin-plans' },
  },
  {
    key: 'payment', icon: '🏦', title: '支付渠道',
    desc: '开启微信 / 支付宝 / Stripe，配置商户凭据与回调地址',
    tabKey: 'payment',
    warn: !payChannels.value.some((c) => c.enabled && c.name !== 'sandbox'),
    status: payChannels.value.length
      ? `${payChannels.value.filter((c) => c.enabled).length} 个已开启`
      : '',
  },
  {
    key: 'mail', icon: '📧', title: '邮件与站点',
    desc: 'SMTP 发信、ICP 备案号、联系邮箱、注册开关',
    tabKey: 'mail',
  },
  {
    key: 'features', icon: '🎚️', title: '功能开关',
    desc: '模块级开关（草稿 / 短剧 / 一致性检查等）',
    tabKey: 'values',
  },
  {
    key: 'security', icon: '🔒', title: '安全与限流',
    desc: '登录限流、令牌有效期、密码策略',
    tabKey: 'values',
  },
  {
    key: 'cost', icon: '📊', title: '成本控制',
    desc: 'token 上限、突增告警、额度预警阈值',
    tabKey: 'values',
  },
  {
    key: 'advanced', icon: '⚙️', title: '高级参数',
    desc: '模型超时、重试次数、档位映射等技术项',
    tabKey: 'values',
  },
  {
    // ★★ v1.0-E4.7：把"采集到的数据"变成可看的洞察
    //   ★ 为什么放配置中心：运营调完参数后，最想知道"改了有没有变好" ——
    //     在这里给入口，形成"配置 → 观察 → 再调"的闭环
    key: 'insight', icon: '📈', title: '数据洞察',
    desc: '提示词版本对比、采纳漏斗 —— 回答「改了 prompt 有没有变好」',
    to: { name: 'admin-insight' },
  },
])

/* ── 支付渠道（v1.0-E4.4）── */
const payChannels = ref<PayChannel[]>([])
const payLoading = ref(false)
const paySaving = ref('')
const payTesting = ref('')
const payTestResult = ref<PayChannelTestResult | null>(null)
/** ★ 凭据草稿（留空 = 不修改，所以初始都是空串） */
const payDraft = ref<Record<string, Record<string, string>>>({})

async function loadPay() {
  payLoading.value = true
  try {
    const r = await payChannelsApi.list()
    payChannels.value = r.items || []
    // ★ 草稿初始化为空 —— 界面**不回显**已有凭据
    payDraft.value = Object.fromEntries(
      (r.items || []).map((c) => [c.name, {}]))
  } catch (e) {
    ui.err((e as Error).message || '读取支付渠道失败')
  } finally {
    payLoading.value = false
  }
}

async function savePayChannel(c: PayChannel) {
  if (!canEdit.value) return
  const creds = payDraft.value[c.name] || {}
  // ★ 只提交填了值的字段（留空 = 不修改）
  const filled = Object.fromEntries(
    Object.entries(creds).filter(([, v]) => String(v || '').trim()))
  paySaving.value = c.name
  try {
    await payChannelsApi.save(c.name, { enabled: c.enabled, creds: filled })
    ui.ok(`「${PAY_LABEL[c.name] || c.name}」已保存`)
    await loadPay()
  } catch (e) {
    ui.err((e as Error).message || '保存失败')
  } finally {
    paySaving.value = ''
  }
}

async function testPayChannel(c: PayChannel) {
  payTesting.value = c.name
  payTestResult.value = null
  try {
    payTestResult.value = await payChannelsApi.test(c.name)
    if (payTestResult.value.ok) ui.ok('自检通过')
    else ui.warn('自检发现问题，见下方明细')
  } catch (e) {
    ui.err((e as Error).message || '自检失败')
  } finally {
    payTesting.value = ''
  }
}

/** 渠道展示名 */
const PAY_LABEL: Record<string, string> = {
  sandbox: '沙箱支付', wechat: '微信支付', alipay: '支付宝', stripe: 'Stripe',
}

/** ★ 只提交真正改过的项（避免"打开页面就写回全部配置"） */
const changedKeys = computed(() => {
  const out: string[] = []
  for (const [k, v] of Object.entries(draft.value)) {
    const cur = items.value.find((i) => i.key === k)
    if (!cur) continue
    if (String(cur.value ?? '') !== String(v)) out.push(k)
  }
  return out
})

const dirty = computed(() => changedKeys.value.length > 0)

/* ── diff 弹窗 ── */
const diffOpen = ref(false)
const diffRows = ref<Array<{ key: string; old: unknown; new: unknown; risk?: string; changed: boolean }>>([])
const reason = ref('')
const rbReason = ref('')
const saving = ref(false)

/** ★ 高危配置项（需要额外确认 + 强制原因） */
const riskyCount = computed(() =>
  diffRows.value.filter((r) => r.risk && r.risk !== 'low').length)

async function openDiff() {
  if (!dirty.value) return
  const changes: Record<string, unknown> = {}
  for (const k of changedKeys.value) changes[k] = draft.value[k]
  try {
    const r = await configApi.diff(changes)
    diffRows.value = (r.changes || []).filter((x) => x.changed !== false)
    reason.value = ''
    diffOpen.value = true
  } catch (e) {
    ui.err((e as Error).message || '生成 diff 失败')
  }
}

async function applyDiff() {
  if (reason.value.trim().length < 2) { ui.err('请填写变更原因（至少 2 个字符）'); return }
  saving.value = true
  try {
    const changes: Record<string, unknown> = {}
    for (const k of changedKeys.value) changes[k] = draft.value[k]
    await configApi.save(changes, reason.value.trim())
    ui.ok('配置已保存')
    diffOpen.value = false
    draft.value = {}
    await load()
  } catch (e) {
    ui.err((e as Error).message || '保存失败')
  } finally {
    saving.value = false
  }
}

/* ── 回滚 ── */
const pendingRollback = ref<ConfigHistoryRow | null>(null)
const rollbackOpen = computed({
  get: () => pendingRollback.value !== null,
  set: (v: boolean) => { if (!v) pendingRollback.value = null },
})

async function loadHistory() {
  try {
    const r = await configApi.history('', 100)
    history.value = r.items || []
  } catch {
    history.value = []
  }
}

async function doRollback(reasonText: string) {
  const h = pendingRollback.value
  if (!h || !h.id) return
  saving.value = true
  try {
    await configApi.rollback(h.id, reasonText)
    ui.ok('已回滚')
    pendingRollback.value = null
    await load()
    await loadHistory()
  } catch (e) {
    ui.err((e as Error).message || '回滚失败')
  } finally {
    saving.value = false
  }
}

function setVal(key: string, v: string) {
  draft.value = { ...draft.value, [key]: v }
}

function valOf(item: ConfigItem): string {
  const k = item.key
  if (k in draft.value) return draft.value[k]
  const v = item.value
  return v === null || v === undefined ? '' : String(v)
}

/* ════════ ★★ v1.0-E4.15：类型化渲染 ════════
 *
 * ★ 用户诉求："有些栏目不明白怎么填写或者修改，把能做成下拉的改成下拉"。
 *
 * 根因：此前 values 表所有项一律渲染纯文本框 ——
 *   · choice 项（如公告级别）要用户"猜"合法值是什么
 *   · bool 项要手敲 true/false（敲 "True"/"1" 全靠后端宽松转换兜底）
 *   · 数值项看不出合法范围，超范围要等保存时才报错
 *   · text 项（全站公告）单行放不下多行内容
 *
 * ★ 渲染判据全来自后端注册表（单一事实源）：
 *   it.type + it.choices + it.min/max + it.unit —— 前端不硬编码任何 key。
 */

/** 当前项是否为布尔（★ 读 it.type 而不是猜值内容） */
function isBool(it: ConfigItem): boolean {
  return it.type === 'bool'
}

/** 当前项是否为枚举下拉 */
function isChoice(it: ConfigItem): boolean {
  return it.type === 'choice' && Array.isArray(it.choices) && it.choices.length > 0
}

/** 当前项是否为数值（int/float） */
function isNumber(it: ConfigItem): boolean {
  return it.type === 'int' || it.type === 'float'
}

/** 当前项是否为多行文本 */
function isText(it: ConfigItem): boolean {
  return it.type === 'text'
}

/** 当前项是否只读（系统维护） */
function isReadonly(it: ConfigItem): boolean {
  return it.readonly === true || it.type === 'secret'
}

/**
 * ★ 数值范围提示（如「5 ~ 900 秒」）。
 *   保存关口后端会校验，但**填的时候就能看到范围**才不浪费一次提交。
 */
function numHint(it: ConfigItem): string {
  const parts: string[] = []
  if (it.min !== null && it.min !== undefined) parts.push(String(it.min))
  else parts.push('−∞')
  if (it.max !== null && it.max !== undefined) parts.push(String(it.max))
  else parts.push('+∞')
  const range = `${parts[0]} ~ ${parts[1]}${it.unit ? ' ' + it.unit : ''}`
  return `范围：${range}`
}

/**
 * ★★ 枚举值的中文含义映射（下拉选项的辅助说明）。
 *
 * 为什么前端映射而不是后端：choice 值是稳定契约（存 DB 的就是它），
 * 中文含义是**展示层**的关注点，放前端不污染注册表。
 * 后端 choices 新增值时这里查不到 → 显示原值（fail-soft，不空白）。
 */

/** 档位值 → 中文含义（与 model_router.ROLES 对齐） */
const ROLE_CHOICE_LABELS: Record<string, string> = {
  default: '通用（未指定角色时使用）',
  planner: '规划（大纲/任务单）',
  writer: '写作（正文生成）',
  critic: '判定与摘要（质检/压缩）',
  script: '剧本（小说改编短剧）',
  storyboard: '分镜（八段提示词细化）',
  image: '图片生成（参考图/分镜图）',
  video: '视频生成（分镜视频/成片）',
  tts: '语音合成（配音/音色）',
  embedding: '向量嵌入（语义检索）',
}

const CHOICE_LABELS: Record<string, Record<string, string>> = {
  'system.announcement_level': {
    info: '蓝色 · 一般通知',
    warning: '黄色 · 重要提醒',
    danger: '红色 · 紧急/停服',
  },
  'system.default_role': {
    user: '普通用户（只用写作功能）',
    editor: '编辑（含内容审核协作权限）',
  },
  'payment.currency': {
    CNY: '人民币 ¥',
    USD: '美元 $',
    HKD: '港币 HK$',
  },
  // 档位类（model.role_*）：含义来自 model_router.ROLES 的中文说明
  'model.role_writer': ROLE_CHOICE_LABELS,
  'model.role_critic': ROLE_CHOICE_LABELS,
  'model.role_planner': ROLE_CHOICE_LABELS,
  'model.role_utility': ROLE_CHOICE_LABELS,
  'model.free_tier_role': ROLE_CHOICE_LABELS,
}

/** 查枚举值的展示标签：映射表优先，查不到显示原值（fail-soft） */
function choiceLabel(it: ConfigItem, val: string): string {
  const m = CHOICE_LABELS[it.key]
  return (m && m[val]) || val
}

function resetAll() {
  draft.value = {}
  ui.ok('已放弃所有未保存改动')
}

const RISK_TAG: Record<string, 'danger' | 'warning' | 'default'> = {
  high: 'danger', medium: 'warning', low: 'default',
}

function switchTab(t: string) {
  tab.value = t as typeof tab.value
  if (tab.value === 'history') loadHistory()
  if (tab.value === 'mail') loadMail()
  // ★ v1.0-E4.4：切到支付页时按需加载（避免拖慢首屏）
  if (tab.value === 'payment') loadPay()
}

/** ★ 能力卡片点击：站内 tab 直接切，外部专页走路由 */
function onCapability(c: Capability) {
  if (c.tabKey) {
    switchTab(c.tabKey)
    // ★ 「功能开关」等需要定位到具体分组
    if (c.tabKey === 'values') {
      group.value = c.key === 'advanced' ? '' : ''
    }
  } else if (c.to) {
    router.push(c.to)
  }
}

/* ══════════════ ★ 邮件与站点（v1.0-E4.2）══════════════ */

interface MailStatus {
  configured: boolean
  dry_run: boolean
  host: string
  port: number
  user: string
  from_addr: string
  from_name: string
  secure: string
  has_password: boolean
  hint: string
}

const mailStatus = ref<MailStatus | null>(null)
const mailSaving = ref(false)
const mailTesting = ref(false)
const mailTestTo = ref('')

/** 表单草稿（★ 密码不回显 —— 留空表示"不修改"） */
const mail = ref({
  host: '', port: 465, user: '', password: '',
  from_addr: '', from_name: '', secure: '',
})

/** 站点与合规（走通用配置注册表） */
const site = ref({
  icp: '', contactEmail: '', announcement: '', allowRegister: true,
})
const siteSaving = ref(false)

async function loadMail() {
  try {
    const r = await configApi.mailStatus()
    mailStatus.value = r as MailStatus
    // ★ 用服务端值填充表单，但**不填密码**（只显示"已设置"占位）
    mail.value = {
      host: r.host || '',
      port: r.port || 465,
      user: r.user || '',
      password: '',
      from_addr: r.from_addr || '',
      from_name: r.from_name || '',
      secure: r.secure || '',
    }
  } catch (e) {
    ui.err((e as Error).message || '读取邮件状态失败')
  }
  await loadSite()
}

async function loadSite() {
  try {
    const l = await configApi.list('system')
    const map: Record<string, unknown> = {}
    for (const it of (l.items || []) as ConfigItem[]) map[it.key] = it.value
    site.value = {
      icp: String(map['system.icp'] ?? ''),
      contactEmail: String(map['system.contact_email'] ?? ''),
      announcement: String(map['system.announcement'] ?? ''),
      allowRegister: map['system.allow_register'] !== false
        && String(map['system.allow_register']) !== 'false',
    }
  } catch { /* fail-soft：站点信息读不到不影响 SMTP 配置 */ }
}

async function saveMail() {
  if (!canEdit.value) return
  mailSaving.value = true
  try {
    const r = await configApi.mailSave({
      host: mail.value.host.trim(),
      port: Number(mail.value.port) || 0,
      user: mail.value.user.trim(),
      password: mail.value.password,          // ★ 留空 = 不改
      from_addr: mail.value.from_addr.trim(),
      from_name: mail.value.from_name.trim(),
      secure: mail.value.secure,
    })
    mailStatus.value = (r as { status?: MailStatus }).status || mailStatus.value
    mail.value.password = ''                  // ★ 保存后清空密码框
    ui.ok('SMTP 配置已保存（立即生效）')
  } catch (e) {
    ui.err((e as Error).message || '保存失败')
  } finally {
    mailSaving.value = false
  }
}

async function testMail() {
  if (!mailTestTo.value) return
  mailTesting.value = true
  try {
    await configApi.mailTest(mailTestTo.value.trim())
    ui.ok(`测试邮件已发送至 ${mailTestTo.value}`)
  } catch (e) {
    ui.err((e as Error).message || '发送失败')
  } finally {
    mailTesting.value = false
  }
}

/** ★ 站点信息与注册开关：走通用配置保存（带原因，满足审计要求） */
async function saveSite() {
  if (!canEdit.value) return
  siteSaving.value = true
  try {
    await configApi.save({
      'system.icp': site.value.icp.trim(),
      'system.contact_email': site.value.contactEmail.trim(),
      'system.announcement': site.value.announcement,
    }, '后台配置站点信息与合规项')
    ui.ok('站点信息已保存')
  } catch (e) {
    ui.err((e as Error).message || '保存失败')
  } finally {
    siteSaving.value = false
  }
}

async function toggleRegister() {
  if (!canEdit.value) return
  const next = !site.value.allowRegister
  // ★ 开放注册是高风险操作（可能被刷号），要求二次确认
  if (next && !window.confirm(
      '开放注册后，任何人都可以自助注册账号。\n确认要开放吗？')) return
  siteSaving.value = true
  try {
    await configApi.save({ 'system.allow_register': next },
                         next ? '开放注册' : '关闭注册')
    site.value.allowRegister = next
    ui.ok(next ? '已开放注册' : '已关闭注册')
  } catch (e) {
    ui.err((e as Error).message || '操作失败')
  } finally {
    siteSaving.value = false
  }
}

async function load() {
  loading.value = true
  err.value = ''
  try {
    const [l, m] = await Promise.all([
      configApi.list(group.value),
      configApi.meta().catch(() => null),
    ])
    items.value = (l.items || []) as ConfigItem[]
    meta.value = m
  } catch (e) {
    items.value = []
    err.value = (e as Error).message || '加载配置失败'
  } finally {
    loading.value = false
  }
}

async function exportCfg() {
  try {
    await configApi.exportCfg()
    ui.ok('已开始下载')
  } catch (e) {
    ui.err((e as Error).message || '导出失败')
  }
}

onMounted(async () => {
  await load()
  // ★ v1.0-E4.4：总览页需要支付渠道状态（用于卡片上的"N 个已开启"）
  loadPay()
})
watch(() => route.query._r, () => { load() })
</script>

<template>
  <AppPage
    title="配置中心"
    subtitle="配置有优先级：DB（界面）> 环境变量 > 注册表默认值。改环境变量不生效是正常的 —— 界面值优先。"
    :loading="loading"
    :error="err"
  >
    <template #actions>
      <AppTag v-if="!canEdit" size="sm" kind="warning">只读（需 config.update）</AppTag>
      <AppTag v-if="meta" size="sm">{{ meta.total }} 项</AppTag>
      <AppTag v-if="dirty" size="sm" kind="warning">{{ changedKeys.length }} 项待保存</AppTag>
      <AppButton size="sm" variant="ghost" @click="exportCfg">导出</AppButton>
      <AppButton v-if="canEdit && dirty" size="sm" variant="ghost" @click="resetAll">放弃改动</AppButton>
      <AppButton
        v-if="canEdit" size="sm" variant="primary"
        :disabled="!dirty" @click="openDiff"
      >查看变更并保存</AppButton>
    </template>

    <template #toolbar>
      <AppTabs
        :model-value="tab"
        :items="[
          { key: 'overview', label: '能力总览' },
          { key: 'values', label: '全部配置项' },
          { key: 'payment', label: '支付渠道' },
          { key: 'mail', label: '邮件与站点' },
          { key: 'history', label: '变更历史' },
        ]"
        @update:model-value="switchTab"
      />
      <template v-if="tab === 'values'">
        <select v-model="group" class="sel" @change="load">
          <option value="">全部分组</option>
          <option v-for="g in groups" :key="g.group" :value="g.group">{{ g.name }}</option>
        </select>
        <!-- ★★ v1.0-E4.4：高级项折叠开关 -->
        <label class="adv-toggle" :title="`还有 ${hiddenAdvancedCount} 项技术参数被折叠`">
          <input v-model="showAdvanced" type="checkbox" />
          <span>显示高级项</span>
          <span v-if="hiddenAdvancedCount" class="adv-toggle__n">
            +{{ hiddenAdvancedCount }}
          </span>
        </label>
      </template>
      <span class="sp" />
      <span class="hint-inline">
        缓存：<code>{{ meta?.cache ? JSON.stringify(meta.cache) : '—' }}</code>
      </span>
    </template>

    <!-- ══════════ ★★ 能力总览（v1.0-E4.4 新增 · 默认首页）══════════
         为什么要有这一页：
           配置中心此前是"56 项扁平表格"，运营看不到**全貌** ——
           「模型配置」「套餐管理」「支付渠道」这些真正的业务能力
           要么藏在别的专页（没入口），要么根本不存在。

         ★ 判据：**配置中心应该是"能配什么"的总入口**，
           而不是"所有配置项的堆砌"。
           表格留给"我要精确改某一项"的场景，总览负责"我该去哪儿配"。 -->
    <template v-if="!loading && tab === 'overview'">
      <div class="caps">
        <button
          v-for="c in capabilities" :key="c.key"
          class="cap" :class="{ 'cap--warn': c.warn }"
          type="button" @click="onCapability(c)"
        >
          <span class="cap__ico" aria-hidden="true">{{ c.icon }}</span>
          <span class="cap__body">
            <strong class="cap__title">{{ c.title }}</strong>
            <span class="cap__desc">{{ c.desc }}</span>
            <span v-if="c.status" class="cap__status">{{ c.status }}</span>
            <span v-else-if="c.warn" class="cap__status cap__status--warn">
              尚未配置真实渠道
            </span>
          </span>
          <span class="cap__go" aria-hidden="true">›</span>
        </button>
      </div>

      <AppCard title="配置优先级说明" class="mt">
        <p class="prio">
          <b>界面值</b> &gt; <b>环境变量</b> &gt; <b>注册表默认值</b>
        </p>
        <p class="prio-desc">
          ★ 改 <code>.env</code> 不生效是**正常的** —— 界面值优先。
          想让环境变量生效，需要先在界面里清空该项。
        </p>
        <p class="prio-desc">
          共 <b>{{ meta?.total }}</b> 项配置，其中
          <b>{{ items.filter(i => i.advanced).length }}</b> 项为技术参数（默认折叠）。
          「全部配置项」页可精确修改某一项，且**保存前会显示变更 diff**。
        </p>
      </AppCard>
    </template>

    <!-- ══════════ ★ 支付渠道（v1.0-E4.4 新增）══════════ -->
    <template v-if="!loading && tab === 'payment'">
      <AppCard title="支付渠道配置" class="mt">
        <p class="hint">
          ★ 开启渠道前必须**填全凭据** —— 否则用户点了会拿到"渠道不可用"错误，
          比根本不显示该渠道体验更差。
          <br>
          ★ 凭据**加密存储且不回显**（只显示"已设置"）；留空 = 不修改。
        </p>

        <div v-if="payLoading" class="state">正在读取…</div>

        <div v-else class="chans">
          <div v-for="c in payChannels" :key="c.name" class="chan">
            <div class="chan__hd">
              <label class="switch">
                <input v-model="c.enabled" type="checkbox" :disabled="!canEdit" />
                <strong>{{ PAY_LABEL[c.name] || c.name }}</strong>
              </label>
              <div class="chan__tags">
                <AppTag v-if="c.enabled" size="sm" kind="success">已开启</AppTag>
                <AppTag v-else size="sm">已关闭</AppTag>
                <AppTag v-if="!c.complete && c.name !== 'sandbox'" size="sm" kind="warning">
                  凭据不全
                </AppTag>
              </div>
            </div>

            <p v-if="c.name === 'sandbox'" class="chan__note">
              沙箱渠道无需凭据，用于开发演示。<strong>生产环境建议关闭。</strong>
            </p>

            <div v-else class="chan__fields">
              <div v-for="f in c.fields" :key="f.key" class="cfield">
                <label class="cfield__label">
                  {{ f.label }}
                  <span v-if="c.cred_state[f.key]" class="cfield__set">已设置</span>
                  <span v-else class="cfield__unset">未设置</span>
                </label>
                <!-- ★ 密码类字段：永远不回显，placeholder 提示"留空不改" -->
                <input
                  :type="f.secret ? 'password' : 'text'"
                  class="cfield__in"
                  :placeholder="c.cred_state[f.key] ? '（留空则不修改）' : f.desc"
                  :disabled="!canEdit"
                  :value="(payDraft[c.name] || {})[f.key] || ''"
                  @input="(e) => {
                    payDraft[c.name] = payDraft[c.name] || {}
                    payDraft[c.name][f.key] = (e.target as HTMLInputElement).value
                  }"
                />
                <span class="cfield__desc">{{ f.desc }}</span>
              </div>
            </div>

            <div class="chan__ops">
              <AppButton size="sm" variant="primary" :loading="paySaving === c.name"
                         :disabled="!canEdit" @click="savePayChannel(c)">
                保存
              </AppButton>
              <AppButton size="sm" variant="ghost" :loading="payTesting === c.name"
                         @click="testPayChannel(c)">
                自检
              </AppButton>
            </div>
          </div>
        </div>
      </AppCard>

      <!-- 自检结果 -->
      <AppCard v-if="payTestResult" :title="`自检结果 · ${PAY_LABEL[payTestResult.name] || payTestResult.name}`" class="mt">
        <ul class="checks">
          <li v-for="(k, i) in payTestResult.checks" :key="i"
              :class="{ 'is-bad': !k.ok }">
            <span class="checks__mark">{{ k.ok ? '✓' : '✗' }}</span>
            <span class="checks__item">{{ k.item }}</span>
            <span v-if="k.hint" class="checks__hint">{{ k.hint }}</span>
          </li>
        </ul>
        <p class="checks__note">{{ payTestResult.note }}</p>
      </AppCard>

      <!-- 危险开关 -->
      <AppCard title="⚠️ 高级选项" class="mt">
        <p class="hint hint--warn">
          ★★ <strong>跳过签名校验</strong>：开启后任何人都能伪造"支付成功"回调。
          仅用于本地调试，<strong>生产环境开启等于白送钱</strong>。
        </p>
        <p class="prio-desc">
          如需开启，请到「全部配置项」页搜索
          <code>payment.skip_signature_check</code>（该页会强制你填写变更原因）。
        </p>
      </AppCard>
    </template>

    <!-- ══════════ ★ 邮件与站点（v1.0-E4.2 新增）══════════
         为什么单独一个标签页：
         通用配置表（上面的"配置项"）只能改**注册表里声明过的**标量值，
         而 SMTP 需要「密码不明文回显」+「测试发信」+「组合校验」，
         站点信息需要「合规提示」—— 这些用通用表格表达不了。

         ★ 数据来源：SMTP 走 `/api/admin/mail/*`（加密存储，独立于注册表）；
           站点信息走通用配置（system.icp / system.contact_email）。 -->
    <template v-if="!loading && tab === 'mail'">
      <!-- SMTP -->
      <AppCard title="邮件发送（SMTP）" class="mt">
        <template #header-extra>
          <AppTag size="sm" :kind="mailStatus?.configured ? 'success' : 'warning'">
            {{ mailStatus?.configured ? '已配置' : '未配置' }}
          </AppTag>
        </template>

        <p class="hint">
          ★ 不配置时，通知类邮件会<strong>静默跳过</strong>（站内信不受影响）。
          配置后立即生效，<strong>无需重启服务</strong>。
        </p>

        <div class="mgrid">
          <AppInput v-model="mail.host" label="SMTP 服务器"
                    placeholder="smtp.exmail.qq.com" />
          <AppInput v-model.number="mail.port" label="端口" type="number"
                    placeholder="465" />
          <AppInput v-model="mail.user" label="账号"
                    placeholder="noreply@sozeer.com" />
          <AppInput v-model="mail.password" label="密码 / 授权码" type="password"
                    :placeholder="mailStatus?.has_password ? '已设置（留空则不修改）' : '未设置'" />
          <AppInput v-model="mail.from_addr" label="发件地址"
                    placeholder="留空则用账号" />
          <AppInput v-model="mail.from_name" label="发件人显示名"
                    placeholder="溯字 SOZEER" />
          <div class="mfield">
            <label class="mfield__label">加密方式</label>
            <select v-model="mail.secure" class="sel sel--wide">
              <option value="">自动（按端口推断）</option>
              <option value="ssl">SSL（465）</option>
              <option value="starttls">STARTTLS（587）</option>
              <option value="none">不加密（不推荐）</option>
            </select>
          </div>
        </div>

        <div v-if="mailStatus?.hint" class="hint hint--warn">{{ mailStatus.hint }}</div>

        <div class="mactions">
          <AppButton variant="primary" :loading="mailSaving"
                     :disabled="!canEdit" @click="saveMail">保存 SMTP 配置</AppButton>
          <div class="mtest">
            <AppInput v-model="mailTestTo" size="sm" placeholder="收件邮箱"
                      aria-label="测试收件人" />
            <AppButton variant="ghost" :loading="mailTesting"
                       :disabled="!canEdit || !mailTestTo" @click="testMail">
              发送测试邮件
            </AppButton>
          </div>
        </div>
      </AppCard>

      <!-- 站点与合规 -->
      <AppCard title="站点与合规" class="mt">
        <p class="hint">
          ★ <strong>ICP 备案号</strong>与<strong>联系邮箱</strong>会显示在页脚 ——
          在中国大陆提供服务属<strong>合规要求</strong>。
        </p>
        <div class="mgrid">
          <AppInput v-model="site.icp" label="ICP 备案号"
                    placeholder="京ICP备00000000号-1" />
          <AppInput v-model="site.contactEmail" label="联系邮箱"
                    placeholder="hello@sozeer.com" />
          <AppInput v-model="site.announcement" label="全站公告"
                    placeholder="留空则不显示" />
        </div>
        <div class="mactions">
          <AppButton variant="primary" :loading="siteSaving"
                     :disabled="!canEdit" @click="saveSite">保存站点信息</AppButton>
        </div>
      </AppCard>

      <!-- 注册开关（运营最常用，单独放显眼位置） -->
      <AppCard title="注册开关" class="mt">
        <p class="hint">
          ★ 当前状态：<strong :class="site.allowRegister ? 'ok' : 'warn'">
            {{ site.allowRegister ? '开放注册' : '已关闭注册' }}
          </strong>
          <span class="dim">（外部用户{{ site.allowRegister ? '可以' : '不能' }}自助注册）</span>
        </p>
        <div class="mactions">
          <AppButton :variant="site.allowRegister ? 'ghost' : 'primary'"
                     :loading="siteSaving" :disabled="!canEdit"
                     @click="toggleRegister">
            {{ site.allowRegister ? '关闭注册' : '开放注册' }}
          </AppButton>
        </div>
      </AppCard>
    </template>

    <!-- ══════════ 配置项 ══════════ -->
    <template v-if="!loading && tab === 'values'">
      <AppEmpty v-if="!visible.length" icon="⚙" title="没有匹配的配置项" />

      <AppCard v-else class="mt">
        <div class="tblwrap">
          <table class="tbl">
            <thead>
              <tr>
                <th style="width:230px">配置项</th>
                <th style="width:76px">风险</th>
                <th style="width:280px">当前值</th>
                <th style="width:90px">来源</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="it in visible" :key="it.key"
                  :class="{ 'is-advanced': it.advanced }">
                <td>
                  <code class="code">{{ it.key }}</code>
                  <div class="dim">{{ it.label || '' }}</div>
                  <!-- ★ 高级项标记：让运营知道"这不是给你调的" -->
                  <span v-if="it.advanced" class="adv-flag" title="技术参数，建议保持默认">
                    高级
                  </span>
                </td>
                <td>
                  <AppTag v-if="it.risk" size="sm" :kind="RISK_TAG[it.risk] || 'default'">
                    {{ meta?.risk_label?.[it.risk] || it.risk }}
                  </AppTag>
                  <span v-else class="dim">—</span>
                </td>
                <td>
                  <!-- ══ ★★ v1.0-E4.15：类型化渲染 ══
                       渲染方式全部由注册表 it.type 派生（不硬编码 key）：
                       choice → 下拉（含中文含义）/ bool → 开关 /
                       int|float → 数字输入+范围提示 / text → 多行 / 其余 → 文本框 -->
                  <!-- ① 枚举 → 下拉 -->
                  <select
                    v-if="isChoice(it)"
                    class="sel sel--wide"
                    :disabled="!canEdit || isReadonly(it)"
                    :value="valOf(it)"
                    @change="(e) => setVal(it.key, (e.target as HTMLSelectElement).value)"
                  >
                    <option v-if="!valOf(it)" value="">（未设置）</option>
                    <option v-for="c in (it.choices || [])" :key="c" :value="c">
                      {{ choiceLabel(it, c) }}
                    </option>
                  </select>

                  <!-- ② 布尔 → 开关（显示 开/关 而不是 true/false） -->
                  <label v-else-if="isBool(it)" class="boolrow" :class="{ 'is-off': !canEdit || isReadonly(it) }">
                    <input
                      type="checkbox"
                      class="boolrow__sw"
                      :checked="valOf(it) === '1' || valOf(it) === 'true' || valOf(it) === 'True'"
                      :disabled="!canEdit || isReadonly(it)"
                      @change="(e) => setVal(it.key, (e.target as HTMLInputElement).checked ? '1' : '0')"
                    />
                    <span class="boolrow__state" :class="valOf(it) === '1' || valOf(it) === 'true' || valOf(it) === 'True' ? 'on' : 'off'">
                      {{ (valOf(it) === '1' || valOf(it) === 'true' || valOf(it) === 'True') ? '已开启' : '已关闭' }}
                    </span>
                  </label>

                  <!-- ③ 数值 → 数字输入 + 范围提示 -->
                  <template v-else-if="isNumber(it)">
                    <AppInput
                      :model-value="valOf(it)"
                      type="number"
                      :step="it.type === 'float' ? '0.1' : '1'"
                      :min="(it.min ?? undefined) as number | undefined"
                      :max="(it.max ?? undefined) as number | undefined"
                      :disabled="!canEdit || isReadonly(it)"
                      :hint="numHint(it)"
                      @update:model-value="(v: string) => setVal(it.key, v)"
                    />
                  </template>

                  <!-- ④ 多行文本（公告等） -->
                  <textarea
                    v-else-if="isText(it)"
                    class="ta ta--cell"
                    rows="3"
                    :disabled="!canEdit || isReadonly(it)"
                    :placeholder="it.desc ? it.desc.slice(0, 60) : ''"
                    :value="valOf(it)"
                    @input="(e) => setVal(it.key, (e.target as HTMLTextAreaElement).value)"
                  />

                  <!-- ⑤ 普通文本 / 密文 -->
                  <template v-else>
                    <AppInput
                      :model-value="valOf(it)"
                      :disabled="!canEdit || isReadonly(it)"
                      :placeholder="it.type === 'secret' ? '（敏感值不可见）' : ''"
                      @update:model-value="(v: string) => setVal(it.key, v)"
                    />
                  </template>
                  <div v-if="it.default !== undefined && it.default !== null"
                       class="dim def">
                    默认：{{ String(it.default).slice(0, 60) || '（空）' }}
                  </div>
                </td>
                <td>
                  <AppTag size="sm" :kind="it.source === 'db' ? 'brand' : 'default'">
                    {{ it.source === 'db' ? '界面' : it.source === 'env' ? '环境变量' : '默认' }}
                  </AppTag>
                </td>
                <td class="note">
                  {{ it.desc || '—' }}
                  <!-- ★ 数值项：范围直接写进说明列（双保险，方便对照） -->
                  <div v-if="isNumber(it) && (it.min !== null || it.max !== null)" class="note__rng">
                    {{ numHint(it) }}
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </AppCard>
    </template>

    <!-- ══════════ 变更历史 ══════════ -->
    <template v-if="!loading && tab === 'history'">
      <AppCard title="变更历史" subtitle="★ 改错能退回 —— 这是「改得可控」的关键">
        <AppEmpty v-if="!history.length" icon="≡" title="暂无变更记录" />
        <div v-else class="tblwrap">
          <table class="tbl">
            <thead>
              <tr>
                <th style="width:150px">时间</th>
                <th style="width:120px">操作人</th>
                <th style="width:220px">配置项</th>
                <th>变更</th>
                <th style="width:110px" class="ops">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="h in history" :key="h.id || h.key + String(h.created_at)">
                <td class="mono dim">{{ h.created_at || '—' }}</td>
                <td>{{ h.operator_name || '—' }}</td>
                <td><code class="code">{{ h.key }}</code></td>
                <td class="diff">
                  <span class="diff__old">{{ (h.old_value || '（空）').slice(0, 40) }}</span>
                  <span class="diff__arr">→</span>
                  <span class="diff__new">{{ (h.new_value || '（空）').slice(0, 40) }}</span>
                  <div v-if="h.reason" class="dim">原因：{{ h.reason }}</div>
                </td>
                <td class="ops">
                  <button v-if="canEdit" class="op" @click="pendingRollback = h">回滚</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </AppCard>
    </template>

    <template #hint>
      ★ <b>保存流程</b>：改动 → 点「查看变更并保存」→ 看 diff → 填原因 → 确认。
      这样设计是为了让「会改什么」在提交前可见，避免误改生产配置。<br>
      ★ <b>优先级</b>：DB（界面）> 环境变量 > 注册表默认值。界面值会覆盖 .env。
    </template>
  </AppPage>

  <!-- ═══════════ diff 确认 ═══════════ -->
  <AppModal v-model="diffOpen" title="确认配置变更" width="720px">
    <div class="diffbox">
      <p class="diffbox__d">
        共 <b>{{ diffRows.length }}</b> 项变更
        <span v-if="riskyCount" class="warn">（其中 {{ riskyCount }} 项为高/中风险）</span>
      </p>

      <div class="dtable">
        <table class="tbl">
          <thead>
            <tr>
              <th style="width:210px">配置项</th>
              <th style="width:64px">风险</th>
              <th>原值</th>
              <th>新值</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in diffRows" :key="r.key">
              <td><code class="code">{{ r.key }}</code></td>
              <td>
                <AppTag v-if="r.risk" size="sm" :kind="RISK_TAG[r.risk] || 'default'">
                  {{ meta?.risk_label?.[r.risk] || r.risk }}
                </AppTag>
              </td>
              <td class="diff__old">{{ String(r.old ?? '（空）').slice(0, 60) }}</td>
              <td class="diff__new">{{ String(r.new ?? '（空）').slice(0, 60) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <label class="fld">
        <span class="fld__l">变更原因 <i class="req">必填</i></span>
        <textarea
          v-model="reason"
          class="ta"
          rows="2"
          placeholder="请说明为什么改（会写入审计与变更历史）"
        />
      </label>
    </div>

    <template #footer>
      <AppButton variant="ghost" :disabled="saving" @click="diffOpen = false">取消</AppButton>
      <AppButton
        variant="primary"
        :disabled="saving || reason.trim().length < 2"
        :loading="saving"
        @click="applyDiff"
      >确认应用</AppButton>
    </template>
  </AppModal>

  <!-- ═══════════ 回滚确认 ═══════════ -->
  <AppModal v-model="rollbackOpen" title="回滚配置" width="480px">
    <p class="rb">
      将配置项 <code class="code">{{ pendingRollback?.key }}</code> 回滚到
      <b>{{ (pendingRollback?.old_value || '（空）').slice(0, 40) }}</b>。
      <br><span class="dim">回滚本身也会记入变更历史。</span>
    </p>
    <label class="fld">
      <span class="fld__l">回滚原因 <i class="req">必填</i></span>
      <textarea
        v-model="rbReason"
        class="ta"
        rows="2"
        placeholder="请填写回滚原因（至少 2 个字符）"
      />
    </label>
    <template #footer>
      <AppButton variant="ghost" :disabled="saving" @click="rollbackOpen = false">取消</AppButton>
      <AppButton
        variant="primary"
        :disabled="saving || rbReason.trim().length < 2"
        :loading="saving"
        @click="doRollback(rbReason)"
      >确认回滚</AppButton>
    </template>
  </AppModal>
</template>

<style scoped>
.sel { padding: 6px 10px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); color: var(--text); font-size: 12px; font-family: inherit; }
.sel--wide { width: 100%; max-width: 260px; }

/* ════════ ★ v1.0-E4.15：类型化渲染样式 ════════ */
/* 布尔开关行 */
.boolrow { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; }
.boolrow.is-off { cursor: not-allowed; opacity: .6; }
.boolrow__sw { width: 16px; height: 16px; cursor: inherit; accent-color: var(--brand, #2b6cb0); }
.boolrow__state { font-size: 12px; font-weight: 500; }
.boolrow__state.on { color: var(--success, #1a7f4b); }
.boolrow__state.off { color: var(--text-faint); }

/* 数值范围提示（说明列） */
.note__rng {
  margin-top: 3px; font-size: 10.5px;
  color: var(--brand-600, #2b6cb0);
  font-variant-numeric: tabular-nums;
}

/* 单元格内多行文本 */
.ta--cell {
  width: 100%; max-width: 280px; min-height: 60px;
  padding: 6px 10px;
  border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); color: var(--text);
  font-size: 12px; font-family: inherit; line-height: 1.6;
  resize: vertical;
}
.ta--cell:focus { outline: none; border-color: var(--brand); }
.ta--cell:disabled { background: var(--bg-sunken, var(--surface)); color: var(--text-faint); }
.sp { flex: 1; }
.mt { margin-top: 12px; }
.dim { color: var(--text-faint); font-size: 11px; }
.warn { color: var(--warn, #b8860b); }
.code { font-size: 11px; padding: 1px 5px; border-radius: 4px;
  border: 1px solid var(--border); word-break: break-all; }
.hint-inline { font-size: 11px; color: var(--text-faint); }
.hint-inline code { font-size: 10px; padding: 1px 4px; border-radius: 3px; border: 1px solid var(--border); }

.tblwrap { overflow-x: auto; }
.tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
.tbl th { text-align: left; font-weight: 500; font-size: 11px; color: var(--text-faint);
  padding: 7px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }
.tbl td { padding: 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
.tbl tr:last-child td { border-bottom: none; }
.tbl .ops { white-space: nowrap; vertical-align: middle; }
.note { font-size: 11px; color: var(--text-muted); line-height: 1.7; }
.def { margin-top: 4px; }
.op { font-size: 11px; padding: 3px 8px; border: 1px solid var(--border);
  border-radius: 5px; background: var(--surface); color: var(--text-muted);
  cursor: pointer; font-family: inherit; }
.op:hover { color: var(--text); border-color: var(--brand); }

.diff { font-size: 11px; line-height: 1.7; }
.diff__old { color: var(--danger, #d33); text-decoration: line-through;
  word-break: break-all; }
.diff__arr { margin: 0 7px; color: var(--text-faint); }
.diff__new { color: var(--success, #1a7f4b); word-break: break-all; }

.diffbox { display: flex; flex-direction: column; gap: 14px; }
.diffbox__d { margin: 0; font-size: 12px; color: var(--text-muted); }
.diffbox__d b { color: var(--text); }
.dtable { max-height: 320px; overflow: auto; border: 1px solid var(--border); border-radius: 8px; }
.fld { display: flex; flex-direction: column; gap: 5px; }
.fld__l { font-size: 12px; color: var(--text-muted); }
.fld__l i { color: var(--danger, #d33); font-size: 11px; font-style: normal; }
.ta { padding: 9px 11px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); color: var(--text); font-size: 13px;
  font-family: inherit; line-height: 1.7; width: 100%; resize: vertical; }
.ta:focus { outline: none; border-color: var(--brand); }
.rb { margin: 0; font-size: 13px; line-height: 1.85; color: var(--text-muted); }
.rb b { color: var(--text); }

/* ────────── ★ 邮件与站点（v1.0-E4.2）────────── */
.hint {
  margin: 0 0 var(--sp-3); font-size: var(--fs-xs);
  color: var(--text-muted); line-height: 1.8;
}
.hint strong { color: var(--text); }
.hint--warn {
  padding: var(--sp-2) var(--sp-3);
  border-left: 3px solid var(--warning, #b8860b);
  background: var(--bg-sunken); border-radius: var(--r-sm);
}
.hint .ok { color: var(--success, #1a7f4b); }
.hint .warn { color: var(--warning, #b8860b); }
.dim { color: var(--text-faint); font-size: var(--fs-xs); }

/* 邮件表单：两列自适应，窄屏单列 */
.mgrid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: var(--sp-3);
}
.mfield { display: flex; flex-direction: column; gap: 5px; }
.mfield__label { font-size: var(--fs-xs); color: var(--text-muted); }
.sel--wide { width: 100%; height: 32px; }

.mactions {
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-3); flex-wrap: wrap; margin-top: var(--sp-4);
}
.mtest { display: flex; align-items: center; gap: var(--sp-2); }

/* ══════════ ★★ v1.0-E4.4：能力总览卡片 ══════════
   ★ 设计意图：让运营**一屏看到所有可配的东西**，
     而不是面对 56 行扁平表格猜"模型配置在哪儿"。 */
.caps {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: var(--sp-3);
}
.cap {
  display: flex; align-items: flex-start; gap: var(--sp-3);
  padding: var(--sp-4);
  border: 1px solid var(--border); border-radius: 14px;
  background: var(--bg-elevated);
  text-align: left; cursor: pointer;
  transition: transform var(--dur-base) var(--ease),
              box-shadow var(--dur-base) var(--ease),
              border-color var(--dur-base) var(--ease);
}
.cap:hover {
  transform: translateY(-2px);
  border-color: var(--brand-200);
  box-shadow: 0 12px 28px -18px rgba(43, 108, 176, .45);
}
/* ★ 未配置的渠道用暖色描边提示（不是错误，是"可以去做"） */
.cap--warn { border-color: var(--warning, #d69e2e); }
.cap__ico {
  display: grid; place-items: center; flex: none;
  width: 38px; height: 38px; border-radius: 10px;
  background: var(--brand-50); font-size: 18px;
}
.cap__body { display: flex; flex-direction: column; gap: 3px; min-width: 0; flex: 1; }
.cap__title { font-size: var(--fs-sm); color: var(--text); }
.cap__desc {
  font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.7;
}
.cap__status {
  margin-top: 3px; font-size: 11px; color: var(--brand-600);
}
.cap__status--warn { color: var(--warning, #b8860b); }
.cap__go {
  flex: none; align-self: center;
  color: var(--text-subtle); font-size: 20px; line-height: 1;
}
.cap:hover .cap__go { color: var(--brand); }

.prio { margin: 0 0 var(--sp-3); font-size: var(--fs-sm); color: var(--text); }
.prio b { color: var(--brand-600); }
.prio-desc {
  margin: 0 0 var(--sp-2); font-size: var(--fs-xs);
  color: var(--text-muted); line-height: 1.9;
}
.prio-desc code {
  padding: 1px 5px; border-radius: 4px;
  background: var(--bg-sunken); font-family: var(--font-mono); font-size: 11px;
}

/* ══════════ 高级项折叠 ══════════ */
.adv-toggle {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: var(--fs-xs); color: var(--text-muted); cursor: pointer;
  padding: 4px 10px; border-radius: var(--r-full);
  border: 1px solid var(--border); background: var(--bg-elevated);
}
.adv-toggle:hover { border-color: var(--border-strong); }
.adv-toggle__n {
  padding: 0 5px; border-radius: var(--r-full);
  background: var(--bg-sunken); color: var(--text-subtle);
  font-size: 10px; font-variant-numeric: tabular-nums;
}
/* ★ 高级行：整体降饱和，视觉上"退到后面" */
tr.is-advanced { background: var(--bg-sunken); }
tr.is-advanced .code { color: var(--text-muted); }
.adv-flag {
  display: inline-block; margin-top: 3px;
  padding: 0 5px; border-radius: 4px;
  background: var(--bg-sunken); border: 1px solid var(--border);
  color: var(--text-subtle); font-size: 10px;
}

/* ══════════ 支付渠道 ══════════ */
.chans { display: flex; flex-direction: column; gap: var(--sp-4); }
.chan {
  padding: var(--sp-4);
  border: 1px solid var(--border); border-radius: 12px;
  background: var(--bg-sunken);
}
.chan__hd {
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-3); margin-bottom: var(--sp-3);
}
.chan__hd strong { font-size: var(--fs-sm); color: var(--text); }
.chan__tags { display: flex; gap: var(--sp-2); }
.chan__note {
  margin: 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.8;
}
.chan__fields {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: var(--sp-3); margin-bottom: var(--sp-3);
}
.cfield { display: flex; flex-direction: column; gap: 4px; }
.cfield__label {
  display: flex; align-items: center; gap: 6px;
  font-size: var(--fs-xs); color: var(--text-muted);
}
.cfield__set { color: var(--success, #38a169); font-size: 10px; }
.cfield__unset { color: var(--text-subtle); font-size: 10px; }
.cfield__in {
  height: 32px; padding: 0 var(--sp-3); width: 100%;
  border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--bg-elevated); color: var(--text);
  font-size: var(--fs-sm); font-family: inherit;
}
.cfield__in:focus { outline: none; border-color: var(--brand); }
.cfield__in:disabled { background: var(--bg-hover); color: var(--text-subtle); }
.cfield__desc { font-size: 10.5px; color: var(--text-subtle); line-height: 1.5; }
.chan__ops { display: flex; gap: var(--sp-2); }

/* 自检结果 */
.checks { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.checks li {
  display: flex; align-items: baseline; gap: var(--sp-2);
  font-size: var(--fs-sm); color: var(--text);
}
.checks li.is-bad { color: var(--danger, #d33); }
.checks__mark { flex: none; font-weight: 700; }
.checks li:not(.is-bad) .checks__mark { color: var(--success, #38a169); }
.checks__hint { font-size: var(--fs-xs); color: var(--text-muted); }
.checks__note {
  margin: var(--sp-3) 0 0; padding-top: var(--sp-3);
  border-top: 1px solid var(--border);
  font-size: var(--fs-xs); color: var(--text-subtle); line-height: 1.8;
}
.switch { display: flex; align-items: center; gap: var(--sp-2); cursor: pointer; }
.switch input { cursor: pointer; }
.state { padding: var(--sp-5); text-align: center; color: var(--text-muted); font-size: var(--fs-sm); }
</style>
