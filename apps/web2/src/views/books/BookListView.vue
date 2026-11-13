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
import bookApi, {
  type Book, type CreateBookPayload, type PackInspectResult,
} from '@/api/modules/book'
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

/* ══════════════════════ ★ v1.2.0 封面生成 ══════════════════════
 * 后端 AI 无字底图（系统配置 image 模型）+ 服务端 PIL 真字体排字，
 * 彻底规避扩散模型乱码。卡片悬停「封面」按钮 → 生成 → 弹窗预览。
 * 画幅默认 2:3（书封），弹窗里可切 9:16 / 换风格再生成。
 */
const coverBusyId = ref('')
const showCover = ref(false)
const coverPreview = ref('')
const coverTitle = ref('')
const coverBaseOk = ref(false)   // ai_base=false 说明底图失败，已降级纯渐变
const coverPortrait = ref<'2:3' | '9:16'>('2:3')
const coverStyle = ref<'xianxia' | 'modern'>('xianxia')
/** ★ v1.2.0 弹窗里的画幅/风格选项（带字面量类型，模板 v-for 直接引用，避免 string[] 推断） */
const PORTRAIT_OPTS: { v: '2:3' | '9:16'; label: string }[] = [
  { v: '2:3', label: '小说封面 2:3' },
  { v: '9:16', label: '竖屏 9:16' },
]
const STYLE_OPTS: { v: 'xianxia' | 'modern'; label: string }[] = [
  { v: 'xianxia', label: '古风竖排' },
  { v: 'modern', label: '现代横排' },
]

async function generateCover(b: Book, ev?: Event) {
  ev?.stopPropagation()
  coverBusyId.value = b.id
  try {
    const r = await bookApi.generateCover(b.id, {
      style: coverStyle.value, portrait: coverPortrait.value,
    })
    coverPreview.value = r.url
    coverTitle.value = b.title
    coverBaseOk.value = r.ai_base
    showCover.value = true
    ui.ok(r.ai_base ? '封面已生成' : '底图模型不可用，已用渐变底生成')
  } catch (e) {
    ui.err((e instanceof ApiError ? e.message : '封面生成失败') || '封面生成失败')
  } finally {
    coverBusyId.value = ''
  }
}

/** ★ v1.2.0 弹窗里换画幅/风格后「重新生成」当前书 */
async function regenerateCover() {
  if (!coverTitle.value) return
  const b = books.value.find(x => (x.title || '').trim() === (coverTitle.value || '').trim())
  if (!b) return
  coverBusyId.value = b.id
  try {
    const r = await bookApi.generateCover(b.id, {
      style: coverStyle.value, portrait: coverPortrait.value,
    })
    coverPreview.value = r.url
    coverBaseOk.value = r.ai_base
  } catch (e) {
    ui.err((e instanceof ApiError ? e.message : '重新生成失败') || '重新生成失败')
  } finally {
    coverBusyId.value = ''
  }
}

/** 下载封面 */
function downloadCover() {
  if (!coverPreview.value) return
  const a = document.createElement('a')
  a.href = coverPreview.value
  a.download = `${(coverTitle.value || 'cover').slice(0, 40)}.png`
  a.target = '_blank'
  a.rel = 'noopener'
  a.click()
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

/* ══════════════════════ F2 整书打包 备份 / 恢复 ══════════════════════
 *
 * 后端 .nfbook（ZIP 机器可还原快照：设定/大纲/人物/伏笔/纪年表/账本/
 *   短剧工程/原始文件）三端点早已就绪（/pack 导出 · /import-pack 导入 ·
 *   /inspect-pack 预检），但前端**从未接上**——删除弹窗里那句「改用导出
 *   备份保留数据」根本点不到东西。这里把两条入口补齐：
 *
 *   · 每本作品卡片/行上「备份」→ 下载该书 .nfbook
 *   · 页级「导入备份」→ 选 .nfbook 文件：先只读预检（/inspect-pack，
 *     校验魔数/格式版本/结构，不落库），通过后再选 复制/覆盖 执行导入
 *
 * ★ 导入是「可逆/幂等」操作（copy 换新 id 绝不覆盖现有书），但 merge
 *   沿用原 id 会覆盖同名书，故 merge 需二次确认（复用删除弹窗同款
 *   两阶段思路的精神：破坏性选项要让用户看清代价）。
 */
const packingId = ref('')

/** 下载某本书的 .nfbook 备份（浏览器保存，复用 createObjectURL 模式） */
async function backupBook(b: Book, ev?: Event) {
  ev?.stopPropagation()          // ★ 卡片可点「打开作品」，备份按钮不能触发它
  packingId.value = b.id
  try {
    const blob = await bookApi.packBook(b.id)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${(b.title || 'book').slice(0, 40)}.${Date.now()}.nfbook`
    a.click()
    URL.revokeObjectURL(url)
    ui.ok(`《${b.title}》备份已开始下载`)
  } catch (e) {
    ui.err((e instanceof ApiError ? e.message : '备份失败') || '备份失败')
  } finally {
    packingId.value = ''
  }
}

/* ── 导入备份（页级）：选文件 → 预检 → copy/merge → 执行 ── */
const showImport = ref(false)
const importBusy = ref(false)
const importMode = ref<'copy' | 'merge'>('copy')
/** 已选文件 + 预检结果（未预检通过前不允许执行导入） */
const importFile = ref<File | null>(null)
const importInspect = ref<PackInspectResult | null>(null)
const importInspecting = ref(false)
const importInspectOk = computed(() =>
  !!importInspect.value?.ok && importInspect.value.compatible !== false)
/** ★ merge 覆盖现有书 → 需输入要覆盖的书名二次确认（copy 则无需） */
const importConfirmText = ref('')
const mergeTextOk = computed(() =>
  importMode.value === 'copy' || importConfirmText.value.trim().length > 0)

function openImport() {
  importFile.value = null
  importInspect.value = null
  importMode.value = 'copy'
  importConfirmText.value = ''
  showImport.value = true
}

function onImportFileChange(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0] || null
  importFile.value = f
  importInspect.value = null            // 换了文件必须重新预检
  importConfirmText.value = ''
}

/** 预检（只读，不落库）：校验魔数/格式版本/结构，给出导入前信息 */
async function inspectImportFile() {
  if (!importFile.value) return
  importInspecting.value = true
  try {
    importInspect.value = await bookApi.inspectPack(importFile.value)
    if (!importInspect.value.ok) {
      ui.err(importInspect.value.error || '备份包无效，无法导入')
    }
  } catch (e) {
    importInspect.value = null
    ui.err((e instanceof ApiError ? e.message : '预检失败') || '预检失败')
  } finally {
    importInspecting.value = false
  }
}

async function doImport() {
  if (!importFile.value || !importInspectOk.value || !mergeTextOk.value) return
  importBusy.value = true
  try {
    const r = await bookApi.importPack(importFile.value, importMode.value)
    ui.ok(`导入成功：《${r.title}》（${r.mode === 'merge' ? '覆盖' : '复制'}）`)
    showImport.value = false
    await load()                          // 刷新列表，呈现新导入的书
  } catch (e) {
    ui.err((e instanceof ApiError ? e.message : '导入失败') || '导入失败')
  } finally {
    importBusy.value = false
  }
}
</script>

<template>
  <div class="page">
    <header class="page__head">
      <div>
        <h1 class="page__title">我的作品</h1>
        <p class="page__sub">共 {{ books.length }} 部</p>
      </div>
      <div class="page__acts">
        <AppButton variant="ghost" icon="⬆" @click="openImport">导入备份</AppButton>
        <AppButton variant="primary" icon="＋" @click="openCreate">新建作品</AppButton>
      </div>
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
          <!-- ★ F2 备份入口：下载该书 .nfbook 完整快照（换服务器/时间点回退） -->
          <button
            class="bcard__bak"
            type="button"
            :disabled="packingId === b.id"
            :title="packingId === b.id ? '正在备份…' : '备份这本作品（.nfbook 完整快照）'"
            aria-label="备份作品"
            @click="backupBook(b, $event)"
          >{{ packingId === b.id ? '…' : '备份' }}</button>
          <!-- ★ v1.2.0 封面生成入口：AI 无字底图 + 服务端排字（规避乱码） -->
          <button
            class="bcard__cover"
            type="button"
            :disabled="coverBusyId === b.id"
            :title="coverBusyId === b.id ? '正在生成封面…' : '生成封面（AI 底图 + 书名/作者排版）'"
            aria-label="生成封面"
            @click="generateCover(b, $event)"
          >{{ coverBusyId === b.id ? '…' : '封面' }}</button>
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
          <span class="r ltable__acts">
            <button
              class="ltable__bak"
              type="button"
              :disabled="packingId === b.id"
              :title="packingId === b.id ? '正在备份…' : '备份这本作品（.nfbook 完整快照）'"
              aria-label="备份作品"
              @click="backupBook(b, $event)"
            >{{ packingId === b.id ? '…' : '备份' }}</button>
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

    <!-- ★ v1.2.0 封面预览弹窗：生成后预览 + 切画幅/风格重生成 + 下载 -->
    <AppModal
      v-model="showCover"
      title="封面预览"
      :hide-footer="true"
      width="520px"
      @cancel="showCover = false"
    >
      <div class="cov">
        <p class="cov__title">{{ coverTitle }}</p>
        <div class="cov__stage">
          <img v-if="coverPreview" :src="coverPreview" :alt="coverTitle" class="cov__img" />
          <p v-else class="muted tiny">生成中…</p>
        </div>
        <p v-if="!coverBaseOk" class="cov__note">
          ℹ️ 当前底图模型不可用，已用渐变底生成（到配置中心添加 image 角色模型后可出真图）。
        </p>
        <div class="cov__opts">
          <div class="cov__row">
            <label class="cov__lbl">画幅</label>
            <button
              v-for="opt in PORTRAIT_OPTS"
              :key="opt.v"
              class="chip" :class="{ 'is-on': coverPortrait === opt.v }"
              type="button"
              @click="coverPortrait = opt.v"
            >{{ opt.label }}</button>
          </div>
          <div class="cov__row">
            <label class="cov__lbl">风格</label>
            <button
              v-for="opt in STYLE_OPTS"
              :key="opt.v"
              class="chip" :class="{ 'is-on': coverStyle === opt.v }"
              type="button"
              @click="coverStyle = opt.v"
            >{{ opt.label }}</button>
          </div>
          <div class="cov__row">
            <button class="cov__regen" type="button" :disabled="!!coverBusyId" @click="regenerateCover">
              {{ coverBusyId ? '生成中…' : '按当前设置重新生成' }}</button>
            <button class="cov__dl" type="button" :disabled="!coverPreview" @click="downloadCover">⬇ 下载</button>
          </div>
        </div>
      </div>
    </AppModal>

    <!-- ★ F2 导入备份弹窗：选文件 → 预检（只读）→ 选 复制/覆盖 → 执行 -->
    <AppModal
      v-model="showImport"
      title="导入作品备份（.nfbook）"
      :hide-footer="true"
      width="520px"
      @cancel="showImport = false"
    >
      <div class="imp">
        <p class="imp__lead">
          上传一个 <code>.nfbook</code> 完整备份，把某本书连同设定 / 大纲 /
          人物 / 伏笔 / 纪年表 / 账本 / 短剧工程一起搬过来（换服务器或恢复时间点用）。
        </p>

        <!-- ① 选文件 + 预检 -->
        <label class="imp__pick">
          <input type="file" accept=".nfbook,.zip" @change="onImportFileChange" />
          <span class="imp__picktxt">{{ importFile?.name || '选择 .nfbook 文件…' }}</span>
        </label>

        <button
          class="imp__inspect"
          type="button"
          :disabled="!importFile || importInspecting"
          @click="inspectImportFile"
        >{{ importInspecting ? '正在预检…' : '① 预检（校验包格式）' }}</button>

        <!-- 预检结果 -->
        <div v-if="importInspect && importInspectOk" class="imp__ok">
          ✔ 预检通过：《{{ importInspect.manifest?.book_title || importInspect.manifest?.book_id || '未知' }}》
          <span class="muted tiny">
            格式 v{{ importInspect.manifest?.format_version }} · {{ importInspect.entries || 0 }} 项
            <template v-if="importInspect.manifest?.exported_at"> · {{ importInspect.manifest.exported_at }}</template>
          </span>
        </div>
        <div v-else-if="importInspect && !importInspectOk" class="imp__err">
          ✖ {{ importInspect.error || '包无效或格式不受支持' }}
        </div>

        <!-- ② 选导入方式（仅预检通过后可选） -->
        <div v-if="importInspectOk" class="imp__mode">
          <label class="imp__radio">
            <input v-model="importMode" type="radio" value="copy" />
            <div>
              <b>复制为新作品</b>
              <span class="muted tiny">生成新 id，<strong>不覆盖</strong>任何现有作品（最安全，默认）</span>
            </div>
          </label>
          <label class="imp__radio">
            <input v-model="importMode" type="radio" value="merge" />
            <div>
              <b>覆盖原 id</b>
              <span class="muted tiny">沿用原作品 id，<strong>会覆盖同名书的既有数据</strong>（恢复用）</span>
            </div>
          </label>
          <!-- ★ merge 需二次确认（不可逆覆盖）；copy 无需 -->
          <AppInput
            v-if="importMode === 'merge'"
            v-model="importConfirmText"
            label="覆盖属不可逆操作，输入 YES 以确认"
            placeholder="YES"
          />
        </div>

        <!-- ③ 执行 -->
        <div class="imp__foot">
          <button
            class="imp__import"
            type="button"
            :disabled="!importInspectOk || !mergeTextOk || importBusy"
            @click="doImport"
          >{{ importBusy ? '导入中…' : '② 执行导入' }}</button>
        </div>
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
.page__acts { display: flex; align-items: center; gap: var(--sp-2); }

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

/* ★ F2 卡片「备份」角标：与删除 ✕ 并排，同样悬停/聚焦才显现（低干扰） */
.bcard__bak {
  position: absolute; top: 4px; right: 26px;
  height: 22px; padding: 0 8px; line-height: 20px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elevated); color: var(--text-muted);
  font-size: 11px; cursor: pointer; white-space: nowrap;
  opacity: 0; transition: opacity var(--dur-fast) var(--ease),
                          color var(--dur-fast) var(--ease),
                          border-color var(--dur-fast) var(--ease);
}
.bcard:hover .bcard__bak,
.bcard:focus-within .bcard__bak { opacity: 1; }
.bcard__bak:hover:not(:disabled) { color: var(--brand-600); border-color: var(--brand-200); }
.bcard__bak:disabled { cursor: progress; }
.bcard__bak:focus-visible { opacity: 1; outline: 2px solid var(--brand-500); }

/* ★ v1.2.0 卡片「封面」角标：与备份并排（最左），同样悬停/聚焦才显现 */
.bcard__cover {
  position: absolute; top: 4px; right: 70px;
  height: 22px; padding: 0 8px; line-height: 20px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elevated); color: var(--text-muted);
  font-size: 11px; cursor: pointer; white-space: nowrap;
  opacity: 0; transition: opacity var(--dur-fast) var(--ease),
                          color var(--dur-fast) var(--ease),
                          border-color var(--dur-fast) var(--ease);
}
.bcard:hover .bcard__cover,
.bcard:focus-within .bcard__cover { opacity: 1; }
.bcard__cover:hover:not(:disabled) { color: var(--brand-600); border-color: var(--brand-200); }
.bcard__cover:disabled { cursor: progress; }
.bcard__cover:focus-visible { opacity: 1; outline: 2px solid var(--brand-500); }

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
.ltable__acts { display: inline-flex; align-items: center; gap: 6px; }
/* ★ F2 行内「备份」文字按钮：低干扰，与「删除」并排 */
.ltable__bak {
  border: 0; background: none; padding: 2px 4px;
  color: var(--text-subtle); font-size: var(--fs-xs);
  cursor: pointer; border-radius: var(--r-sm);
}
.ltable__bak:hover:not(:disabled) { color: var(--brand-600); background: var(--bg-sunken); }
.ltable__bak:disabled { cursor: progress; }
.ltable__bak:focus-visible { outline: 2px solid var(--brand-500); }

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

/* ★ F2 导入备份弹窗 */
.imp { display: flex; flex-direction: column; gap: var(--sp-3); }
.imp__lead { font-size: var(--fs-sm); color: var(--text-muted); line-height: 1.6; }
.imp__lead code {
  background: var(--bg-sunken); padding: 1px 5px;
  border-radius: var(--r-sm); font-size: var(--fs-xs); color: var(--text);
}
.imp__pick { display: block; }
.imp__pick input[type="file"] { display: none; }
.imp__picktxt {
  display: block; padding: var(--sp-2) var(--sp-3);
  border: 1px dashed var(--border); border-radius: var(--r-md);
  background: var(--bg-elevated); color: var(--text-muted);
  font-size: var(--fs-xs); text-align: center; cursor: pointer;
}
.imp__pick:hover .imp__picktxt { border-color: var(--brand-200); color: var(--text); }
.imp__inspect {
  align-self: flex-start; padding: 4px var(--sp-3);
  border: 1px solid var(--border); border-radius: var(--r-full);
  background: var(--bg-elevated); color: var(--text-muted);
  font-size: var(--fs-xs); cursor: pointer;
}
.imp__inspect:disabled { opacity: .5; cursor: not-allowed; }
.imp__inspect:not(:disabled) { border-color: var(--brand-200); color: var(--brand-600); }
.imp__ok {
  padding: var(--sp-2) var(--sp-3); border-radius: var(--r-md);
  background: color-mix(in srgb, var(--brand-500) 8%, transparent);
  border: 1px solid var(--brand-200);
  font-size: var(--fs-xs); line-height: 1.6;
}
.imp__err {
  padding: var(--sp-2) var(--sp-3); border-radius: var(--r-md);
  background: var(--danger-bg); border: 1px solid var(--danger);
  color: var(--danger); font-size: var(--fs-xs);
}
.imp__mode { display: flex; flex-direction: column; gap: var(--sp-2); }
.imp__radio {
  display: flex; align-items: flex-start; gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--bg-elevated); cursor: pointer;
}
.imp__radio:hover { border-color: var(--brand-200); }
.imp__radio input { margin-top: 2px; accent-color: var(--brand-500); }
.imp__radio b { font-size: var(--fs-sm); font-weight: var(--fw-medium); display: block; }
.imp__foot { margin-top: var(--sp-1); }
.imp__import {
  width: 100%; padding: var(--sp-2) var(--sp-3);
  border: none; border-radius: var(--r-md);
  background: var(--brand-500); color: #fff;
  font-size: var(--fs-sm); font-weight: var(--fw-medium); cursor: pointer;
}
.imp__import:disabled { background: var(--gray-300, #ccc); cursor: not-allowed; }
.imp__import:not(:disabled) { background: var(--brand-600); }

/* ★ v1.2.0 封面预览弹窗 */
.cov { display: flex; flex-direction: column; gap: var(--sp-3); }
.cov__title { font-size: var(--fs-sm); font-weight: var(--fw-medium); }
.cov__stage {
  display: flex; align-items: center; justify-content: center;
  min-height: 240px; border: 1px solid var(--border);
  border-radius: var(--r-md); background: var(--bg-sunken);
  padding: var(--sp-2); overflow: hidden;
}
.cov__img {
  max-height: 340px; max-width: 100%; border-radius: var(--r-sm);
  object-fit: contain; background: #101018;
}
.cov__note {
  padding: var(--sp-2) var(--sp-3); border-radius: var(--r-md);
  background: var(--bg-sunken); font-size: var(--fs-xs); color: var(--text-muted);
  line-height: 1.6;
}
.cov__opts { display: flex; flex-direction: column; gap: var(--sp-2); }
.cov__row {
  display: flex; align-items: center; gap: var(--sp-2);
  flex-wrap: wrap;
}
.cov__lbl { font-size: var(--fs-xs); color: var(--text-muted); min-width: 28px; }
.cov__regen {
  padding: 4px var(--sp-3); border: 1px solid var(--brand-200);
  border-radius: var(--r-full); background: var(--brand-50);
  color: var(--brand-600); font-size: var(--fs-xs); cursor: pointer;
}
.cov__regen:disabled { opacity: .6; cursor: progress; }
.cov__dl {
  padding: 4px var(--sp-3); border: 1px solid var(--border);
  border-radius: var(--r-full); background: var(--bg-elevated);
  color: var(--text-muted); font-size: var(--fs-xs); cursor: pointer;
}
.cov__dl:disabled { opacity: .5; cursor: not-allowed; }
.cov__dl:not(:disabled) { border-color: var(--brand-200); color: var(--brand-600); }

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
