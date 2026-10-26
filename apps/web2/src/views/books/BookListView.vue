<script setup lang="ts">
/**
 * BookListView · 作品列表
 * 卡片/列表切换 + 搜索 + 排序 + 新建 + 删除
 *
 * ★ v1.0-E4.2：补上**删除作品**（此前只有后端接口，UI 从未接上）。
 *
 * ★★ 破坏性操作"两阶段"铁律（MEMORY §35）：先给用户看"会删什么"，再执行。
 *   本项目已有血泪教训 —— 直接删的交互会让误操作无法挽回：
 *   删书会**级联清除**章节/角色/伏笔/设定/大纲/质检报告/对话/任务，
 *   并删除 Git 仓库（历史版本一起没）。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import bookApi, { type Book, type CreateBookPayload } from '@/api/modules/book'
import { useUiStore } from '@/stores/ui'
import { ApiError } from '@/types/api'
import AppButton from '@/components/AppButton.vue'
import AppCard from '@/components/AppCard.vue'
import AppInput from '@/components/AppInput.vue'
import AppModal from '@/components/AppModal.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppTag from '@/components/AppTag.vue'

const router = useRouter()
const ui = useUiStore()

const loading = ref(true)
const books = ref<Book[]>([])
const keyword = ref('')
const sort = ref<'updated' | 'created' | 'title'>('updated')
const view = ref<'card' | 'list'>('card')

// 新建弹窗
const showCreate = ref(false)
const creating = ref(false)
const form = ref<CreateBookPayload>({ title: '', theme: '', author: '', premise: '' })
const formError = ref('')
/** ★ AppInput 的 modelValue 不接受 undefined（可选字段要收敛成 ''） */
const authorSafe = computed({
  get: () => form.value.author ?? '',
  set: (v: string) => { form.value.author = v },
})

/* ────────────── 删除作品（v1.0-E4.2 · 两阶段）──────────────
 *
 * ★ 为什么必须"两阶段"：删书**不可逆** ——
 *   级联清除章节/角色/伏笔/设定/大纲/质检/对话/任务 + 删 Git 仓库。
 *   用户点错一次，几个月的心血就没了。
 *
 * ★ 阶段① `askDelete(b)` —— 弹出确认框，**列出会删掉什么**（用真实数据）
 * ★ 阶段② `doDelete()` —— 用户输入书名二次确认后才真正执行
 *
 * ★ 为什么要求输入书名而不是"再点一次确定"：
 *   连续两次点击是**同一个肌肉记忆动作**，误操作防不住；
 *   手打书名会强制大脑参与，且能避免"删错那一本"。
 */
const delTarget = ref<Book | null>(null)
const delBusy = ref(false)
const delConfirmText = ref('')

/** 输入的书名是否与目标一致（前后空格容错） */
const delTextOk = computed(() =>
  !!delTarget.value &&
  delConfirmText.value.trim() === (delTarget.value.title || '').trim())

function askDelete(b: Book, ev?: Event) {
  ev?.stopPropagation()          // ★ 阻止冒泡到卡片的"打开作品"
  delTarget.value = b
  delConfirmText.value = ''
}

function cancelDelete() {
  delTarget.value = null
  delConfirmText.value = ''
}

async function doDelete() {
  const b = delTarget.value
  if (!b || !delTextOk.value) return
  delBusy.value = true
  try {
    await bookApi.remove(b.id)
    // ★ 本地移除（不整页重载，保留搜索/排序状态）
    books.value = books.value.filter((x) => x.id !== b.id)
    ui.ok(`已删除《${b.title}》`)
    cancelDelete()
  } catch (e) {
    // ★ 失败要说清原因：后端可能因权限/关联数据拒绝
    const msg = e instanceof ApiError
      ? (e.status === 403 ? '没有权限删除该作品' : e.message || '删除失败')
      : '网络异常，请稍后重试'
    ui.err(msg)
  } finally {
    delBusy.value = false
  }
}

/** 首批题材（与后端 genres 对齐） */
const THEMES = [
  { key: 'xianxia', label: '仙侠' },
  { key: 'urban', label: '都市' },
  { key: 'urban_suspense', label: '都市悬疑' },
  { key: 'scifi', label: '科幻' },
  { key: 'game', label: '游戏' },
]

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  let list = books.value
  if (kw) {
    list = list.filter(b =>
      (b.title || '').toLowerCase().includes(kw) ||
      (b.author || '').toLowerCase().includes(kw) ||
      (b.theme || '').toLowerCase().includes(kw))
  }
  const arr = [...list]
  if (sort.value === 'title') {
    arr.sort((a, b) => (a.title || '').localeCompare(b.title || '', 'zh-CN'))
  } else if (sort.value === 'created') {
    arr.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
  } else {
    arr.sort((a, b) => (b.updated_at || b.created_at || '').localeCompare(a.updated_at || a.created_at || ''))
  }
  return arr
})

onMounted(load)

async function load() {
  loading.value = true
  try {
    const r = await bookApi.list()
    // ★ 兼容：后端现为裸数组；万一将来改成 {items} 也不会白屏
    books.value = Array.isArray(r) ? r : ((r as { items?: Book[] }).items || [])
  } catch (e) {
    if (e instanceof ApiError) ui.err(e.message)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  form.value = { title: '', theme: '', author: '', premise: '' }
  formError.value = ''
  showCreate.value = true
}

async function submitCreate() {
  formError.value = ''
  if (!form.value.title.trim()) {
    formError.value = '请填写作品名'
    return
  }
  creating.value = true
  try {
    const r = await bookApi.create({
      title: form.value.title.trim(),
      theme: form.value.theme || undefined,
      author: form.value.author?.trim() || undefined,
      premise: form.value.premise?.trim() || undefined,
    })
    showCreate.value = false
    ui.ok('作品已创建')
    const id = r?.book?.id
    if (id) router.push({ name: 'studio', params: { bookId: id } })
    else load()
  } catch (e) {
    formError.value = e instanceof ApiError ? e.message : '创建失败，请稍后重试'
  } finally {
    creating.value = false
  }
}

function openBook(b: Book) {
  router.push({ name: 'studio', params: { bookId: b.id } })
}

function fmtDate(s?: string) { return s ? s.slice(0, 10) : '—' }

function themeLabel(key?: string) {
  return THEMES.find(t => t.key === key)?.label || key || ''
}
</script>

<template>
  <div class="page">
    <header class="page__head">
      <div>
        <h1 class="page__title">我的作品</h1>
        <p class="page__sub">共 {{ books.length }} 部</p>
      </div>
      <AppButton variant="primary" icon="＋" @click="openCreate">新建作品</AppButton>
    </header>

    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar__search">
        <AppInput
          v-model="keyword"
          placeholder="搜索作品名 / 作者 / 题材"
          size="sm"
          clearable
          aria-label="搜索作品"
        />
      </div>
      <select v-model="sort" class="toolbar__select" aria-label="排序方式">
        <option value="updated">最近更新</option>
        <option value="created">创建时间</option>
        <option value="title">名称</option>
      </select>
      <div class="toolbar__view" role="group" aria-label="视图切换">
        <button
          class="toolbar__vbtn" :class="{ 'is-on': view === 'card' }"
          type="button" aria-label="卡片视图" @click="view = 'card'"
        >▦</button>
        <button
          class="toolbar__vbtn" :class="{ 'is-on': view === 'list' }"
          type="button" aria-label="列表视图" @click="view = 'list'"
        >☰</button>
      </div>
    </div>

    <!-- 内容 -->
    <AppSkeleton v-if="loading" variant="card" :count="3" />

    <AppEmpty
      v-else-if="!filtered.length"
      :icon="keyword ? '🔍' : '📖'"
      :title="keyword ? '没有匹配的作品' : '还没有作品'"
      :description="keyword ? '换个关键词试试' : '点下面按钮，开始你的第一部小说'"
      :action-text="keyword ? '' : '＋ 新建作品'"
      @action="openCreate"
    />

    <!-- 卡片视图 -->
    <div v-else-if="view === 'card'" class="grid">
      <AppCard
        v-for="b in filtered"
        :key="b.id"
        clickable
        hoverable
        padding="md"
        @click="openBook(b)"
      >
        <div class="bcard">
          <!-- ★ 删除入口：只在悬停/聚焦时显现，避免干扰主要操作（打开作品） -->
          <button
            class="bcard__del"
            type="button"
            title="删除这本作品"
            aria-label="删除作品"
            @click="askDelete(b, $event)"
          >✕</button>
          <h3 class="bcard__title truncate">{{ b.title }}</h3>
          <p class="bcard__meta">
            {{ b.author || '未署名' }}
            <AppTag v-if="b.theme" size="sm">{{ themeLabel(b.theme) }}</AppTag>
          </p>
          <div class="bcard__stats">
            <span>{{ b.chapters || 0 }} 章</span>
            <span>{{ (b.words || 0).toLocaleString('zh-CN') }} 字</span>
            <span v-if="b.quality_score">质检 {{ b.quality_score }}</span>
          </div>
          <p class="bcard__date subtle tiny">更新于 {{ fmtDate(b.updated_at || b.created_at) }}</p>
        </div>
      </AppCard>
    </div>

    <!-- 列表视图 -->
    <AppCard v-else padding="none">
      <div class="ltable">
        <div class="ltable__head">
          <span>作品</span><span>题材</span>
          <span class="r">章数</span><span class="r">字数</span><span>更新时间</span>
          <span class="r">操作</span>
        </div>
        <div
          v-for="b in filtered"
          :key="b.id"
          class="ltable__row"
          tabindex="0" role="button"
          @click="openBook(b)"
          @keydown.enter="openBook(b)"
        >
          <span class="truncate"><strong>{{ b.title }}</strong></span>
          <span class="muted">{{ themeLabel(b.theme) || '—' }}</span>
          <span class="r mono">{{ b.chapters || 0 }}</span>
          <span class="r mono">{{ (b.words || 0).toLocaleString('zh-CN') }}</span>
          <span class="muted tiny">{{ fmtDate(b.updated_at || b.created_at) }}</span>
          <span class="r">
            <button
              class="ltable__del"
              type="button"
              title="删除这本作品"
              aria-label="删除作品"
              @click="askDelete(b, $event)"
            >删除</button>
          </span>
        </div>
      </div>
    </AppCard>

    <!-- ★★ 删除确认弹窗（两阶段：先看"会删什么"，再输入书名二次确认） -->
    <AppModal
      :model-value="!!delTarget"
      title="删除作品"
      :loading="delBusy"
      danger
      ok-text="确认删除"
      cancel-text="取消"
      :ok-disabled="!delTextOk"
      @update:model-value="cancelDelete"
      @ok="doDelete"
    >
      <div v-if="delTarget" class="del">
        <p class="del__warn">
          ⚠️ <strong>此操作不可撤销</strong>，将永久删除
          《<strong>{{ delTarget.title }}</strong>》
        </p>

        <!-- ★ 用真实数据列出"会删掉什么" —— 让用户看清代价 -->
        <div class="del__scope">
          <p class="del__scope-title">将一并删除：</p>
          <ul class="del__list">
            <li v-if="delTarget.chapters">
              <b>{{ delTarget.chapters }}</b> 个章节（约
              {{ (delTarget.words || 0).toLocaleString('zh-CN') }} 字正文）
            </li>
            <li>全部角色卡、世界观设定、伏笔与关系图谱</li>
            <li>三级大纲、质检报告与一致性记录</li>
            <li>创作对话历史与任务记录</li>
            <li>Git 版本历史（<strong>所有历史版本一起消失</strong>）</li>
          </ul>
        </div>

        <p class="del__tip">
          💡 如果只是想暂时收起，可以改用「导出备份」保留数据。
        </p>

        <AppInput
          v-model="delConfirmText"
          :label="`请输入作品名「${delTarget.title}」以确认`"
          :placeholder="delTarget.title"
        />
      </div>
    </AppModal>

    <!-- 新建弹窗 -->
    <AppModal
      v-model="showCreate"
      title="新建作品"
      :loading="creating"
      ok-text="创建并开始"
      @ok="submitCreate"
    >
      <div class="form">
        <AppInput
          v-model="form.title"
          label="作品名"
          placeholder="给你的作品起个名字"
          :maxlength="64"
          show-count
        />
        <div class="form__field">
          <label class="form__label">题材（决定实力体系与文风）</label>
          <div class="chips">
            <button
              v-for="t in THEMES"
              :key="t.key"
              class="chip"
              :class="{ 'is-on': form.theme === t.key }"
              type="button"
              @click="form.theme = form.theme === t.key ? '' : t.key"
            >{{ t.label }}</button>
          </div>
        </div>
        <AppInput v-model="authorSafe" label="作者名（可选）" placeholder="笔名" :maxlength="32" />
        <div class="form__field">
          <label class="form__label">一句话简介（可选）</label>
          <textarea
            v-model="form.premise"
            class="form__textarea"
            rows="3"
            placeholder="例如：一个落魄书生，捡到半卷残经，从此走上逆天之路"
          />
        </div>
        <p v-if="formError" class="form__error" role="alert">{{ formError }}</p>
      </div>
    </AppModal>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 1200px; margin: 0 auto; }
.page__head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--sp-4); margin-bottom: var(--sp-5);
}
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); }

.toolbar { display: flex; align-items: center; gap: var(--sp-3); margin-bottom: var(--sp-4); }
.toolbar__search { flex: 1 1 auto; max-width: 320px; }
.toolbar__select {
  height: 30px; padding: 0 var(--sp-2);
  border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--bg-elevated); color: var(--text); font-size: var(--fs-xs);
}
.toolbar__view { display: flex; gap: 2px; border: 1px solid var(--border); border-radius: var(--r-md); overflow: hidden; }
.toolbar__vbtn {
  width: 30px; height: 30px; border: none; background: transparent;
  color: var(--text-subtle); font-size: 13px;
}
.toolbar__vbtn.is-on { background: var(--brand-50); color: var(--brand-600); }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: var(--sp-4);
}
.bcard { display: flex; flex-direction: column; gap: var(--sp-2); position: relative; }
.bcard__title { font-size: var(--fs-sm); }

/* ★ 删除按钮：默认淡出，悬停卡片才显现 —— 避免与"打开作品"抢注意力 */
.bcard__del {
  position: absolute; top: -4px; right: -4px;
  width: 22px; height: 22px; line-height: 1;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elevated); color: var(--text-subtle);
  font-size: 12px; cursor: pointer;
  opacity: 0; transition: opacity var(--dur-fast) var(--ease),
                          color var(--dur-fast) var(--ease),
                          border-color var(--dur-fast) var(--ease);
}
/* ★ 键盘用户也要能用：focus-within 让 Tab 聚焦时同样显现 */
.bcard:hover .bcard__del,
.bcard:focus-within .bcard__del { opacity: 1; }
.bcard__del:hover {
  color: var(--danger, #d33); border-color: var(--danger, #d33);
}
.bcard__del:focus-visible { opacity: 1; outline: 2px solid var(--brand-500); }

.bcard__meta {
  display: flex; align-items: center; gap: var(--sp-2);
  font-size: var(--fs-xs); color: var(--text-muted);
}
.bcard__stats {
  display: flex; gap: var(--sp-3);
  font-size: var(--fs-xs); color: var(--text-subtle);
  font-variant-numeric: tabular-nums;
}
.bcard__date { margin-top: auto; }

.ltable { display: flex; flex-direction: column; }
.ltable__head, .ltable__row {
  display: grid;
  grid-template-columns: 2.2fr 1fr .6fr .8fr 1fr .6fr;
  gap: var(--sp-3); align-items: center;
  padding: var(--sp-3) var(--sp-4);
}
.ltable__head {
  font-size: var(--fs-xs); color: var(--text-muted);
  border-bottom: 1px solid var(--border);
  background: var(--bg-sunken);
}
.ltable__row {
  border-bottom: 1px solid var(--border);
  font-size: var(--fs-sm); cursor: pointer;
}
.ltable__row:last-child { border-bottom: none; }
.ltable__row:hover { background: var(--bg-hover); }
.ltable__row:focus-visible { outline: 2px solid var(--brand-500); outline-offset: -2px; }
.r { text-align: right; }

/* 行内删除按钮：低干扰（文字按钮，悬停才变色） */
.ltable__del {
  border: 0; background: none; padding: 2px 4px;
  color: var(--text-subtle); font-size: var(--fs-xs);
  cursor: pointer; border-radius: var(--r-sm);
}
.ltable__del:hover { color: var(--danger, #d33); background: var(--bg-sunken); }
.ltable__del:focus-visible { outline: 2px solid var(--brand-500); }

/* 删除确认弹窗 */
.del { display: flex; flex-direction: column; gap: var(--sp-4); }
.del__warn {
  padding: var(--sp-3);
  border: 1px solid var(--danger, #d33);
  border-radius: var(--r-md);
  background: color-mix(in srgb, var(--danger, #d33) 8%, transparent);
  font-size: var(--fs-sm); line-height: 1.6;
}
.del__scope { font-size: var(--fs-xs); color: var(--text-muted); }
.del__scope-title { margin-bottom: var(--sp-2); font-weight: 600; color: var(--text); }
.del__list { margin: 0; padding-left: 1.2em; line-height: 1.9; }
.del__tip {
  font-size: var(--fs-xs); color: var(--text-subtle);
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-sunken); border-radius: var(--r-md);
}

/* 表单 */
.form { display: flex; flex-direction: column; gap: var(--sp-4); }
.form__field { display: flex; flex-direction: column; gap: var(--sp-2); }
.form__label { font-size: var(--fs-xs); font-weight: var(--fw-medium); color: var(--text-muted); }
.form__textarea {
  width: 100%; padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--bg-elevated); color: var(--text);
  font-size: var(--fs-sm); font-family: inherit; resize: vertical;
}
.form__textarea:focus { outline: none; border-color: var(--brand-500); box-shadow: var(--focus-ring); }
.form__error {
  padding: var(--sp-2) var(--sp-3); border-radius: var(--r-md);
  background: var(--danger-bg); border: 1px solid var(--danger);
  color: var(--danger); font-size: var(--fs-xs);
}

.chips { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.chip {
  height: 30px; padding: 0 var(--sp-3);
  border: 1px solid var(--border); border-radius: var(--r-full);
  background: var(--bg-elevated); color: var(--text-muted);
  font-size: var(--fs-xs);
  transition: all var(--dur-fast) var(--ease);
}
.chip:hover { border-color: var(--brand-200); }
.chip.is-on { background: var(--brand-500); border-color: var(--brand-500); color: #fff; }

@media (max-width: 720px) {
  .ltable__head, .ltable__row { grid-template-columns: 2fr 1fr .8fr; }
  .ltable__head span:nth-child(4), .ltable__row span:nth-child(4),
  .ltable__head span:nth-child(5), .ltable__row span:nth-child(5) { display: none; }
}
</style>
