<script setup lang="ts">
/**
 * DramaListView · 短剧项目列表（v0.26.1）
 *
 * ★★ 架构说明（重要）：
 *   短剧接口是**按书挂载**的：`/api/books/{bookId}/drama/projects`
 *   所以这个页面必须先有"当前书"的上下文。
 *   设计：顶部放书选择器，从「我的作品」列表拉取；选中后展示该书下的短剧项目。
 *   —— 这是后端架构决定的，不是前端设计偏好。
 *
 * ★ 契约：
 *   GET /api/books                    → 裸数组 [{id, title, chapters, words, status}]
 *   GET /api/books/{bid}/drama/projects → { items: [...] }
 *   POST /api/books/{bid}/drama/projects
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'
import AppInput from '@/components/AppInput.vue'
import AppTag from '@/components/AppTag.vue'
import AppModal from '@/components/AppModal.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import { useUiStore } from '@/stores/ui'
import { bookApi } from '@/api/modules/book'
import { dramaApi, type DramaProject } from '@/api/modules/drama'

const ui = useUiStore()
const router = useRouter()

const loading = ref(true)
const books = ref<Array<{ id: string; title: string; chapters?: number; words?: number }>>([])
const bookId = ref('')
const projects = ref<DramaProject[]>([])
const listBusy = ref(false)

const STATUS_MAP: Record<string, { label: string; kind: string }> = {
  draft: { label: '草稿', kind: 'default' },
  planning: { label: '规划中', kind: 'info' },
  ready: { label: '待生成', kind: 'warning' },
  generating: { label: '生成中', kind: 'warning' },
  done: { label: '已完成', kind: 'success' },
}

// 新建弹窗
const newOpen = ref(false)
const newBusy = ref(false)
const form = ref({
  title: '', style: '日系二次元漫画，线条干净，光影柔和',
  content_mode: 'story', generation_mode: 'storyboard', aspect_ratio: '9:16',
})

const newOpenSafe = computed({
  get: () => newOpen.value,
  set: (v: boolean) => { newOpen.value = v },
})

const curBook = computed(() => books.value.find(b => b.id === bookId.value))

async function loadBooks() {
  try {
    const r = await bookApi.list()
    // ★ 兼容裸数组（当前契约）与 {items}（防御未来变更）
    books.value = Array.isArray(r) ? r : ((r as { items?: never[] }).items || [])
    if (books.value.length && !bookId.value) {
      bookId.value = books.value[0].id
    }
  } catch (e) {
    ui.err((e as Error).message || '加载作品失败')
  }
}

async function loadProjects() {
  if (!bookId.value) { projects.value = []; return }
  listBusy.value = true
  try {
    const r = await dramaApi.listProjects(bookId.value)
    projects.value = r.items || []
  } catch (e) {
    ui.err((e as Error).message || '加载短剧项目失败')
    projects.value = []
  } finally {
    listBusy.value = false
  }
}

async function boot() {
  loading.value = true
  await loadBooks()
  await loadProjects()
  loading.value = false
}

watch(bookId, loadProjects)

function openNew() {
  if (!bookId.value) { ui.warn('请先选择一本书'); return }
  form.value.title = curBook.value
    ? `${curBook.value.title} · 短剧` : '未命名短剧'
  newOpen.value = true
}

async function doCreate() {
  if (!form.value.title.trim()) { ui.warn('请填写项目名称'); return }
  newBusy.value = true
  try {
    const r = await dramaApi.createProject(bookId.value, { ...form.value })
    ui.ok('短剧项目已创建')
    newOpen.value = false
    const pid = (r as { id?: string }).id
    if (pid) {
      router.push({ name: 'drama-detail', params: { projectId: pid },
                    query: { book: bookId.value } })
    } else {
      await loadProjects()
    }
  } catch (e) {
    ui.err((e as Error).message || '创建失败')
  } finally {
    newBusy.value = false
  }
}

function open(p: DramaProject) {
  router.push({ name: 'drama-detail', params: { projectId: p.id },
                query: { book: bookId.value } })
}

function fmt(s?: string) { return s ? s.replace('T', ' ').slice(0, 10) : '—' }

onMounted(boot)
</script>

<template>
  <div class="page">
    <header class="page__head">
      <div>
        <h1 class="page__title">短剧工坊</h1>
        <p class="page__sub">把小说改编成短剧：分集 → 剧本 → 分镜 → 视频</p>
      </div>
      <div class="head-actions">
        <select v-if="books.length" v-model="bookId" class="picker" aria-label="选择作品">
          <option v-for="b in books" :key="b.id" :value="b.id">
            {{ b.title }}（{{ b.chapters || 0 }} 章）
          </option>
        </select>
        <AppButton variant="primary" @click="openNew">新建短剧</AppButton>
      </div>
    </header>

    <AppSkeleton v-if="loading" variant="card" :count="3" />

    <AppEmpty v-else-if="!books.length" icon="✎" title="还没有作品"
              description="短剧由小说改编而来，请先创建一部作品。">
      <template #action>
        <AppButton variant="primary" @click="router.push({ name: 'books' })">
          去创建作品
        </AppButton>
      </template>
    </AppEmpty>

    <AppCard v-else :title="`《${curBook?.title || ''}》下的短剧`"
             :subtitle="`${projects.length} 个项目`">
      <AppSkeleton v-if="listBusy" variant="text" :rows="3" />
      <AppEmpty v-else-if="!projects.length" icon="▶" title="还没有短剧项目"
                description="点击右上角「新建短剧」开始改编。" />
      <ul v-else class="list">
        <li v-for="p in projects" :key="p.id" class="proj" @click="open(p)">
          <div class="proj__main">
            <div class="proj__title">
              {{ p.title }}
              <AppTag :kind="(STATUS_MAP[p.status]?.kind || 'default') as never" size="sm">
                {{ STATUS_MAP[p.status]?.label || p.status || '草稿' }}
              </AppTag>
            </div>
            <div class="muted tiny proj__meta">
              {{ p.aspect_ratio }} · {{ p.content_mode === 'story' ? '剧情向' : p.content_mode }}
              · {{ p.generation_mode === 'storyboard' ? '分镜模式' : p.generation_mode }}
              · 创建于 {{ fmt(p.created_at) }}
            </div>
            <p v-if="p.style" class="muted tiny proj__style">风格：{{ p.style }}</p>
          </div>
          <span class="proj__go">进入 →</span>
        </li>
      </ul>
    </AppCard>

    <AppModal v-model="newOpenSafe" title="新建短剧项目" ok-text="创建"
              :loading="newBusy" @ok="doCreate">
      <div class="form">
        <AppInput v-model="form.title" label="项目名称" :maxlength="64" />
        <div class="field">
          <label>画面风格</label>
          <input v-model="form.style" class="ipt" maxlength="200"
                 placeholder="如：日系二次元漫画，线条干净">
          <p class="hint">风格词会注入每个分镜的提示词，全片统一</p>
        </div>
        <div class="row2">
          <div class="field">
            <label>内容模式</label>
            <select v-model="form.content_mode">
              <option value="story">剧情向</option>
              <option value="comedy">喜剧向</option>
              <option value="suspense">悬疑向</option>
            </select>
          </div>
          <div class="field">
            <label>生成模式</label>
            <select v-model="form.generation_mode">
              <option value="storyboard">分镜模式（推荐）</option>
              <option value="direct">直出模式</option>
            </select>
          </div>
        </div>
        <div class="field">
          <label>画幅比例</label>
          <select v-model="form.aspect_ratio">
            <option value="9:16">9:16 竖屏（短视频）</option>
            <option value="16:9">16:9 横屏</option>
            <option value="1:1">1:1 方形</option>
          </select>
        </div>
      </div>
    </AppModal>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 1000px; margin: 0 auto; }
.page__head { display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--sp-4); margin-bottom: var(--sp-5); flex-wrap: wrap; }
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); }
.head-actions { display: flex; gap: var(--sp-2); align-items: center; }
.picker { padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); color: var(--text); font-size: var(--fs-sm);
  font-family: inherit; max-width: 240px; }

.list { list-style: none; margin: 0; padding: 0; }
.proj { display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-4); padding: var(--sp-4) 0; border-bottom: 1px solid var(--border);
  cursor: pointer; transition: background .15s; }
.proj:last-child { border-bottom: none; }
.proj:hover { background: var(--surface-2); }
.proj__main { flex: 1; min-width: 0; }
.proj__title { display: flex; align-items: center; gap: var(--sp-2);
  font-size: var(--fs-md); margin-bottom: 5px; }
.proj__meta { display: block; }
.proj__style { margin: 5px 0 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.proj__go { color: var(--brand); font-size: var(--fs-sm); flex: none; }

.form { display: flex; flex-direction: column; gap: var(--sp-3); }
.field label { display: block; font-size: var(--fs-sm); margin-bottom: 6px; }
.row2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); }
.ipt, select { width: 100%; padding: 9px 12px; border: 1px solid var(--border);
  border-radius: 8px; background: var(--surface); color: var(--text);
  font-size: var(--fs-sm); font-family: inherit; }
.hint { margin: 6px 0 0; font-size: var(--fs-xs); color: var(--text-faint); }
</style>
