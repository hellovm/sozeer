<script setup lang="ts">
/**
 * AdminGateView.vue · 后台登录门（v0.26.2 G0-6）
 *
 * ★★ 与前台登录的关键差异：
 *   ① **行内报错**，不做全局 401 跳转 —— 密码错误不能被当成"会话失效"
 *      （否则会清掉会话、刷新页面，用户看到"点了登录没反应"，§34/§77）
 *   ② 非管理员登录成功也要**明确拒绝**（而不是进去后处处 403）
 *      拒绝时要说明原因和下一步（找谁开权限），不能只说"无权限"
 *   ③ 不提供"记住我"—— 后台会话应当短
 */
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAdminStore } from '@/stores/admin'

const router = useRouter()
const route = useRoute()
const adm = useAdminStore()

const username = ref('')
const password = ref('')
const errMsg = ref('')
const showPwd = ref(false)

onMounted(() => {
  // ★ 带过来的提示（如"登录已过期"）只显示一次
  if (adm.notice) {
    errMsg.value = adm.notice
    adm.logout('')
  }
})

async function submit() {
  errMsg.value = ''
  const u = username.value.trim()
  const p = password.value
  if (!u || !p) {
    errMsg.value = '请输入账号与密码'
    return
  }
  try {
    await adm.login(u, p)
    // ★ 回跳到登录前想去的页面（深链友好）
    const back = adm.lastPage()
    const redirect = (route.query.redirect as string) || back || ''
    if (redirect && redirect.startsWith('/admin/')) {
      await router.replace(redirect)
    } else {
      await router.replace({ name: 'admin-dashboard' })
    }
  } catch (e) {
    const msg = (e as Error).message || '登录失败'
    errMsg.value = msg
    password.value = ''
    // ★ 明确区分"凭据错误"与"无后台权限"——给的下一步动作完全不同
    if (msg.includes('没有后台访问权限')) {
      errMsg.value = msg
    }
  }
}
</script>

<template>
  <div class="gate">
    <div class="gate__card">
      <div class="gate__brand">
        <span class="gate__cn">溯字</span>
        <span class="gate__en">SOZEER</span>
      </div>
      <h1 class="gate__title">管理后台</h1>
      <p class="gate__sub">仅限管理员访问。所有操作（含失败）都会被审计记录。</p>

      <form class="gate__form" @submit.prevent="submit">
        <label class="gate__field">
          <span class="gate__label">管理员账号</span>
          <input
            v-model="username"
            class="gate__input"
            autocomplete="username"
            placeholder="用户名"
            :disabled="adm.busy"
          >
        </label>

        <label class="gate__field">
          <span class="gate__label">密码</span>
          <div class="gate__pwd">
            <input
              v-model="password"
              class="gate__input"
              :type="showPwd ? 'text' : 'password'"
              autocomplete="current-password"
              placeholder="密码"
              :disabled="adm.busy"
            >
            <button
              type="button"
              class="gate__eye"
              :aria-label="showPwd ? '隐藏密码' : '显示密码'"
              @click="showPwd = !showPwd"
            >
              {{ showPwd ? '隐藏' : '显示' }}
            </button>
          </div>
        </label>

        <button class="gate__btn" type="submit" :disabled="adm.busy">
          {{ adm.busy ? '登录中…' : '登录' }}
        </button>

        <!-- ★ 行内常驻错误（不清空、不跳转） -->
        <p v-if="errMsg" class="gate__msg" role="alert">{{ errMsg }}</p>
      </form>

      <p class="gate__foot">
        本页不索引、不缓存。建议在 Cloudflare 侧为本路径配置 Access 策略。
      </p>
      <RouterLink class="gate__back" :to="{ name: 'intro' }">← 返回站点首页</RouterLink>
    </div>
  </div>
</template>

<style scoped>
.gate {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background: var(--bg);
}
.gate__card {
  width: 100%;
  max-width: 380px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 30px 28px 24px;
  box-shadow: 0 10px 40px rgba(0, 0, 0, .06);
}
.gate__brand { display: flex; align-items: baseline; gap: 6px; margin-bottom: 18px; }
.gate__cn { font-size: 16px; font-weight: 700; letter-spacing: .06em; }
.gate__en { font-size: 10px; color: var(--brand); letter-spacing: .18em; }
.gate__title { font-size: 19px; margin: 0 0 6px; }
.gate__sub {
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.75;
  margin: 0 0 22px;
}
.gate__form { display: flex; flex-direction: column; gap: 14px; }
.gate__field { display: flex; flex-direction: column; gap: 6px; }
.gate__label { font-size: 12px; color: var(--text-muted); }
.gate__input {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
  font-family: inherit;
  font-size: 14px;
}
.gate__input:focus { outline: none; border-color: var(--brand); }
.gate__pwd { position: relative; }
.gate__pwd .gate__input { padding-right: 58px; }
.gate__eye {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  border: none;
  background: none;
  color: var(--text-faint);
  font-size: 11px;
  cursor: pointer;
  padding: 4px 6px;
  font-family: inherit;
}
.gate__btn {
  margin-top: 4px;
  padding: 11px;
  border: none;
  border-radius: 8px;
  background: var(--brand);
  color: #fff;
  font-size: 14px;
  font-family: inherit;
  cursor: pointer;
}
.gate__btn:hover:not(:disabled) { filter: brightness(1.06); }
.gate__btn:disabled { opacity: .62; cursor: default; }
.gate__msg {
  margin: 0;
  padding: 9px 11px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--danger, #d33) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--danger, #d33) 30%, transparent);
  color: var(--danger, #d33);
  font-size: 12px;
  line-height: 1.7;
}
.gate__foot {
  margin: 20px 0 0;
  font-size: 11px;
  color: var(--text-faint);
  line-height: 1.7;
}
.gate__back {
  display: inline-block;
  margin-top: 12px;
  font-size: 12px;
  color: var(--text-muted);
  text-decoration: none;
}
.gate__back:hover { color: var(--brand); }
</style>
