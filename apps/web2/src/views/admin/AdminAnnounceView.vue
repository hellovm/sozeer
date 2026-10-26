<script setup lang="ts">
/**
 * AdminAnnounceView.vue · 公告广播（v1.0-E4.13 WS1）
 *
 * ★ 能力边界（与配置中心分工）：
 *   · 本页 = **一次性群发站内信**（维护通知、活动公告等"事件型"消息）
 *   · 常驻顶部公告条 = 配置中心的 `system.announcement` 配置项（持久生效）
 *
 * ★ 安全约束（对照 notify_routes.admin_broadcast）：
 *   · 只发站内信 —— 邮件通道服务端强制关闭（防垃圾邮件 / SMTP 配额）
 *   · 服务端上限 20000 人；标题必填
 */
import { computed, ref } from 'vue'
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'
import AppInput from '@/components/AppInput.vue'
import AdminConfirm from '@/components/AdminConfirm.vue'
import { useUiStore } from '@/stores/ui'
import { notifyApi } from '@/api/admin_modules_extra'
import { useRouter } from 'vue-router'

const ui = useUiStore()
const router = useRouter()

const title = ref('')
const body = ref('')
const link = ref('')
const confirmOpen = ref(false)
const submitting = ref(false)
const lastResult = ref<{ sent?: number; skipped?: number; total?: number; note?: string } | null>(null)

const canSend = computed(() => title.value.trim().length > 0)

function askSend() {
  if (!canSend.value) { ui.warn('请填写公告标题'); return }
  confirmOpen.value = true
}

async function doSend() {
  submitting.value = true
  try {
    const r = await notifyApi.broadcast(title.value.trim(), body.value.trim(),
      link.value.trim())
    lastResult.value = r
    ui.ok(`公告已发送（${r.sent ?? r.total ?? '?'} 人）`)
    title.value = ''
    body.value = ''
    link.value = ''
    confirmOpen.value = false
  } catch (e) {
    ui.err((e as Error).message || '发送失败')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="page">
    <AppCard title="发系统公告" subtitle="群发站内信给全部活跃用户（邮件通道默认关闭）">
      <div class="form">
        <AppInput v-model="title" label="标题（必填）" :maxlength="60"
                  placeholder="如：今晚 23:00-24:00 系统维护" />
        <label class="fld">
          <span class="fld__k">正文（选填）</span>
          <textarea v-model="body" rows="5" :maxlength="500"
                    placeholder="补充说明（维护影响、活动规则等）" />
        </label>
        <AppInput v-model="link" label="跳转链接（选填）" placeholder="/pricing 或 /help/maintenance" />
        <p class="dim tiny">
          提示：站内信在右上角铃铛查看；链接建议用站内路径（/ 开头）。
          需要常驻的顶部公告条请到「配置中心 → system.announcement」设置。
        </p>
      </div>
      <template #footer>
        <AppButton variant="primary" :disabled="!canSend" :loading="submitting" @click="askSend">
          发送公告
        </AppButton>
        <AppButton variant="ghost" @click="router.push({ name: 'admin-config' })">
          配置常驻公告条 ›
        </AppButton>
      </template>
    </AppCard>

    <p v-if="lastResult" class="result dim">
      上次发送：{{ lastResult.sent ?? lastResult.total ?? '?' }} 人成功<template
        v-if="lastResult.skipped">，跳过 {{ lastResult.skipped }}</template>。{{ lastResult.note || '' }}
    </p>

    <AdminConfirm
      v-model="confirmOpen"
      title="确认群发公告"
      :body="`将向<b>全部活跃用户</b>发送站内信「${title}」，发送后不可撤回。`"
      ok-text="确认发送"
      :loading="submitting"
      @confirm="doSend"
    />
  </div>
</template>

<style scoped>
.page { max-width: 720px; }
.form { display: flex; flex-direction: column; gap: 14px; }
.fld { display: flex; flex-direction: column; gap: 6px; }
.fld__k { font-size: 12px; color: var(--text-muted); }
textarea {
  padding: 9px 11px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); color: var(--text); font-size: 13px;
  font-family: inherit; resize: vertical;
}
textarea:focus { outline: 2px solid var(--brand-500, #4a7dff); outline-offset: -1px; }
.dim { color: var(--text-faint); }
.tiny { font-size: 11px; }
.result { margin-top: 12px; font-size: 12px; }
</style>
