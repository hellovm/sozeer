<script setup lang="ts">
/**
 * RegisterView · 注册
 * ★ 首个注册用户自动成为管理员（后端逻辑），这里给出提示
 */
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { ApiError } from '@/types/api'
import AppButton from '@/components/AppButton.vue'
import AppInput from '@/components/AppInput.vue'
import authApi from '@/api/modules/auth'

const router = useRouter()
// ★ v1.0-E4：读 ?redirect= 用于注册后回跳（转化优化）
const route = useRoute()
const auth = useAuthStore()
const ui = useUiStore()

const username = ref('')
const password = ref('')
const password2 = ref('')
const email = ref('')
const error = ref('')
const submitting = ref(false)
const isFirstUser = ref(false)
/* ★★ v1.0-E4.11（合规/E1 备案）：协议同意勾选。
 *   未勾选 → 提交按钮禁用（后端 agreed_terms=False 同样会拒，双保险）。 */
const agreed = ref(false)

/**
 * ★★ v1.0-E4.2：注册开关状态。
 *
 * 修复前的问题：这里调了 `allowRegister()` 但**只读了 `first_user`，
 * 把 `allow` 丢掉了** → 注册已关闭时，页面照常显示表单，
 * 用户认真填完点提交才收到 403。**体验上等于骗用户填一次**。
 *
 * ★ 判据：**能提前判定的拒绝，就不要等到提交才告诉用户**
 *   （同 MEMORY §37「能确定性判定的不用模型」的思想：
 *     把"注定失败"的路径尽量前置，别浪费用户的输入）。
 *
 * ★ 默认值用 `null` 表示"还没问出来" —— 此时**不显示任何提示**
 *   （避免网络慢时闪一下"已关闭"再变成"开放"，反而困惑）。
 */
const allowOpen = ref<boolean | null>(null)

onMounted(async () => {
  try {
    const r = await authApi.allowRegister()
    isFirstUser.value = Boolean(r.first_user)
    allowOpen.value = Boolean(r.allow)
  } catch {
    // ★ fail-open：探测失败不拦用户（后端仍会兜底校验）
    allowOpen.value = null
  }
})

function validate(): string {
  const u = username.value.trim()
  if (u.length < 3) return '用户名至少 3 个字符'
  if (password.value.length < 8) return '密码至少 8 位（安全要求）'
  if (password.value !== password2.value) return '两次输入的密码不一致'
  if (email.value && !/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(email.value)) {
    return '邮箱格式不正确'
  }
  return ''
}

async function onSubmit() {
  error.value = validate()
  if (error.value) return
  if (!agreed.value) {
    error.value = '请先阅读并同意《用户协议》与《隐私政策》'
    return
  }

  submitting.value = true
  try {
    await auth.register(username.value.trim(), password.value, email.value.trim() || undefined)
    ui.ok('注册成功，欢迎加入')
    // ★★ v1.0-E4 转化优化：优先回跳用户原本想去的地方
    //
    //   ★ 为什么重要：从定价页点"购买专业版"→ 被要求注册 → 注册完
    //     如果直接扔到工作台，用户得自己再找回定价页并重新选套餐。
    //     **这一段流失率极高**（等于把"已下决心付费"的用户半路丢掉）。
    //   ★ 安全：只接受站内相对路径（`/` 开头且不含 `//`），
    //     防止开放重定向漏洞（`?redirect=https://evil.com`）。
    const raw = String(route.query.redirect || '')
    const safe = raw.startsWith('/') && !raw.startsWith('//') ? raw : ''
    if (safe) {
      router.push(safe)
    } else {
      router.push({ name: 'home' })
    }
  } catch (e) {
    if (e instanceof ApiError) {
      error.value = e.status === 429
        ? '注册过于频繁，请稍后再试'
        : (e.message || '注册失败')
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
        <span class="auth__name">溯字 SOZEER</span>
      </div>

      <h1 class="auth__title">创建账号</h1>
      <p class="auth__sub">开始你的第一部作品</p>

      <!-- ★ 首个用户提示（体验优化，避免用户不知道自己是管理员） -->
      <div v-if="isFirstUser" class="auth__notice">
        你是第一位用户 —— 注册后将自动成为管理员，可访问管理后台。
      </div>

      <!-- ★★ v1.0-E4.2：注册已关闭 → 明确告知，不显示表单。
           为什么不做成"表单可填但提交报错"：
           那等于让用户白填一遍，是最容易被骂的交互之一。 -->
      <div v-if="allowOpen === false" class="auth__closed" role="status">
        <div class="auth__closed-ico" aria-hidden="true">🔒</div>
        <h2 class="auth__closed-title">当前未开放注册</h2>
        <p class="auth__closed-desc">
          站点管理员已关闭自助注册。<br>
          如果你已有账号，请直接登录；如需开通，请联系管理员。
        </p>
        <AppButton variant="primary" size="lg" block
                   @click="router.push({ name: 'login' })">
          去登录
        </AppButton>
        <p v-if="isFirstUser" class="auth__closed-hint">
          提示：检测到系统还没有用户 —— 请联系管理员在后台重新开放注册。
        </p>
      </div>

      <form v-else class="auth__form" @submit.prevent="onSubmit">
        <AppInput
          v-model="username"
          label="用户名"
          placeholder="3-32 位，用于登录"
          autocomplete="username"
          :maxlength="32"
        />
        <AppInput
          v-model="email"
          label="邮箱（可选）"
          type="email"
          placeholder="用于找回密码与接收通知"
          autocomplete="email"
          hint="不填也能注册，但无法找回密码"
        />
        <AppInput
          v-model="password"
          label="密码"
          type="password"
          placeholder="至少 8 位"
          autocomplete="new-password"
        />
        <AppInput
          v-model="password2"
          label="确认密码"
          type="password"
          placeholder="再输入一次"
          autocomplete="new-password"
          @enter="onSubmit"
        />

        <p v-if="error" class="auth__error" role="alert">{{ error }}</p>

        <!-- ★★ v1.0-E4.11（合规）：协议勾选 —— 未勾选禁用提交。
             链接在新窗口打开，勾选状态不因跳转丢失。 -->
        <label class="auth__agree">
          <input v-model="agreed" type="checkbox">
          <span>
            我已阅读并同意
            <a href="/terms" target="_blank">《用户协议》</a>与
            <a href="/privacy" target="_blank">《隐私政策》</a>
          </span>
        </label>

        <AppButton variant="primary" size="lg" block type="submit"
                   :loading="submitting" :disabled="!agreed">
          注册
        </AppButton>
      </form>

      <p class="auth__switch">
        已有账号？
        <RouterLink :to="{ name: 'login' }">去登录</RouterLink>
      </p>
    </div>
  </div>
</template>

<style scoped>
.auth {
  min-height: 100vh; display: grid; place-items: center;
  padding: var(--sp-5); background: var(--bg);
}
.auth__card {
  width: min(100%, 400px);
  padding: var(--sp-8) var(--sp-6);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  box-shadow: var(--sh-md);
}
.auth__brand { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: var(--sp-5); }
.auth__logo {
  width: 28px; height: 28px; display: grid; place-items: center;
  border-radius: 8px; background: var(--brand-500); color: #fff;
  font-size: 16px; font-family: serif;
}
.auth__name { font-size: var(--fs-sm); font-weight: var(--fw-bold); }
.auth__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.auth__sub { font-size: var(--fs-sm); color: var(--text-muted); margin-bottom: var(--sp-5); }
.auth__notice {
  padding: var(--sp-3);
  border-radius: var(--r-md);
  background: var(--brand-50);
  border: 1px solid var(--brand-200);
  color: var(--brand-600);
  font-size: var(--fs-xs);
  line-height: var(--lh-normal);
  margin-bottom: var(--sp-4);
}

/* ★★ v1.0-E4.2：注册关闭的提示（替代表单） */
.auth__closed {
  display: flex; flex-direction: column; align-items: center;
  gap: var(--sp-3); text-align: center;
  padding: var(--sp-5) var(--sp-3);
  border: 1px dashed var(--border);
  border-radius: var(--r-lg);
  background: var(--bg-sunken);
}
.auth__closed-ico { font-size: 32px; line-height: 1; }
.auth__closed-title { font-size: var(--fs-lg); margin: 0; }
.auth__closed-desc {
  margin: 0; font-size: var(--fs-sm);
  color: var(--text-muted); line-height: var(--lh-relaxed);
}
.auth__closed .btn { margin-top: var(--sp-2); }
.auth__closed-hint {
  margin: var(--sp-2) 0 0; font-size: var(--fs-xs);
  color: var(--warning, #b8860b);
}

.auth__form { display: flex; flex-direction: column; gap: var(--sp-4); }
.auth__agree {
  display: flex; gap: var(--sp-2); align-items: flex-start;
  font-size: var(--fs-xs); color: var(--text-muted); line-height: var(--lh-relaxed);
  cursor: pointer; user-select: none;
}
.auth__agree input { margin-top: 2px; accent-color: var(--brand); flex: none; }
.auth__agree a { color: var(--brand); text-decoration: none; }
.auth__agree a:hover { text-decoration: underline; }
.auth__error {
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-md);
  background: var(--danger-bg); border: 1px solid var(--danger);
  color: var(--danger); font-size: var(--fs-xs); line-height: var(--lh-normal);
}
.auth__switch { margin-top: var(--sp-5); font-size: var(--fs-sm); text-align: center; }
</style>
