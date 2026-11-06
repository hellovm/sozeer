<script setup lang="ts">
/**
 * AdminModelsView.vue · 模型配置（v0.26.3 G0-6 续）
 *
 * ★ 对照旧版 RENDER.models + modelForm + comfyWizard + routeForm 核对。
 *
 * ★★ 本页承载三个核心能力（用户需求 3/4）：
 *   ① **一个模型槽可挂多个供应商端点** —— 多 key 负载分流 / 故障转移
 *   ② **按套餐指定模型** —— 免费版用便宜模型、Pro 用强模型
 *   ③ **ComfyUI 三步接入向导** —— 选模板 + 填地址 + 测试连接（之前要 10+ 步）
 *
 * ★★ 权限：`model.manage` **仅 owner**（含 api_key 与计费参数，直接决定成本）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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
import { useAdminStore } from '@/stores/admin'
import {
  modelsApi, type ModelProfileRow, type ComfyTemplate,
} from '@/api/admin_modules_extra'

const route = useRoute()
const ui = useUiStore()
const adm = useAdminStore()

const canManage = computed(() => adm.can('model.manage'))

const loading = ref(true)
const err = ref('')
const tab = ref<'slots' | 'routes' | 'templates'>('slots')

interface Overview {
  profiles: ModelProfileRow[]
  routes: Array<{ id: string; plan_key: string; role: string; profile_ids: string[] }>
  roles: Record<string, string>
  provider_kinds: Record<string, string>
  media_roles: string[]
  circuit: { fail_threshold: number; open_seconds: number }
  templates: Array<ComfyTemplate & { requires_node?: string }>
  plans: Array<{ key: string; name: string }>
}
const d = ref<Overview | null>(null)

/** 按角色分组（同角色的多个槽 = 降级链） */
const byRole = computed(() => {
  const m: Record<string, ModelProfileRow[]> = {}
  for (const p of d.value?.profiles || []) {
    (m[p.role] = m[p.role] || []).push(p)
  }
  return m
})

/* ══════════════════ 供应商端点编辑 ══════════════════ */

interface ProviderDraft {
  name: string
  base_url: string
  api_key: string
  weight: number
  enabled: number
  has_key?: boolean
}

/* ══════════════════ 模型槽表单 ══════════════════ */

const formOpen = ref(false)
const editing = ref<ModelProfileRow | null>(null)
const saving = ref(false)
const formErr = ref('')

interface ProfileDraft {
  id: string
  name: string
  provider: string
  base_url: string
  api_key: string
  model_id: string
  role: string
  is_default: number
  enabled: number
  context_window: number
  price_in: number
  price_out: number
  temperature: number
  max_tokens: number
  providers: ProviderDraft[]
}

function blankProfile(): ProfileDraft {
  return {
    id: '', name: '', provider: 'openai-compatible', base_url: '', api_key: '',
    model_id: '', role: 'default', is_default: 0, enabled: 1,
    context_window: 128000, price_in: 2.0, price_out: 8.0,
    temperature: 0.85, max_tokens: 8192, providers: [],
  }
}
const f = ref<ProfileDraft>(blankProfile())

/** ★ 是否媒体类角色（走 media.py，provider 选项不同） */
const isMediaRole = computed(() =>
  (d.value?.media_roles || []).includes(f.value.role))

/** 按角色过滤 provider 选项（文本模型不该看到 ComfyUI 选项） */
const providerOptions = computed(() => {
  const all = d.value?.provider_kinds || {}
  return Object.entries(all).map(([k, v]) => ({ k, v }))
})

/** ★ ComfyUI 类 provider 需要不同的表单（模板键而非 model_id） */
const isComfy = computed(() => f.value.provider.startsWith('comfyui'))

function openCreate() {
  if (!canManage.value) { ui.err('需要「模型管理」权限（model.manage，仅所有者）'); return }
  editing.value = null
  f.value = blankProfile()
  formErr.value = ''
  formOpen.value = true
}

function openEdit(p: ModelProfileRow) {
  if (!canManage.value) { ui.err('需要「模型管理」权限（model.manage，仅所有者）'); return }
  editing.value = p
  f.value = {
    id: p.id,
    name: p.name || '',
    provider: p.provider || 'openai-compatible',
    base_url: (p.base_url as string) || '',
    api_key: '',
    model_id: (p.model_id as string) || '',
    role: (p.role as string) || 'default',
    is_default: Number(p.is_default ?? 0),
    enabled: Number(p.enabled ?? 1),
    context_window: Number(p.context_window ?? 128000),
    price_in: Number(p.price_in ?? 2),
    price_out: Number(p.price_out ?? 8),
    temperature: Number(p.temperature ?? 0.85),
    max_tokens: Number(p.max_tokens ?? 8192),
    providers: (p.providers || []).map((x) => ({
      name: x.name || '',
      base_url: x.base_url || '',
      api_key: '',
      weight: Number(x.weight ?? 100),
      enabled: Number(x.enabled ?? 1),
      has_key: x.api_key_set,
    })),
  }
  formErr.value = ''
  formOpen.value = true
}

function addProvider() {
  f.value.providers.push({
    name: `线路 ${f.value.providers.length + 1}`,
    base_url: '', api_key: '', weight: 100, enabled: 1,
  })
}

function delProvider(i: number) {
  f.value.providers.splice(i, 1)
}

async function saveProfile() {
  formErr.value = ''
  const v = f.value
  if (!v.name.trim()) { formErr.value = '请填写名称'; return }
  if (!v.model_id.trim()) { formErr.value = isComfy ? '请填写模板键或工作流 JSON' : '请填写模型 ID'; return }
  if (!isComfy && !v.base_url.trim()) { formErr.value = '请填写 API 地址'; return }
  saving.value = true
  try {
    await modelsApi.upsertProfile({
      id: v.id, name: v.name, provider: v.provider, base_url: v.base_url,
      api_key: v.api_key, model_id: v.model_id, role: v.role,
      is_default: v.is_default, enabled: v.enabled,
      context_window: v.context_window, price_in: v.price_in, price_out: v.price_out,
      temperature: v.temperature, max_tokens: v.max_tokens,
      providers: v.providers.filter((p) => p.base_url.trim()),
    })
    ui.ok('已保存')
    formOpen.value = false
    await load()
  } catch (e) {
    formErr.value = (e as Error).message || '保存失败'
  } finally {
    saving.value = false
  }
}

/* ══════════════════ 连接测试 ══════════════════ */

const testing = ref('')
/** ★ 测试结果（含逐项检查 + 可执行建议） */
const testResult = ref<{
  ok: boolean
  title: string
  checks: Array<{ ok: boolean; name: string; detail: string }>
  hint: string
} | null>(null)
const testOpen = computed({
  get: () => testResult.value !== null,
  set: (v: boolean) => { if (!v) testResult.value = null },
})

async function testProfile(p: ModelProfileRow) {
  testing.value = p.id
  try {
    if ((p.provider || '').startsWith('comfyui')) {
      const r = await modelsApi.testComfyui((p.base_url as string) || '')
      const checks = (r.checks as Array<{ ok: boolean; name: string; detail: string }>) || []
      testResult.value = {
        ok: !!r.ok,
        title: r.ok ? 'ComfyUI 连接正常' : 'ComfyUI 连接失败',
        checks,
        hint: String(r.hint || r.message || r.advice || ''),
      }
    } else {
      // ★ 非 ComfyUI（agnus / openai-compatible / minimax / custom）：
      //   走通用 test-model 端点（后端按 provider 分支，OpenAI 兼容类打 /models 探针，
      //   靠 profile_id 从 keyvault 还原真实密钥 —— 前端只有掩码）。
      //   ★ 修复根因：原来误走 test-comfyui（打 ComfyUI 专有 /system_stats）
      //     → agnus 必然 404 → 误报"ComfyUI 已启动"。
      const r = await modelsApi.testModel({
        provider: (p.provider as string) || 'openai-compatible',
        baseUrl: ((p.base_url as string) || '').replace(/\/$/, ''),
        profileId: p.id,
        modelId: String(p.model_id || ''),
      })
      const checks = (r.checks as Array<{ ok: boolean; name: string; detail: string }>) || []
      testResult.value = {
        ok: !!r.ok,
        title: r.ok ? '连接正常' : '连接失败',
        checks,
        hint: String(r.hint || r.message || ''),
      }
    }
  } catch (e) {
    testResult.value = {
      ok: false, title: '测试请求失败', checks: [],
      hint: (e as Error).message || '未知错误',
    }
  } finally {
    testing.value = ''
  }
}

/* ══════════════════ 删除 ══════════════════ */

const pendingDel = ref<ModelProfileRow | null>(null)
const deleting = ref(false)
const delOpen = computed({
  get: () => pendingDel.value !== null,
  set: (v: boolean) => { if (!v) pendingDel.value = null },
})

async function doDelete(reason: string) {
  const p = pendingDel.value
  if (!p) return
  deleting.value = true
  try {
    await modelsApi.removeProfile(p.id)
    ui.ok('已删除')
    pendingDel.value = null
    await load()
  } catch (e) {
    ui.err((e as Error).message || '删除失败')
  } finally {
    deleting.value = false
  }
}

/* ══════════════════ ComfyUI 向导 ══════════════════ */

const wzOpen = ref(false)
const wzStep = ref(1)
const wz = ref({
  template: '',
  base_url: 'http://localhost:8188',
  token: '',
  name: '',
  role: 'video',
})
const wzTesting = ref(false)
const wzTest = ref<{ ok: boolean; hint: string } | null>(null)

const comfyTemplates = computed(() =>
  (d.value?.templates || []).filter((t) =>
    String(t.key).includes('ltx') || String(t.key).includes('h3')
    || String(t.key).includes('minimax') || String(t.key).includes('comfy')))

function openWizard() {
  if (!canManage.value) { ui.err('需要「模型管理」权限（model.manage，仅所有者）'); return }
  wzStep.value = 1
  wz.value = { template: '', base_url: 'http://localhost:8188', token: '', name: '', role: 'video' }
  wzTest.value = null
  wzOpen.value = true
}

async function wizardTest() {
  wzTesting.value = true
  wzTest.value = null
  try {
    const r = await modelsApi.testComfyui(wz.value.base_url, wz.value.token)
    wzTest.value = { ok: !!r.ok, hint: String(r.hint || r.message || r.advice || '') }
  } catch (e) {
    wzTest.value = { ok: false, hint: (e as Error).message || '连接失败' }
  } finally {
    wzTesting.value = false
  }
}

async function wizardSave() {
  saving.value = true
  try {
    await modelsApi.upsertProfile({
      name: wz.value.name || wz.value.template,
      provider: 'comfyui',
      base_url: wz.value.base_url,
      api_key: wz.value.token,
      model_id: wz.value.template,          // ★ 模板键直接填 model_id
      role: wz.value.role,
      enabled: 1,
    })
    ui.ok('ComfyUI 模型已接入')
    wzOpen.value = false
    await load()
  } catch (e) {
    ui.err((e as Error).message || '接入失败')
  } finally {
    saving.value = false
  }
}

/* ══════════════════ 套餐路由 ══════════════════ */

const routeOpen = ref(false)
const rt = ref({ plan_key: '', role: 'writer', profile_ids: [] as string[] })

function openRoute() {
  if (!canManage.value) { ui.err('需要「模型管理」权限（model.manage，仅所有者）'); return }
  rt.value = { plan_key: d.value?.plans?.[0]?.key || '', role: 'writer', profile_ids: [] }
  routeOpen.value = true
}

/** 当前角色下可选的模型槽（路由只能从同角色的槽里选） */
const routeCandidates = computed(() =>
  (d.value?.profiles || []).filter((p) => p.role === rt.value.role))

function toggleRouteModel(id: string) {
  const i = rt.value.profile_ids.indexOf(id)
  if (i >= 0) rt.value.profile_ids.splice(i, 1)
  else rt.value.profile_ids.push(id)
}

async function saveRoute() {
  if (!rt.value.plan_key) { ui.err('请选择套餐'); return }
  saving.value = true
  try {
    await modelsApi.setRoute(rt.value.plan_key, rt.value.role, rt.value.profile_ids)
    ui.ok('路由已保存')
    routeOpen.value = false
    await load()
  } catch (e) {
    ui.err((e as Error).message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function load() {
  loading.value = true
  err.value = ''
  try {
    d.value = (await modelsApi.overview()) as unknown as Overview
  } catch (e) {
    d.value = null
    err.value = (e as Error).message || '加载模型配置失败'
  } finally {
    loading.value = false
  }
}

function switchTab(t: string) {
  tab.value = t as typeof tab.value
}

onMounted(load)
watch(() => route.query._r, () => { load() })

/* ── ★ v1.0-E4.15 编辑锁：表单/向导/路由弹窗打开期间，hold 住后台 60s 自动刷新 ──
   任一编辑弹窗开着 → 对应一把锁；全关 → 锁清零。AdminLayout 的软刷新定时器
   读到 isEditing 就跳过，绝不冲掉输入中的状态。
   用"本页贡献锁数"（myEditLocks）与全局计数解耦：
   · watcher 算出当前应持有的锁数（几个弹窗开着），按差值 beginEdit/endEdit 补齐
   · onBeforeUnmount 把本页所有锁释放干净（防泄漏把别的页也锁死，MEMORY §资源释放穷举出口） */
let myEditLocks = 0
watch([() => formOpen.value, () => wzOpen.value, () => routeOpen.value],
  (openStates) => {
    const target = openStates.filter(Boolean).length
    while (myEditLocks < target) { adm.beginEdit(); myEditLocks++ }
    while (myEditLocks > target) { adm.endEdit(); myEditLocks-- }
  },
  { immediate: true },
)
onBeforeUnmount(() => {
  while (myEditLocks > 0) { adm.endEdit(); myEditLocks-- }
})
</script>

<template>
  <AppPage
    title="模型配置"
    subtitle="一个模型槽可挂多个供应商端点（多 key 负载分流）；同一角色可挂多个模型（按序降级）。"
    :loading="loading"
    :error="err"
  >
    <template #actions>
      <AppTag v-if="!canManage" size="sm" kind="warning">只读（需 model.manage）</AppTag>
      <AppTag v-if="d" size="sm">
        {{ d.profiles.length }} 个模型槽 · {{ d.templates.length }} 个模板
      </AppTag>
      <AppButton v-if="canManage" size="sm" variant="primary" @click="openCreate">＋ 新增模型槽</AppButton>
      <AppButton v-if="canManage" size="sm" variant="ghost" @click="openWizard">🧭 ComfyUI 向导</AppButton>
      <AppButton v-if="canManage" size="sm" variant="ghost" @click="openRoute">套餐路由</AppButton>
      <AppButton size="sm" variant="ghost" @click="load">刷新</AppButton>
    </template>

    <template #toolbar>
      <AppTabs
        :model-value="tab"
        :items="[
          { key: 'slots', label: '模型槽', badge: d?.profiles.length },
          { key: 'routes', label: '套餐路由', badge: d?.routes.length },
          { key: 'templates', label: '工作流模板', badge: d?.templates.length },
        ]"
        @update:model-value="switchTab"
      />
      <span class="sp" />
      <span v-if="d" class="hint-inline">
        熔断：连续失败 {{ d.circuit.fail_threshold }} 次 → 停 {{ d.circuit.open_seconds }} 秒
      </span>
    </template>

    <!-- ★ 权限说明 -->
    <p v-if="!canManage" class="permbar">
      当前账号<b>没有模型管理权限</b>（<code>model.manage</code>，仅所有者可用）。
      原因：模型配置含 <code>api_key</code> 与计费参数，直接决定成本，因此权限刻意收得更紧。
    </p>

    <!-- ══════════ 模型槽 ══════════ -->
    <template v-if="!loading && tab === 'slots'">
      <AppEmpty
        v-if="!d?.profiles.length"
        icon="⚙"
        title="尚未配置任何模型"
        description="点「新增模型槽」开始，或用「ComfyUI 向导」快速接入本地视频模型。"
      />
      <AppCard
        v-for="(list, role) in byRole"
        v-else
        :key="role"
        :title="d?.roles?.[role] || role"
        :subtitle="`role=${role} · ${list.length} 个${list.length > 1 ? '（按序降级）' : ''}`"
        class="mt"
      >
        <div class="tblwrap">
          <table class="tbl">
            <thead>
              <tr>
                <th style="width:150px">名称</th>
                <th style="width:160px">模型 ID / 模板键</th>
                <th style="width:150px">类型</th>
                <th style="width:110px">端点</th>
                <th style="width:70px">默认</th>
                <th style="width:70px">状态</th>
                <th style="width:190px" class="ops">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="p in list" :key="p.id">
                <td>{{ p.name || '—' }}</td>
                <td><code class="code">{{ String(p.model_id).slice(0, 40) }}</code></td>
                <td class="dim">{{ p.provider }}</td>
                <td>
                  <template v-if="(p.providers || []).length">
                    <AppTag size="sm">{{ (p.providers || []).length }} 个</AppTag>
                    <span v-if="(p.providers || []).some((x) => x.open_until && x.open_until > '')"
                          class="danger"> 熔断中</span>
                  </template>
                  <span v-else class="dim">继承槽配置</span>
                </td>
                <td>{{ p.is_default ? '是' : '—' }}</td>
                <td>{{ p.enabled ? '启用' : '停用' }}</td>
                <td class="ops">
                  <button class="op" @click="openEdit(p)">编辑</button>
                  <button class="op" :disabled="testing === p.id" @click="testProfile(p)">
                    {{ testing === p.id ? '测试中…' : '测试' }}
                  </button>
                  <button class="op danger" @click="pendingDel = p">删除</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </AppCard>
    </template>

    <!-- ══════════ 套餐路由 ══════════ -->
    <template v-if="!loading && tab === 'routes'">
      <AppCard title="套餐 → 模型路由" subtitle="不同套餐用不同模型（免费版用便宜模型、Pro 用强模型）">
        <AppEmpty v-if="!d?.routes.length" icon="≡" title="尚未配置套餐路由"
                  description="未配置时全部套餐走「全局默认」（按角色降级链）。" />
        <div v-else class="tblwrap">
          <table class="tbl">
            <thead>
              <tr>
                <th style="width:140px">套餐</th>
                <th style="width:160px">角色</th>
                <th>优先模型（按序降级）</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in d.routes" :key="r.id">
                <td><AppTag size="sm">{{ r.plan_key }}</AppTag></td>
                <td>{{ d.roles?.[r.role] || r.role }}</td>
                <td>
                  <code class="code">{{ r.profile_ids.join(' → ') || '（空）' }}</code>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <template #footer>
          <AppButton v-if="canManage" size="sm" variant="ghost" @click="openRoute">新增/修改路由</AppButton>
        </template>
      </AppCard>
    </template>

    <!-- ══════════ 模板 ══════════ -->
    <template v-if="!loading && tab === 'templates'">
      <AppCard title="内置 ComfyUI 工作流模板"
                subtitle="接 ComfyUI 视频模型时，在模型槽的「模型 ID」里直接填模板键即可，无需自己搭节点图">
        <div class="tblwrap">
          <table class="tbl">
            <thead>
              <tr>
                <th style="width:190px">模板键</th>
                <th style="width:250px">名称</th>
                <th>说明</th>
                <th style="width:140px">依赖节点</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="t in d?.templates || []" :key="t.key">
                <td><code class="code">{{ t.key }}</code></td>
                <td>{{ t.name }}</td>
                <td class="dim">{{ t.desc || '—' }}</td>
                <td class="dim">{{ t.requires_node || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </AppCard>
    </template>

    <template #hint>
      ★ <b>负载分流</b>：同一模型槽挂多个供应商端点（各带 key），按权重分摊 ——
      既提升吞吐，也是故障转移。<br>
      ★ <b>按套餐路由</b>：为不同套餐指定不同的模型槽，未配置时回落到全局默认。<br>
      ★ <b>零回归</b>：无供应商记录时回退到槽自身配置，老部署行为完全不变。
    </template>
  </AppPage>

  <!-- ═══════════ 模型槽编辑 ═══════════ -->
  <AppModal v-model="formOpen" :title="editing ? '编辑模型槽' : '新增模型槽'" width="700px">
    <div class="form">
      <div class="form__grid">
        <label class="fld">
          <span class="fld__l">名称 <i class="req">*</i></span>
          <AppInput v-model="f.name" placeholder="如「写正文 · 主模型」" />
        </label>
        <label class="fld">
          <span class="fld__l">功能角色 <i class="req">*</i></span>
          <select v-model="f.role" class="sel">
            <option v-for="(label, k) in d?.roles || {}" :key="k" :value="k">{{ label }}</option>
          </select>
        </label>
      </div>

      <label class="fld">
        <span class="fld__l">供应商类型 <i class="req">*</i></span>
        <select v-model="f.provider" class="sel">
          <option v-for="o in providerOptions" :key="o.k" :value="o.k">{{ o.v }}</option>
        </select>
        <span v-if="isMediaRole" class="fld__h">
          当前是媒体类角色（{{ f.role }}）—— 会走媒体适配器而非文本接口。
        </span>
      </label>

      <!-- ★ ComfyUI：模型 ID 填模板键 -->
      <label class="fld">
        <span class="fld__l">
          {{ isComfy ? '工作流模板键 / JSON' : '模型 ID' }} <i class="req">*</i>
        </span>
        <select v-if="isComfy" v-model="f.model_id" class="sel">
          <option value="">— 选择内置模板 —</option>
          <option v-for="t in d?.templates || []" :key="t.key" :value="t.key">
            {{ t.name }}（{{ t.key }}）
          </option>
        </select>
        <AppInput v-else v-model="f.model_id" placeholder="如 deepseek-v4 / agnes-image-2.5-flash" />
        <span v-if="isComfy" class="fld__h">
          ★ 选内置模板即可，无需自己搭节点图。也可以填自建工作流的 API JSON。
        </span>
      </label>

      <label class="fld">
        <span class="fld__l">{{ isComfy ? 'ComfyUI 地址' : 'API 地址（base_url）' }} <i class="req">*</i></span>
        <AppInput v-model="f.base_url" :placeholder="isComfy ? 'http://localhost:8188' : 'https://api.example.com/v1'" />
      </label>

      <label class="fld">
        <span class="fld__l">{{ isComfy ? 'Token（可选）' : 'API Key' }}</span>
        <AppInput v-model="f.api_key" type="password"
                  :placeholder="editing && (editing.api_key_set) ? '已配置（留空则不修改）' : 'sk-…'" />
      </label>

      <!-- 计费与参数 -->
      <div class="form__grid form__grid--3">
        <label class="fld">
          <span class="fld__l">输入价（元/百万 token）</span>
          <AppInput :model-value="String(f.price_in)" type="number"
                    @update:model-value="(v: string) => f.price_in = Number(v) || 0" />
        </label>
        <label class="fld">
          <span class="fld__l">输出价（元/百万 token）</span>
          <AppInput :model-value="String(f.price_out)" type="number"
                    @update:model-value="(v: string) => f.price_out = Number(v) || 0" />
        </label>
        <label class="fld">
          <span class="fld__l">温度</span>
          <AppInput :model-value="String(f.temperature)" type="number"
                    @update:model-value="(v: string) => f.temperature = Number(v) || 0" />
        </label>
      </div>

      <div class="form__grid form__grid--3">
        <label class="fld">
          <span class="fld__l">上下文窗口</span>
          <AppInput :model-value="String(f.context_window)" type="number"
                    @update:model-value="(v: string) => f.context_window = Number(v) || 0" />
        </label>
        <label class="fld">
          <span class="fld__l">最大输出</span>
          <AppInput :model-value="String(f.max_tokens)" type="number"
                    @update:model-value="(v: string) => f.max_tokens = Number(v) || 0" />
        </label>
        <label class="fld">
          <span class="fld__l">默认槽</span>
          <select v-model.number="f.is_default" class="sel">
            <option :value="0">否</option>
            <option :value="1">是（该角色的默认）</option>
          </select>
        </label>
      </div>

      <!-- ★★ 多供应商端点（负载分流） -->
      <div class="prov">
        <div class="prov__head">
          <b>供应商端点（可选）</b>
          <AppButton size="sm" variant="ghost" @click="addProvider">＋ 添加线路</AppButton>
        </div>
        <p class="prov__d">
          同一个模型挂多个端点（各带 key），按<b>权重</b>分摊请求 ——
          既提升吞吐，也是故障转移（连续失败会自动熔断该端点）。
          <b>留空则直接用上面的槽配置</b>（老行为不变）。
        </p>

        <div v-for="(pv, i) in f.providers" :key="i" class="prov__i">
          <div class="prov__g">
            <AppInput v-model="pv.name" placeholder="展示名（如 主线路）" />
            <AppInput v-model="pv.base_url" placeholder="https://api.example.com/v1" />
            <AppInput v-model="pv.api_key" type="password"
                      :placeholder="pv.has_key ? '已配置（留空不修改）' : 'sk-…'" />
            <AppInput
              :model-value="String(pv.weight)" type="number" placeholder="权重"
              @update:model-value="(v: string) => pv.weight = Number(v) || 0"
            />
            <button class="op danger" @click="delProvider(i)">移除</button>
          </div>
        </div>
      </div>

      <label class="check">
        <input type="checkbox" :checked="f.enabled === 1"
               @change="f.enabled = ($event.target as HTMLInputElement).checked ? 1 : 0">
        <span>启用</span>
      </label>

      <p v-if="formErr" class="form__err">{{ formErr }}</p>
    </div>

    <template #footer>
      <AppButton variant="ghost" :disabled="saving" @click="formOpen = false">取消</AppButton>
      <AppButton variant="primary" :disabled="saving" :loading="saving" @click="saveProfile">
        {{ editing ? '保存修改' : '创建' }}
      </AppButton>
    </template>
  </AppModal>

  <!-- ═══════════ ComfyUI 向导 ═══════════ -->
  <AppModal v-model="wzOpen" title="ComfyUI 快速接入向导" width="600px">
    <div class="wz">
      <ol class="wz__steps">
        <li :class="{ on: wzStep === 1, done: wzStep > 1 }">① 选模板</li>
        <li :class="{ on: wzStep === 2, done: wzStep > 2 }">② 填地址</li>
        <li :class="{ on: wzStep === 3 }">③ 测试并保存</li>
      </ol>

      <div v-if="wzStep === 1" class="wz__b">
        <p class="wz__d">选择要使用的视频模型工作流。<b>无需自己搭节点图</b>。</p>
        <div class="wz__list">
          <button
            v-for="t in comfyTemplates"
            :key="t.key"
            class="wz__t"
            :class="{ on: wz.template === t.key }"
            @click="wz.template = t.key; wz.name = t.name"
          >
            <b>{{ t.name }}</b>
            <code>{{ t.key }}</code>
            <span v-if="t.desc">{{ t.desc }}</span>
          </button>
          <p v-if="!comfyTemplates.length" class="dim">
            未取到模板清单。请确认后端已部署 <code>comfy_workflows.py</code>。
          </p>
        </div>
        <label class="fld">
          <span class="fld__l">功能角色</span>
          <select v-model="wz.role" class="sel">
            <option value="video">视频生成（分镜视频 / 成片）</option>
            <option value="image">图片生成（参考图 / 分镜图）</option>
          </select>
        </label>
      </div>

      <div v-else-if="wzStep === 2" class="wz__b">
        <p class="wz__d">填 ComfyUI 服务地址。<b>注意要从服务端能访问到</b>（内网地址即可）。</p>
        <label class="fld">
          <span class="fld__l">ComfyUI 地址 <i class="req">*</i></span>
          <AppInput v-model="wz.base_url" placeholder="http://localhost:8188" />
          <span class="fld__h">格式：http://主机:端口（默认端口 8188）</span>
        </label>
        <label class="fld">
          <span class="fld__l">Token（可选）</span>
          <AppInput v-model="wz.token" type="password" placeholder="仅当 ComfyUI 开启了鉴权时填" />
        </label>
        <label class="fld">
          <span class="fld__l">显示名</span>
          <AppInput v-model="wz.name" :placeholder="wz.template || 'ComfyUI 视频模型'" />
        </label>
      </div>

      <div v-else class="wz__b">
        <p class="wz__d">先测试连接，确认无误后保存。</p>
        <div class="wz__sum">
          <div><b>模板</b><code>{{ wz.template }}</code></div>
          <div><b>地址</b><code>{{ wz.base_url }}</code></div>
          <div><b>角色</b><span>{{ wz.role === 'video' ? '视频生成' : '图片生成' }}</span></div>
        </div>
        <AppButton size="sm" variant="ghost" :loading="wzTesting" @click="wizardTest">
          测试连接
        </AppButton>
        <div v-if="wzTest" class="wz__r" :class="wzTest.ok ? 'ok' : 'bad'">
          <b>{{ wzTest.ok ? '✅ 连接正常' : '❌ 连接失败' }}</b>
          <p v-if="wzTest.hint">{{ wzTest.hint }}</p>
        </div>
      </div>
    </div>

    <template #footer>
      <AppButton v-if="wzStep > 1" variant="ghost" @click="wzStep--">上一步</AppButton>
      <AppButton v-if="wzStep < 3" variant="primary"
                 :disabled="wzStep === 1 ? !wz.template : !wz.base_url"
                 @click="wzStep++">下一步</AppButton>
      <AppButton v-else variant="primary" :disabled="saving" :loading="saving" @click="wizardSave">
        保存接入
      </AppButton>
    </template>
  </AppModal>

  <!-- ═══════════ 套餐路由 ═══════════ -->
  <AppModal v-model="routeOpen" title="套餐模型路由" width="560px">
    <div class="form">
      <div class="form__grid">
        <label class="fld">
          <span class="fld__l">套餐</span>
          <select v-model="rt.plan_key" class="sel">
            <option v-for="p in d?.plans || []" :key="p.key" :value="p.key">
              {{ p.name }}（{{ p.key }}）
            </option>
          </select>
        </label>
        <label class="fld">
          <span class="fld__l">功能角色</span>
          <select v-model="rt.role" class="sel" @change="rt.profile_ids = []">
            <option v-for="(label, k) in d?.roles || {}" :key="k" :value="k">{{ label }}</option>
          </select>
        </label>
      </div>

      <div class="fld">
        <span class="fld__l">优先模型（按点击顺序 = 降级顺序）</span>
        <div class="cands">
          <button
            v-for="c in routeCandidates"
            :key="c.id"
            class="cand"
            :class="{ on: rt.profile_ids.includes(c.id) }"
            @click="toggleRouteModel(c.id)"
          >
            <span class="cand__i">{{ rt.profile_ids.indexOf(c.id) + 1 || '' }}</span>
            {{ c.name || c.model_id }}
          </button>
          <p v-if="!routeCandidates.length" class="dim">
            该角色下还没有模型槽，请先在「模型槽」里添加。
          </p>
        </div>
      </div>

      <p class="form__note">
        选中的顺序即降级顺序：先试第 1 个，失败自动切第 2 个。
        <b>留空 = 使用全局默认</b>。
      </p>
    </div>

    <template #footer>
      <AppButton variant="ghost" :disabled="saving" @click="routeOpen = false">取消</AppButton>
      <AppButton variant="primary" :disabled="saving" :loading="saving" @click="saveRoute">保存</AppButton>
    </template>
  </AppModal>

  <!-- ═══════════ 测试结果 ═══════════ -->
  <AppModal v-model="testOpen" :title="testResult?.title || '测试结果'" width="520px">
    <div v-if="testResult" class="tr">
      <div v-if="testResult.checks.length" class="tr__checks">
        <div v-for="(c, i) in testResult.checks" :key="i" class="tr__c">
          <span :class="c.ok ? 'ok' : 'bad'">{{ c.ok ? '✓' : '✗' }}</span>
          <b>{{ c.name }}</b>
          <span class="dim">{{ c.detail }}</span>
        </div>
      </div>
      <p v-if="testResult.hint" class="tr__hint">{{ testResult.hint }}</p>
    </div>
  </AppModal>

  <AdminConfirm
    v-model="delOpen"
    title="删除模型槽"
    :body="`确认删除「${pendingDel?.name || pendingDel?.model_id || ''}」？<br>
      <span class='dim'>该模型下的所有供应商端点与套餐路由引用会一并清理。</span>`"
    ok-text="确认删除"
    :loading="deleting"
    reason-placeholder="请填写删除原因"
    @confirm="doDelete"
  />
</template>

<style scoped>
.sp { flex: 1; }
.mt { margin-top: 12px; }
.dim { color: var(--text-faint); font-size: 11px; }
.danger { color: var(--danger, #d33); font-size: 11px; }
.ok { color: var(--success, #1a7f4b); }
.bad { color: var(--danger, #d33); }
.code { font-size: 11px; padding: 1px 5px; border-radius: 4px;
  border: 1px solid var(--border); word-break: break-all; }
.hint-inline { font-size: 11px; color: var(--text-faint); }

.tblwrap { overflow-x: auto; }
.tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
.tbl th { text-align: left; font-weight: 500; font-size: 11px; color: var(--text-faint);
  padding: 7px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }
.tbl td { padding: 8px; border-bottom: 1px solid var(--border); vertical-align: middle; }
.tbl tr:last-child td { border-bottom: none; }
.tbl .ops { white-space: nowrap; }
.op {
  font-size: 11px; padding: 3px 8px; margin-right: 4px;
  border: 1px solid var(--border); border-radius: 5px;
  background: var(--surface); color: var(--text-muted);
  cursor: pointer; font-family: inherit;
}
.op:hover:not(:disabled) { color: var(--text); border-color: var(--brand); }
.op:disabled { opacity: .55; cursor: default; }
.op.danger:hover { color: var(--danger, #d33);
  border-color: color-mix(in srgb, var(--danger, #d33) 50%, transparent); }

.permbar {
  font-size: 12px; line-height: 1.85; margin: 12px 0 0;
  padding: 10px 13px; border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--warn, #b8860b) 34%, transparent);
  background: color-mix(in srgb, var(--warn, #b8860b) 8%, transparent);
  color: var(--text-muted);
}
.permbar b { color: var(--warn, #b8860b); }
.permbar code { font-size: 11px; padding: 1px 5px; border-radius: 4px;
  border: 1px solid var(--border); background: var(--surface); }

/* ── 表单 ── */
.form { display: flex; flex-direction: column; gap: 13px; }
.form__grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.form__grid--3 { grid-template-columns: repeat(3, 1fr); }
.fld { display: flex; flex-direction: column; gap: 5px; }
.fld__l { font-size: 12px; color: var(--text-muted); }
.fld__h { font-size: 11px; color: var(--text-faint); line-height: 1.65; }
.req { color: var(--danger, #d33); font-size: 11px; font-style: normal; }
.form__err { margin: 0; font-size: 12px; color: var(--danger, #d33);
  padding: 8px 11px; border-radius: 8px;
  background: color-mix(in srgb, var(--danger, #d33) 8%, transparent); }
.form__note { margin: 0; font-size: 11px; color: var(--text-faint); line-height: 1.75; }
.sel { padding: 8px 11px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); color: var(--text); font-size: 13px;
  font-family: inherit; width: 100%; }
.sel:focus { outline: none; border-color: var(--brand); }
.check { display: flex; align-items: center; gap: 7px; font-size: 12px; cursor: pointer; }
.check input { accent-color: var(--brand); }

/* ── 供应商端点 ── */
.prov { padding: 11px 12px; border: 1px solid var(--border); border-radius: 9px; }
.prov__head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.prov__head b { font-size: 12px; }
.prov__d { margin: 7px 0 10px; font-size: 11px; color: var(--text-muted); line-height: 1.75; }
.prov__d b { color: var(--text); }
.prov__i { margin-bottom: 8px; }
.prov__g { display: grid; grid-template-columns: 1fr 1.6fr 1.3fr 80px auto; gap: 7px; align-items: center; }

/* ── 向导 ── */
.wz { display: flex; flex-direction: column; gap: 14px; }
.wz__steps { list-style: none; display: flex; gap: 8px; margin: 0; padding: 0; }
.wz__steps li { flex: 1; text-align: center; font-size: 11px; padding: 6px 4px;
  border-radius: 6px; background: var(--surface-2, var(--surface));
  color: var(--text-faint); border: 1px solid var(--border); }
.wz__steps li.on { background: var(--brand); color: #fff; border-color: var(--brand); }
.wz__steps li.done { color: var(--success, #1a7f4b); border-color: var(--success, #1a7f4b); }
.wz__b { display: flex; flex-direction: column; gap: 12px; }
.wz__d { margin: 0; font-size: 12px; color: var(--text-muted); line-height: 1.75; }
.wz__d b { color: var(--text); }
.wz__list { display: flex; flex-direction: column; gap: 7px; max-height: 260px; overflow: auto; }
.wz__t {
  display: flex; flex-direction: column; gap: 3px; align-items: flex-start;
  padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); cursor: pointer; font-family: inherit; text-align: left;
}
.wz__t.on { border-color: var(--brand);
  background: color-mix(in srgb, var(--brand) 6%, transparent); }
.wz__t b { font-size: 12px; color: var(--text); }
.wz__t code { font-size: 10px; color: var(--brand); }
.wz__t span { font-size: 11px; color: var(--text-faint); line-height: 1.6; }
.wz__sum { display: flex; flex-direction: column; gap: 6px; padding: 10px 12px;
  border-radius: 8px; background: var(--surface-2, var(--surface));
  border: 1px solid var(--border); }
.wz__sum > div { display: flex; gap: 10px; font-size: 12px; align-items: baseline; }
.wz__sum b { flex: none; width: 46px; color: var(--text-faint); font-weight: 500; font-size: 11px; }
.wz__r { padding: 10px 12px; border-radius: 8px; font-size: 12px; }
.wz__r.ok { border: 1px solid color-mix(in srgb, var(--success, #1a7f4b) 34%, transparent);
  background: color-mix(in srgb, var(--success, #1a7f4b) 8%, transparent); }
.wz__r.bad { border: 1px solid color-mix(in srgb, var(--danger, #d33) 34%, transparent);
  background: color-mix(in srgb, var(--danger, #d33) 8%, transparent); }
.wz__r p { margin: 5px 0 0; font-size: 11px; color: var(--text-muted); line-height: 1.7; }

/* ── 候选模型 ── */
.cands { display: flex; flex-wrap: wrap; gap: 7px; }
.cand {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 11px; border: 1px solid var(--border); border-radius: 7px;
  background: var(--surface); color: var(--text-muted); font-size: 12px;
  cursor: pointer; font-family: inherit;
}
.cand.on { border-color: var(--brand); color: var(--brand); font-weight: 600; }
.cand__i { display: inline-block; min-width: 14px; text-align: center;
  font-size: 10px; color: var(--brand); }

/* ── 测试结果 ── */
.tr { display: flex; flex-direction: column; gap: 10px; }
.tr__checks { display: flex; flex-direction: column; gap: 7px; }
.tr__c { display: flex; gap: 8px; align-items: baseline; font-size: 12px; }
.tr__c b { flex: none; }
.tr__hint { margin: 0; font-size: 12px; color: var(--text-muted); line-height: 1.8;
  padding: 10px 12px; border-radius: 8px;
  background: var(--surface-2, var(--surface)); border: 1px solid var(--border); }

@media (max-width: 680px) {
  .form__grid, .form__grid--3 { grid-template-columns: 1fr; }
  .prov__g { grid-template-columns: 1fr; }
}
</style>
