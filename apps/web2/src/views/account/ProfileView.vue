<script setup lang="ts">
/**
 * ProfileView · 个人资料（v0.26.1）
 *
 * ★ 契约（对照 apps/api/routers/me.py:41）：
 *   GET   /api/me/profile → { id, username, nickname, bio, avatar, email,
 *     email_verified, phone(已脱敏), phone_bound, phone_verified, role,
 *     perms[], is_admin, totp_enabled, created_at, last_login_at }
 *   PATCH /api/me/profile → { ok, profile }
 *
 * ★ 两个不能踩的点：
 *   ① username 不可改（会让历史作品的作者信息错乱）
 *   ② phone 后端返回的是**已脱敏**值（138****8888）→ 绝不回填到输入框，
 *      否则用户一保存就把脱敏串写进库
 */
import { computed, onMounted, ref } from 'vue'
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'
import AppInput from '@/components/AppInput.vue'
import AppTag from '@/components/AppTag.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import { useUiStore } from '@/stores/ui'
import { meApi, type Profile } from '@/api/modules/me'

const ui = useUiStore()
const loading = ref(true)
const saving = ref(false)
const profile = ref<Profile | null>(null)

/**
 * ★★ 只提交后端接受的两个字段（nickname / bio）。
 *   邮箱有独立的验证流程 —— PATCH 传 email 会被**静默忽略**，
 *   所以这里不做成可编辑输入框，避免"改了没生效"的困惑。
 */
const form = ref({ nickname: '', bio: '' })
const dirty = computed(() =>
  !!profile.value && (
    form.value.nickname !== (profile.value.nickname || '') ||
    form.value.bio !== (profile.value.bio || '')
  ))

async function load() {
  loading.value = true
  try {
    const p = await meApi.profile()
    profile.value = p
    form.value = {
      nickname: p.nickname || '',
      bio: p.bio || '',
    }
  } catch (e) {
    ui.err((e as Error).message || '加载资料失败')
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!dirty.value) return
  saving.value = true
  try {
    await meApi.updateProfile(form.value)
    ui.ok('资料已保存')
    await load()
  } catch (e) {
    ui.err((e as Error).message || '保存失败')
  } finally {
    saving.value = false
  }
}

function reset() {
  if (!profile.value) return
  form.value = {
    nickname: profile.value.nickname || '',
    bio: profile.value.bio || '',
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <header class="page__head">
      <h1 class="page__title">个人资料</h1>
      <p class="page__sub">昵称与简介会显示在作品署名处</p>
    </header>

    <AppSkeleton v-if="loading" :rows="5" />

    <template v-else-if="profile">
      <AppCard title="基本信息">
        <div class="grid">
          <div class="field">
            <label>用户名</label>
            <div class="readonly">
              {{ profile.username }}
              <AppTag v-if="profile.is_admin" kind="brand">管理员</AppTag>
            </div>
            <p class="hint">用户名不可修改（会影响历史作品的署名）</p>
          </div>

          <AppInput
            v-model="form.nickname"
            label="昵称"
            placeholder="想被怎么称呼？"
            :maxlength="32"
            hint="留空则显示用户名"
          />

          <div class="field">
            <label>邮箱</label>
            <div class="readonly">
              <template v-if="profile.email">
                {{ profile.email }}
                <AppTag :kind="profile.email_verified ? 'success' : 'warning'">
                  {{ profile.email_verified ? '已验证' : '未验证' }}
                </AppTag>
              </template>
              <span v-else class="muted">未绑定</span>
            </div>
            <p class="hint">
              邮箱用于登录与找回密码，有独立的验证流程，暂不支持在此直接修改
            </p>
          </div>

          <div class="field">
            <label>手机号</label>
            <div class="readonly">
              <template v-if="profile.phone_bound">
                {{ profile.phone }}
                <AppTag :kind="profile.phone_verified ? 'success' : 'warning'">
                  {{ profile.phone_verified ? '已验证' : '未验证' }}
                </AppTag>
              </template>
              <span v-else class="muted">未绑定</span>
            </div>
            <p class="hint">为保护隐私仅显示脱敏结果；更换请前往安全设置</p>
          </div>

          <div class="field">
            <label>个人简介</label>
            <textarea
              v-model="form.bio"
              class="textarea"
              rows="3"
              maxlength="200"
              placeholder="一句话介绍自己"
            />
            <p class="hint">{{ form.bio.length }} / 200</p>
          </div>
        </div>

        <template #footer>
          <div class="actions">
            <AppButton variant="primary" :loading="saving" :disabled="!dirty" @click="save">
              保存修改
            </AppButton>
            <AppButton v-if="dirty" variant="ghost" @click="reset">撤销</AppButton>
            <span v-if="dirty" class="muted tiny">有未保存的修改</span>
          </div>
        </template>
      </AppCard>

      <AppCard title="账号信息" class="mt">
        <dl class="meta">
          <div><dt>注册时间</dt><dd>{{ profile.created_at || '—' }}</dd></div>
          <div><dt>最近登录</dt><dd>{{ profile.last_login_at || '—' }}</dd></div>
          <div>
            <dt>二次验证</dt>
            <dd>
              <AppTag :kind="profile.totp_enabled ? 'success' : 'default'">
                {{ profile.totp_enabled ? '已开启' : '未开启' }}
              </AppTag>
            </dd>
          </div>
          <div><dt>角色</dt><dd>{{ profile.role }}</dd></div>
        </dl>
        <p class="hint" style="margin-top:12px">
          共 {{ (profile.perms || []).length }} 项权限。
          <RouterLink :to="{ name: 'account-security' }">前往安全设置</RouterLink>
        </p>
      </AppCard>
    </template>

    <AppCard v-else title="加载失败">
      <AppButton variant="primary" @click="load">重试</AppButton>
    </AppCard>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 760px; margin: 0 auto; }
.page__head { margin-bottom: var(--sp-5); }
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); }
.grid { display: flex; flex-direction: column; gap: var(--sp-4); }
.field label { display: block; font-size: var(--fs-sm); margin-bottom: 6px; color: var(--text); }
.readonly {
  display: flex; align-items: center; gap: var(--sp-2);
  padding: 9px 12px; border-radius: 8px;
  background: var(--surface-2); color: var(--text-muted); font-size: var(--fs-sm);
}
.textarea {
  width: 100%; padding: 10px 12px; border: 1px solid var(--border);
  border-radius: 8px; background: var(--surface); color: var(--text);
  font-family: inherit; font-size: var(--fs-sm); line-height: 1.7; resize: vertical;
}
.textarea:focus { outline: 2px solid var(--brand); outline-offset: -1px; }
.hint { margin: 6px 0 0; font-size: var(--fs-xs); color: var(--text-faint); }
.hint a { color: var(--brand); }
.actions { display: flex; align-items: center; gap: var(--sp-3); }
.mt { margin-top: var(--sp-4); }
.meta { display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--sp-3); margin: 0; }
.meta > div { display: flex; justify-content: space-between; gap: var(--sp-2);
  padding-bottom: var(--sp-2); border-bottom: 1px dashed var(--border); }
.meta dt { color: var(--text-muted); font-size: var(--fs-sm); }
.meta dd { margin: 0; font-size: var(--fs-sm); color: var(--text); }
@media (max-width: 560px) { .meta { grid-template-columns: 1fr; } }
</style>
