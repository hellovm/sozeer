<script setup lang="ts">
/**
 * LoginView · 登录
 *
 * ★★ 血泪教训（MEMORY.md §34）：
 *   登录接口的 401 = 密码错误 → 必须**行内常驻显示**，且**不能**触发"会话失效"跳转。
 *   toast 会消失，登录失败需要常驻可读的提示（旧版 v0.21.1 的修复）。
 */
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { ApiError } from '@/types/api'
import AppButton from '@/components/AppButton.vue'
import AppInput from '@/components/AppInput.vue'
import authApi from '@/api/modules/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const ui = useUiStore()

const username = ref('')
const password = ref('')
const error = ref('')
const submitting = ref(false)
const siteName = ref('溯字 SOZEER')
const siteTagline = ref('与创作主编一起，把一部小说从立项聊到成稿')
const allowRegister = ref(true)

onMounted(async () => {
  try {
    const r = await authApi.allowRegister()
    siteName.value = r.site_name || siteName.value
    siteTagline.value = r.site_tagline || siteTagline.value
    allowRegister.value = r.allow !== false
  } catch { /* 拿不到就用默认 */ }
})

async function onSubmit() {
  error.value = ''
  if (!username.value.trim() || !password.value) {
    error.value = '请输入用户名与密码'
    return
  }
  submitting.value = true
  try {
    await auth.login(username.value.trim(), password.value)
    ui.ok('登录成功')
    const redirect = route.query.redirect as string | undefined
    router.push(redirect || { name: 'home' })
  } catch (e) {
    // ★ 401 = 凭据错误，显示原因（不清 token、不跳转）
    if (e instanceof ApiError) {
      error.value = e.status === 429
        ? '操作过于频繁，请稍等几分钟后重试'
        : (e.message || '登录失败，请检查用户名与密码')
    } else {
      error.value = '网络异常，请稍后重试'
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="auth">
    <div class="auth__card">
      <div class="auth__brand">
        <span class="auth__logo" aria-hidden="true">溯</span>
        <span class="auth__name">{{ siteName }}</span>
      </div>
      <p class="auth__tagline">{{ siteTagline }}</p>

      <h1 class="auth__title">欢迎回来</h1>
      <p class="auth__sub">登录以继续你的创作</p>

      <form class="auth__form" @submit.prevent="onSubmit">
        <AppInput
          v-model="username"
          label="用户名"
          placeholder="请输入用户名"
          autocomplete="username"
          clearable
        />
        <AppInput
          v-model="password"
          label="密码"
          type="password"
          placeholder="请输入密码"
          autocomplete="current-password"
          @enter="onSubmit"
        />

        <!-- ★ 行内常驻错误（不用 toast，避免一闪而过） -->
        <p v-if="error" class="auth__error" role="alert">{{ error }}</p>

        <AppButton
          variant="primary"
          size="lg"
          block
          type="submit"
          :loading="submitting"
        >登录</AppButton>
      </form>

      <p v-if="allowRegister" class="auth__switch">
        还没有账号？
        <RouterLink :to="{ name: 'register' }">立即注册</RouterLink>
      </p>
      <p v-else class="auth__switch muted">当前已关闭注册，请联系管理员开通</p>

      <p class="auth__tip">
        登不上？若提示「操作过于频繁」，等几分钟再试（登录限流：10 次 / 5 分钟）。
        <!-- ★ v1.0-E4.16：移除"管理员可访问管理后台"提示 ——
             登录页是公开页面，向所有访客暴露后台入口是没必要的攻击面侦察信息；
             管理员自己知道 /admin 地址（且后台有独立登录+2FA）。 -->
      </p>
    </div>
  </div>
</template>

<style scoped>
.auth {
  min-height: 100vh;
  display: grid; place-items: center;
  padding: var(--sp-5);
  background: var(--bg);
}
.auth__card {
  width: min(100%, 400px);
  padding: var(--sp-8) var(--sp-6);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  box-shadow: var(--sh-md);
}
.auth__brand { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: var(--sp-2); }
.auth__logo {
  width: 28px; height: 28px;
  display: grid; place-items: center;
  border-radius: 8px; background: var(--brand-500); color: #fff;
  font-size: 16px; font-family: serif;
}
.auth__name { font-size: var(--fs-sm); font-weight: var(--fw-bold); }
.auth__tagline {
  font-size: var(--fs-xs); color: var(--text-muted);
  margin-bottom: var(--sp-6); line-height: var(--lh-normal);
}
.auth__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.auth__sub { font-size: var(--fs-sm); color: var(--text-muted); margin-bottom: var(--sp-5); }

.auth__form { display: flex; flex-direction: column; gap: var(--sp-4); }
.auth__error {
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-md);
  background: var(--danger-bg);
  border: 1px solid var(--danger);
  color: var(--danger);
  font-size: var(--fs-xs);
  line-height: var(--lh-normal);
}
.auth__switch { margin-top: var(--sp-5); font-size: var(--fs-sm); text-align: center; }
.auth__tip {
  margin-top: var(--sp-5); padding-top: var(--sp-4);
  border-top: 1px solid var(--border);
  font-size: var(--fs-xs); color: var(--text-subtle);
  line-height: var(--lh-relaxed); text-align: center;
}
</style>
