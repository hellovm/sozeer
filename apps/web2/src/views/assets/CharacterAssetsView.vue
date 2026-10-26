<script setup lang="ts">
/**
 * CharacterAssetsView · 角色资产库（v0.26.1）
 *
 * ★ 说明：角色资产目前**按书挂载**（短剧项目下）。
 *   这里做"跨项目总览"：先选书 → 遍历该书下的短剧项目 → 聚合角色资产。
 *   ★ 这是后端架构决定的（资产挂在 DramaProject 下）。
 *
 * ★ 为什么需要这个页面：角色三视图是**跨镜跨集一致性**的锚点，
 *   作者需要一个地方统一查看"我这个角色到底有几套形象"。
 */
import { computed, onMounted, ref } from 'vue'
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'
import AppTag from '@/components/AppTag.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import AppInput from '@/components/AppInput.vue'
import { useUiStore } from '@/stores/ui'
import { bookApi } from '@/api/modules/book'
import { dramaApi, type DramaAsset, type DramaProject } from '@/api/modules/drama'

const ui = useUiStore()
const loading = ref(true)
const books = ref<Array<{ id: string; title: string }>>([])
const bookId = ref('')
const projects = ref<DramaProject[]>([])
const assets = ref<Array<DramaAsset & { projectTitle: string }>>([])
const q = ref('')

const filtered = computed(() => {
  const k = q.value.trim().toLowerCase()
  if (!k) return assets.value
  return assets.value.filter(a =>
    a.name.toLowerCase().includes(k) ||
    (a.description || '').toLowerCase().includes(k))
})

const characters = computed(() => filtered.value.filter(a => a.kind === 'character'))
const scenes = computed(() => filtered.value.filter(a => a.kind !== 'character'))

function mediaUrl(p?: string) {
  if (!p) return ''
  return p.startsWith('http') ? p : `/api/files/${p}`
}

/** ★ 聚合：遍历该书下所有短剧项目，拉取资产（后端无全局资产接口） */
async function loadAssets() {
  assets.value = []
  if (!bookId.value) return
  try {
    const r = await dramaApi.listProjects(bookId.value)
    projects.value = r.items || []
    const results = await Promise.all(
      projects.value.map(async p => {
        try {
          const a = await dramaApi.assets(bookId.value, p.id)
          return (a.items || []).map(x => ({ ...x, projectTitle: p.title }))
        } catch { return [] }
      }))
    assets.value = results.flat()
  } catch (e) {
    ui.err((e as Error).message || '加载资产失败')
  }
}

async function boot() {
  loading.value = true
  try {
    const r = await bookApi.list()
    books.value = Array.isArray(r) ? r : ((r as unknown as { items?: never[] }).items || [])
    if (books.value.length) {
      bookId.value = books.value[0].id
      await loadAssets()
    }
  } finally { loading.value = false }
}

async function onBookChange() {
  loading.value = true
  await loadAssets()
  loading.value = false
}

onMounted(boot)
</script>

<template>
  <div class="page">
    <header class="page__head">
      <div>
        <h1 class="page__title">角色资产库</h1>
        <p class="page__sub">
          角色三视图与场景全景 —— 跨镜跨集一致性的锚点
        </p>
      </div>
      <div class="head-actions">
        <select v-if="books.length" v-model="bookId" class="picker"
                aria-label="选择作品" @change="onBookChange">
          <option v-for="b in books" :key="b.id" :value="b.id">{{ b.title }}</option>
        </select>
        <AppButton variant="ghost" :disabled="!bookId" @click="onBookChange">刷新</AppButton>
      </div>
    </header>

    <AppSkeleton v-if="loading" variant="card" :count="3" />

    <AppEmpty v-else-if="!books.length" icon="✎" title="还没有作品"
              description="角色资产来自短剧项目，请先创建作品。" />

    <template v-else>
      <AppCard>
        <AppInput v-model="q" placeholder="搜索角色或场景名…" clearable />
        <p class="muted tiny" style="margin-top:8px">
          共 {{ assets.length }} 项资产 · {{ projects.length }} 个短剧项目 ·
          角色 {{ characters.length }} / 场景 {{ scenes.length }}
        </p>
      </AppCard>

      <AppCard title="角色" :subtitle="`${characters.length} 个`" class="mt">
        <AppEmpty v-if="!characters.length" icon="☺" title="暂无角色资产"
                  description="在短剧制作台点「从正文同步角色」自动提取。" />
        <div v-else class="grid">
          <article v-for="a in characters" :key="a.id" class="item">
            <img v-if="a.image" :src="mediaUrl(a.image)" :alt="a.name" loading="lazy">
            <div v-else class="item__ph">人</div>
            <div class="item__body">
              <b>{{ a.name }}</b>
              <AppTag v-if="a.form_name" size="sm" kind="brand">{{ a.form_name }}</AppTag>
              <p class="muted tiny">{{ (a.description || '').slice(0, 70) }}</p>
              <AppTag v-if="a.voice_id" size="sm" kind="info">♪ {{ a.voice_id }}</AppTag>
              <span class="muted tiny item__proj">{{ a.projectTitle }}</span>
            </div>
          </article>
        </div>
      </AppCard>

      <AppCard title="场景" :subtitle="`${scenes.length} 个`" class="mt">
        <AppEmpty v-if="!scenes.length" icon="❖" title="暂无场景资产" />
        <div v-else class="grid">
          <article v-for="a in scenes" :key="a.id" class="item">
            <img v-if="a.image" :src="mediaUrl(a.image)" :alt="a.name" loading="lazy">
            <div v-else class="item__ph">景</div>
            <div class="item__body">
              <b>{{ a.name }}</b>
              <AppTag size="sm">{{ a.kind }}</AppTag>
              <p class="muted tiny">{{ (a.description || '').slice(0, 70) }}</p>
            </div>
          </article>
        </div>
      </AppCard>
    </template>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 1080px; margin: 0 auto; }
.page__head { display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--sp-4); margin-bottom: var(--sp-4); flex-wrap: wrap; }
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); }
.head-actions { display: flex; gap: var(--sp-2); align-items: center; }
.picker { padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); color: var(--text); font-size: var(--fs-sm);
  font-family: inherit; max-width: 220px; }
.mt { margin-top: var(--sp-4); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: var(--sp-3); }
.item { border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
.item img { width: 100%; aspect-ratio: 3/4; object-fit: cover; display: block; }
.item__ph { aspect-ratio: 3/4; display: grid; place-items: center;
  background: var(--surface-2); color: var(--text-faint); font-size: 22px; }
.item__body { padding: var(--sp-2); display: flex; flex-direction: column; gap: 4px; }
.item__body b { font-size: var(--fs-sm); }
.item__body p { margin: 0; line-height: 1.6; }
.item__proj { margin-top: 2px; font-style: italic; }
</style>
