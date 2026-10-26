<script setup lang="ts">
/**
 * CharacterListView · 人物管理
 *
 * ★ 与旧版对比验收：
 *   [x] 角色卡片（角色定位 / 出场章节 / 提及次数 / 当前状态）
 *   [x] 编辑（角色 / 小传 / 声线 / 弧光 / 当前状态）
 *   [x] 从正文同步识别
 *   [x] 自动识别标记（is_auto）
 * ★ 新增：按提及次数排序、搜索、状态高亮
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import settingApi, { type Character } from '@/api/modules/setting'
import { useUiStore } from '@/stores/ui'
import { ApiError } from '@/types/api'
import AppButton from '@/components/AppButton.vue'
import AppCard from '@/components/AppCard.vue'
import AppInput from '@/components/AppInput.vue'
import AppTag from '@/components/AppTag.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppModal from '@/components/AppModal.vue'

const route = useRoute()
const router = useRouter()
const ui = useUiStore()

const bookId = computed(() => String(route.params.bookId || ''))
const items = ref<Character[]>([])
const loading = ref(true)
const syncing = ref(false)
const keyword = ref('')

// 编辑弹窗
const editing = ref<Character | null>(null)
/** ★ AppModal 的 modelValue 是 boolean；这里用计算属性适配（关闭时清空） */
const editOpen = computed({
  get: () => editing.value !== null,
  set: (v: boolean) => { if (!v) editing.value = null },
})
const saving = ref(false)
const form = ref({ role: '', profile: '', voice: '', arc: '', state_now: '' })

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return items.value
  return items.value.filter(c =>
    c.name.toLowerCase().includes(kw) ||
    (c.role || '').toLowerCase().includes(kw) ||
    (c.profile || '').toLowerCase().includes(kw))
})

const stats = computed(() => ({
  total: items.value.length,
  auto: items.value.filter(c => c.is_auto).length,
  named: items.value.filter(c => c.role).length,
}))

onMounted(load)

async function load() {
  loading.value = true
  try {
    const r = await settingApi.characters(bookId.value)
    items.value = r.items || []
  } catch (e) {
    if (e instanceof ApiError) ui.err(e.message)
  } finally {
    loading.value = false
  }
}

async function sync() {
  syncing.value = true
  try {
    const r = await settingApi.syncCharacters(bookId.value)
    ui.ok(`同步完成：新增 ${r.added ?? 0} 个，更新 ${r.updated ?? 0} 个`)
    await load()
  } catch (e) {
    if (e instanceof ApiError) ui.err(e.message)
  } finally {
    syncing.value = false
  }
}

function openEdit(c: Character) {
  editing.value = c
  form.value = {
    role: c.role || '', profile: c.profile || '',
    voice: c.voice || '', arc: c.arc || '', state_now: c.state_now || '',
  }
}

async function saveEdit() {
  if (!editing.value) return
  saving.value = true
  try {
    await settingApi.patchCharacter(bookId.value, editing.value.id, form.value)
    Object.assign(editing.value, form.value)
    ui.ok('已保存')
    editing.value = null
  } catch (e) {
    if (e instanceof ApiError) ui.err(e.message)
  } finally {
    saving.value = false
  }
}

// ★ #311 删除人物（带二次确认）
const delOpen = ref(false)
const deleting = ref(false)
const pendingDel = ref<Character | null>(null)

function askDelete(c: Character) {
  pendingDel.value = c
  delOpen.value = true
}
async function confirmDelete() {
  if (!pendingDel.value) return
  deleting.value = true
  const name = pendingDel.value.name
  try {
    await settingApi.deleteCharacter(bookId.value, pendingDel.value.id)
    items.value = items.value.filter(x => x.id !== pendingDel.value!.id)
    ui.ok(`已删除《${name}》`)
    delOpen.value = false
    pendingDel.value = null
  } catch (e) {
    if (e instanceof ApiError) ui.err(e.message)
  } finally {
    deleting.value = false
  }
}

function roleKind(role: string) {
  if (!role) return 'default' as const
  if (/主角|protagonist/.test(role)) return 'brand' as const
  if (/反派|antagonist/.test(role)) return 'danger' as const
  if (/配角|supporting/.test(role)) return 'info' as const
  return 'default' as const
}
</script>

<template>
  <div class="page">
    <header class="page__head">
      <div class="page__title-row">
        <div>
          <RouterLink class="page__back" :to="{ name: 'studio', params: { bookId } }">← 写作台</RouterLink>
          <h1 class="page__title">人物 · {{ stats.total }} 位</h1>
          <p class="page__sub">
            自动识别 {{ stats.auto }} 位 · 已定位角色 {{ stats.named }} 位
          </p>
        </div>
        <AppButton variant="primary" size="sm" :loading="syncing" @click="sync">
          从正文同步
        </AppButton>
      </div>
    </header>

    <div class="toolbar">
      <div class="toolbar__search">
        <AppInput v-model="keyword" size="sm" placeholder="搜索姓名 / 定位 / 小传"
                  clearable aria-label="搜索人物" />
      </div>
      <span class="muted tiny">按提及次数排序</span>
    </div>

    <AppSkeleton v-if="loading" variant="card" :count="4" />

    <AppEmpty
      v-else-if="!filtered.length"
      :icon="keyword ? '🔍' : '☺'"
      :title="keyword ? '没有匹配的人物' : '还没有人物'"
      :description="keyword ? '换个关键词试试' : '在写作台对话中提到的人物会自动建档；也可以点「从正文同步」立即识别'"
      :action-text="keyword ? '' : '从正文同步'"
      @action="sync"
    />

    <div v-else class="grid">
      <AppCard v-for="c in filtered" :key="c.id" hoverable padding="md" clickable @click="openEdit(c)">
        <div class="ccard">
          <div class="ccard__head">
            <span class="ccard__avatar" aria-hidden="true">{{ c.name.slice(0, 1) }}</span>
            <div class="ccard__names">
              <strong class="truncate">{{ c.name }}</strong>
              <span class="muted tiny">
                <template v-if="c.first_chapter">首次出场 第{{ c.first_chapter }}章</template>
                <template v-else>未出场</template>
                · 提及 {{ c.mentions }}
              </span>
            </div>
            <AppTag v-if="c.role" size="sm" :kind="roleKind(c.role)">{{ c.role }}</AppTag>
            <AppTag v-else-if="c.is_auto" size="sm" kind="warning">待定位</AppTag>
            <button
              class="ccard__del"
              type="button"
              title="删除人物"
              aria-label="删除人物"
              @click.stop="askDelete(c)"
            >✕</button>
          </div>

          <p v-if="c.state_now" class="ccard__state">{{ c.state_now }}</p>
          <p v-if="c.profile" class="ccard__profile">{{ c.profile }}</p>
          <p v-else class="muted tiny">暂无小传，点击补充</p>

          <div v-if="c.voice || c.arc" class="ccard__tags">
            <AppTag v-if="c.voice" size="sm">声线已配</AppTag>
            <AppTag v-if="c.arc" size="sm">弧光已定</AppTag>
          </div>
        </div>
      </AppCard>
    </div>

    <!-- 编辑弹窗 -->
    <AppModal
      v-model="editOpen"
      :title="`编辑人物 · ${editing?.name || ''}`"
      width="520px"
      :loading="saving"
      @ok="saveEdit"
    >
      <div class="form">
        <AppInput v-model="form.role" label="角色定位" placeholder="主角 / 反派 / 配角…" />
        <AppInput v-model="form.state_now" label="当前状态" placeholder="如：重伤未愈、已突破金丹期" />
        <AppInput v-model="form.voice" label="声线设定" placeholder="如：低沉沙哑、清朗少年音" />
        <div class="form__field">
          <label class="form__label">人物小传</label>
          <textarea v-model="form.profile" class="form__ta" rows="4"
                    placeholder="出身、性格、动机、与主角的关系…" />
        </div>
        <div class="form__field">
          <label class="form__label">成长弧光</label>
          <textarea v-model="form.arc" class="form__ta" rows="3"
                    placeholder="这个角色从哪来、要到哪去" />
        </div>
      </div>
    </AppModal>

    <!-- ★ #311 删除确认弹窗 -->
    <AppModal
      v-model="delOpen"
      title="删除人物"
      width="440px"
      :loading="deleting"
      ok-text="确认删除"
      danger
      @ok="confirmDelete"
    >
      <p class="del-warn">
        确认从人物清单删除《<strong>{{ pendingDel?.name }}</strong>》吗？
      </p>
      <p class="muted tiny">
        删除后该人物的小传、声线、关系等知识会一并清除，且不可恢复。
        若它是自动识别误入的泛词（如「叶子/东西/宿主」），删掉即可。
      </p>
    </AppModal>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 1100px; margin: 0 auto; }
.page__head { margin-bottom: var(--sp-4); }
.page__title-row { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--sp-4); }
.page__back { font-size: var(--fs-xs); color: var(--text-muted); display: block; margin-bottom: 4px; }
.page__back:hover { color: var(--brand-500); }
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); }

.toolbar { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3); margin-bottom: var(--sp-4); }
.toolbar__search { flex: 1 1 auto; max-width: 300px; }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: var(--sp-4); }

.ccard { display: flex; flex-direction: column; gap: var(--sp-2); }
.ccard__head { display: flex; align-items: flex-start; gap: var(--sp-3); }
.ccard__avatar {
  width: 34px; height: 34px; flex-shrink: 0;
  display: grid; place-items: center;
  border-radius: 50%; background: var(--brand-100); color: var(--brand-600);
  font-size: var(--fs-sm); font-weight: 700;
}
.ccard__names { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; }
.ccard__names strong { font-size: var(--fs-sm); }
.ccard__state {
  padding: 4px var(--sp-2); border-radius: var(--r-sm);
  background: var(--warning-bg); color: #8a6510;
  font-size: var(--fs-xs);
}
[data-theme="dark"] .ccard__state { color: #e8b64a; }
.ccard__profile {
  font-size: var(--fs-xs); color: var(--text-muted); line-height: var(--lh-normal);
  display: -webkit-box; -webkit-line-clamp: 4; line-clamp: 4;
  -webkit-box-orient: vertical; overflow: hidden;
}
.ccard__tags { display: flex; gap: var(--sp-2); flex-wrap: wrap; }

.ccard__del {
  width: 22px; height: 22px; flex-shrink: 0;
  align-self: flex-start;
  border: 1px solid transparent; border-radius: var(--r-sm);
  background: transparent; color: var(--text-subtle);
  font-size: 12px; line-height: 1;
  opacity: 0; transition: opacity var(--dur-fast) var(--ease),
    background-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.ccard:hover .ccard__del { opacity: 1; }
.ccard__del:hover { background: var(--danger-bg); color: var(--danger); border-color: var(--danger); }

.del-warn { font-size: var(--fs-sm); margin: 0 0 var(--sp-2); }
.del-warn strong { color: var(--text); }

.form { display: flex; flex-direction: column; gap: var(--sp-4); }
.form__field { display: flex; flex-direction: column; gap: var(--sp-2); }
.form__label { font-size: var(--fs-xs); font-weight: var(--fw-medium); color: var(--text-muted); }
.form__ta {
  width: 100%; padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--bg-elevated); color: var(--text);
  font-family: inherit; font-size: var(--fs-sm); resize: vertical;
}
.form__ta:focus { outline: none; border-color: var(--brand-500); box-shadow: var(--focus-ring); }
</style>
