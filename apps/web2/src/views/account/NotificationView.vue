<script setup lang="ts">
/**
 * NotificationView · 通知中心（v0.26.1）
 *
 * ★ 契约：
 *   GET  /api/me/notifications           → { total, items: [{id, kind, title, body, read, link, created_at}] }
 *   GET  /api/me/notifications/count     → { unread }
 *   POST /api/me/notifications/read      { ids }
 *   GET  /api/me/notification-prefs      → { prefs: {}, forced: [] }
 *   PUT  /api/me/notification-prefs      { prefs }
 *
 * ★★ 关键设计（不要"优化"掉）：
 *   ① **安全类通知不可关闭**（forced 列表）→ UI 必须显示"不可关闭"并禁用开关。
 *      账号被盗时这是唯一自救线索（见 MEMORY §23）。
 *   ② 偏好按**类型细分**（不是一个大开关）—— 一个总开关的结果通常是"全关"，
 *      连安全告警一起关掉。
 *   ③ 已读用批量接口（避免 N 次请求）。
 */
import { computed, onMounted, ref } from 'vue'
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'
import AppTag from '@/components/AppTag.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import { useUiStore } from '@/stores/ui'
import { meApi, type Notification } from '@/api/modules/me'

const ui = useUiStore()
const loading = ref(true)
const items = ref<Notification[]>([])
const total = ref(0)
const unread = ref(0)
const tab = ref<'list' | 'prefs'>('list')

/**
 * ★★ 真实契约：items = {kind: {inapp, email, label, forced}}
 *   —— 不是之前以为的 {prefs: {kind: boolean}}。
 *   每个类型有**两个通道**（站内信 + 邮件），要分别开关。
 */
const prefs = ref<Record<string, { inapp?: boolean; email?: boolean; label?: string; forced?: boolean }>>({})
const forcedKinds = ref<string[]>([])
/** 本地草稿（保存前不提交），避免每次点开关都发请求 */
const draft = ref<Record<string, { inapp: boolean; email: boolean }>>({})
const prefsBusy = ref(false)

/** ★ 通知类型的中文名（后端 kind 是自由字符串，必须兜底） */
const KIND_LABEL: Record<string, string> = {
  security: '安全提醒', account: '账号动态', billing: '账单与支付',
  task: '任务完成', system: '系统公告', product: '产品更新',
  marketing: '推广消息', quota: '额度预警',
}
function kindLabel(k: string) {
  return KIND_LABEL[k] || k || '通知'
}
/** ★ 安全类是强制的（不可关闭） */
function isForced(k: string) {
  return forcedKinds.value.includes(k) || prefs.value[k]?.forced === true || k === 'security'
}

const unreadItems = computed(() => items.value.filter(n => !n.read))

async function load() {
  loading.value = true
  try {
    const [n, c, p] = await Promise.all([
      meApi.notifications(50, 0).catch(() => ({ total: 0, items: [] })),
      meApi.notificationCount().catch(() => ({ unread: 0 })),
      meApi.notificationPrefs().catch(() => ({
        items: {} as Record<string, never>, forced_kinds: [], kinds: [],
      })),
    ])
    items.value = n.items || []
    total.value = n.total || items.value.length
    unread.value = c.unread || 0
    prefs.value = p.items || {}
    forcedKinds.value = p.forced_kinds || []
    // 初始化本地草稿
    const d: Record<string, { inapp: boolean; email: boolean }> = {}
    for (const [k, v] of Object.entries(prefs.value)) {
      d[k] = { inapp: v.inapp !== false, email: v.email !== false }
    }
    draft.value = d
  } finally {
    loading.value = false
  }
}

async function markRead(list: Notification[]) {
  const ids = list.filter(n => !n.read).map(n => n.id)
  if (!ids.length) return
  try {
    await meApi.readNotifications(ids)
    // 乐观更新（列表即时反馈），失败时下次 load 会纠正
    items.value.forEach(n => { if (ids.includes(n.id)) n.read = 1 })
    unread.value = Math.max(0, unread.value - ids.length)
  } catch (e) {
    ui.err((e as Error).message || '操作失败')
  }
}

async function markAll() {
  await markRead(items.value)
  ui.ok('已全部标记为已读')
}

async function savePrefs() {
  prefsBusy.value = true
  try {
    // ★ 后端是**按类型**的接口（PerKind）→ 逐个提交，而不是整表 PUT。
    //   为什么：避免并发覆盖（用户在多端改不同项时，整表提交会互相冲掉）。
    const jobs: Promise<unknown>[] = []
    for (const [k, v] of Object.entries(draft.value)) {
      if (isForced(k)) continue        // 强制项不提交（后端也会拒）
      jobs.push(meApi.saveNotificationPref(k, v.inapp, v.email))
    }
    await Promise.all(jobs)
    ui.ok('通知偏好已保存')
    await load()
  } catch (e) {
    ui.err((e as Error).message || '保存失败')
  } finally {
    prefsBusy.value = false
  }
}

function fmt(s?: string) { return s ? s.replace('T', ' ').slice(0, 16) : '—' }

onMounted(load)
</script>

<template>
  <div class="page">
    <header class="page__head">
      <div>
        <h1 class="page__title">通知中心</h1>
        <p class="page__sub">
          共 {{ total }} 条<span v-if="unread"> · <b>{{ unread }} 条未读</b></span>
        </p>
      </div>
      <AppButton v-if="unread" size="sm" @click="markAll">全部已读</AppButton>
    </header>

    <div class="tabs">
      <button class="tab" :class="{ on: tab === 'list' }" @click="tab = 'list'">
        消息列表</button>
      <button class="tab" :class="{ on: tab === 'prefs' }" @click="tab = 'prefs'">
        通知偏好</button>
    </div>

    <AppSkeleton v-if="loading" variant="card" :count="2" />

    <!-- 消息列表 -->
    <AppCard v-else-if="tab === 'list'">
      <AppEmpty v-if="!items.length" icon="◔" title="暂无通知"
                description="任务完成、账单变动、安全提醒都会出现在这里。" />

      <ul v-else class="list">
        <li v-for="n in items" :key="n.id"
            class="item" :class="{ unread: !n.read }"
            @click="markRead([n])">
          <div class="item__dot" :class="{ on: !n.read }" />
          <div class="item__main">
            <div class="item__head">
              <AppTag size="sm" :kind="n.kind === 'security' ? 'danger' : 'default'">
                {{ kindLabel(n.kind) }}
              </AppTag>
              <b class="item__title">{{ n.title }}</b>
              <span class="muted tiny">{{ fmt(n.created_at) }}</span>
            </div>
            <p class="item__body">{{ n.body }}</p>
            <RouterLink v-if="n.link" class="item__link" :to="n.link">查看详情 →</RouterLink>
          </div>
        </li>
      </ul>
    </AppCard>

    <!-- 偏好设置 -->
    <AppCard v-else title="通知偏好"
             subtitle="按类型分别设置 —— 只关掉你不想收的">
      <AppEmpty v-if="!Object.keys(prefs).length" icon="—" title="暂无可配置项" />
      <ul v-else class="prefs">
        <li v-for="(v, k) in prefs" :key="k" class="pref">
          <div class="pref__main">
            <b>{{ v.label || kindLabel(k as string) }}</b>
            <span v-if="isForced(k as string)" class="muted tiny">
              安全类通知不可关闭 —— 账号异常时这是你唯一的自救线索
            </span>
          </div>
          <div class="pref__chans">
            <label class="chan" :class="{ disabled: isForced(k as string) }">
              <input type="checkbox" :checked="draft[k]?.inapp"
                     :disabled="isForced(k as string)"
                     @change="draft[k].inapp = ($event.target as HTMLInputElement).checked">
              <span>站内信</span>
            </label>
            <label class="chan" :class="{ disabled: isForced(k as string) }">
              <input type="checkbox" :checked="draft[k]?.email"
                     :disabled="isForced(k as string)"
                     @change="draft[k].email = ($event.target as HTMLInputElement).checked">
              <span>邮件</span>
            </label>
          </div>
        </li>
      </ul>
      <template #footer>
        <AppButton variant="primary" :loading="prefsBusy" @click="savePrefs">
          保存偏好
        </AppButton>
        <span class="muted tiny">修改后立即生效 · 安全类通知始终开启</span>
      </template>
    </AppCard>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 860px; margin: 0 auto; }
.page__head { display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--sp-4); margin-bottom: var(--sp-4); }
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); }

.tabs { display: flex; gap: 4px; margin-bottom: var(--sp-3); border-bottom: 1px solid var(--border); }
.tab { padding: 8px 14px; border: none; background: none; cursor: pointer;
  color: var(--text-muted); font-size: var(--fs-sm); font-family: inherit;
  border-bottom: 2px solid transparent; }
.tab.on { color: var(--brand); border-bottom-color: var(--brand); }

.list { list-style: none; margin: 0; padding: 0; }
.item { display: flex; gap: var(--sp-3); padding: var(--sp-3) 0;
  border-bottom: 1px solid var(--border); cursor: pointer; }
.item:last-child { border-bottom: none; }
.item:hover { background: var(--surface-2); }
.item__dot { width: 7px; height: 7px; border-radius: 50%; margin-top: 8px; flex: none;
  background: transparent; }
.item__dot.on { background: var(--brand); }
.item__main { flex: 1; min-width: 0; }
.item__head { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.item__title { font-size: var(--fs-sm); }
.item__body { margin: 6px 0 0; font-size: var(--fs-sm); color: var(--text-muted);
  line-height: 1.7; }
.item__link { display: inline-block; margin-top: 6px; font-size: var(--fs-xs);
  color: var(--brand); text-decoration: none; }
.item__link:hover { text-decoration: underline; }
.unread .item__title { color: var(--text); }

.prefs { list-style: none; margin: 0; padding: 0; }
.pref { display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-4); padding: var(--sp-3) 0; border-bottom: 1px solid var(--border); }
.pref:last-child { border-bottom: none; }
.pref__main { display: flex; flex-direction: column; gap: 3px; }
.pref__main b { font-size: var(--fs-sm); }

.pref__chans { display: flex; gap: var(--sp-3); flex: none; }
.chan { display: flex; align-items: center; gap: 5px; font-size: var(--fs-xs);
  color: var(--text-muted); cursor: pointer; }
.chan.disabled { opacity: .5; cursor: not-allowed; }
.chan input { accent-color: var(--brand); }

.switch { position: relative; display: inline-block; width: 40px; height: 22px; flex: none; }
.switch input { opacity: 0; width: 0; height: 0; }
.switch__slider { position: absolute; inset: 0; background: var(--border);
  border-radius: 999px; transition: .2s; cursor: pointer; }
.switch__slider::before { content: ''; position: absolute; width: 16px; height: 16px;
  left: 3px; top: 3px; background: #fff; border-radius: 50%; transition: .2s; }
.switch input:checked + .switch__slider { background: var(--brand); }
.switch input:checked + .switch__slider::before { transform: translateX(18px); }
.switch.disabled .switch__slider { opacity: .5; cursor: not-allowed; }
</style>
