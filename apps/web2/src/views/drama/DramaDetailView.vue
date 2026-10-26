<script setup lang="ts">
/**
 * DramaDetailView · 短剧制作台（v0.26.1）
 *
 * ★ 契约（全部按书挂载）：
 *   GET  /api/books/{bid}/drama/projects/{pid}                项目详情
 *   GET  .../episodes/{n}                                     分集详情（含 scenes）
 *   POST .../plan                                             生成分集
 *   POST .../episodes/{n}/script                              生成剧本
 *   POST .../episodes/{n}/storyboard                          生成分镜（八段提示词）
 *   POST .../episodes/{n}/generate-all/{image|video|audio}  批量生成
 *   POST .../scenes/{sid}/generate/{target}                    单镜生成
 *   POST .../episodes/{n}/confirm                              确认分集
 *   POST .../film/{n}                                          成片导出
 *   POST .../export-jianying/{n}                               剪映草稿
 *   GET  .../voice-cast                                        音色清单
 *   GET  .../assets                                            资产
 *
 * ★★ 制作链路（顺序不能乱）：
 *   ① plan（分集）→ ② script（剧本）→ ③ storyboard（分镜提示词）
 *   → ④ generate-all/image → ⑤ generate-all/video → ⑥ film（成片）
 *   每一步都依赖前一步的产物，UI 要按此顺序引导。
 *
 * ★ 一致性设计（本项目的核心卖点，UI 要体现出来）：
 *   · 资产（角色/场景）是**跨镜跨集复用**的锚点
 *   · 音色（voice-cast）是**跨集音色一致**的保障
 *   · 这两块放在独立 Tab，强调"先建资产再做分镜"
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'
import AppTag from '@/components/AppTag.vue'
import AppTabs from '@/components/AppTabs.vue'
import AppProgress from '@/components/AppProgress.vue'
import AppSkeleton from '@/components/AppSkeleton.vue'
import AppEmpty from '@/components/AppEmpty.vue'
import type { TabItem } from '@/types/components'
import { useUiStore } from '@/stores/ui'
import {
  dramaApi, type DramaAsset, type DramaProject, type DramaScene, type VoiceCastEntry,
} from '@/api/modules/drama'

const route = useRoute()
const router = useRouter()
const ui = useUiStore()

const bookId = computed(() => String(route.query.book || ''))
const projectId = computed(() => String(route.params.projectId || ''))

const loading = ref(true)
const busy = ref('')
const tab = ref('episodes')

const project = ref<DramaProject | null>(null)
const episode = ref(1)
const scenes = ref<DramaScene[]>([])
const episodeBusy = ref(false)
const assets = ref<DramaAsset[]>([])
const voices = ref<VoiceCastEntry[]>([])

const TABS: TabItem[] = [
  { key: 'episodes', label: '分集制作' },
  { key: 'assets', label: '资产（角色/场景）' },
  { key: 'voices', label: '音色' },
]

const IMG_STORYBOARD_KEYS = ['CHARACTER', 'BACKGROUND', 'ACTION', 'SCENE',
  'CAMERA', 'LIGHT', 'TEXT', 'STYLE']

/** ★ 角色定妆图就绪度（E4.23 资产自动管线）：本体角色全有定妆图才算完成 */
const charBaseAssets = computed(() =>
  assets.value.filter(a => a.kind === 'character' && !a.parent_id))
const charBaseDone = computed(() =>
  charBaseAssets.value.filter(a => a.image && a.image_status === 'done').length)

/** ★ 制作进度：5 个阶段按产物判定（不是靠状态字段猜） */
const progress = computed(() => {
  const total = scenes.value.length
  const withImg = scenes.value.filter(s => s.image_path).length
  const withVid = scenes.value.filter(s => s.video_path).length
  const withAudio = scenes.value.filter(s => s.audio_path).length
  const hasScript = scenes.value.some(s => s.scene_description)
  const hasBoard = scenes.value.some(s =>
    s.image_prompt && IMG_STORYBOARD_KEYS.some(k => k in (s.image_prompt || {})))
  const steps = [
    { label: '角色定妆图',
      done: charBaseAssets.value.length === 0 || charBaseDone.value === charBaseAssets.value.length },
    { label: '剧本', done: hasScript },
    { label: '分镜提示词', done: hasBoard },
    { label: '分镜图', done: total > 0 && withImg === total },
    { label: '分镜视频', done: total > 0 && withVid === total },
  ]
  const done = steps.filter(s => s.done).length
  return { steps, percent: Math.round((done / steps.length) * 100),
           total, withImg, withVid, withAudio, hasScript, hasBoard }
})

async function loadProject() {
  if (!bookId.value || !projectId.value) return
  try {
    project.value = await dramaApi.project(bookId.value, projectId.value)
  } catch (e) {
    ui.err((e as Error).message || '加载项目失败')
  }
}

async function loadEpisode() {
  if (!bookId.value || !projectId.value) return
  episodeBusy.value = true
  try {
    const r = await dramaApi.episode(bookId.value, projectId.value, episode.value)
    scenes.value = r.scenes || []
  } catch {
    scenes.value = []       // 该集尚未生成 → 空态（不是错误）
  } finally {
    episodeBusy.value = false
  }
}

async function loadAssets() {
  try {
    const r = await dramaApi.assets(bookId.value, projectId.value)
    assets.value = r.items || []
  } catch { assets.value = [] }
}

async function loadVoices() {
  try {
    const r = await dramaApi.voiceCast(bookId.value, projectId.value)
    voices.value = r.items || []
  } catch { voices.value = [] }
}

async function boot() {
  if (!bookId.value) {
    ui.warn('缺少作品上下文，请从短剧列表进入')
    router.push({ name: 'drama' })
    return
  }
  loading.value = true
  await loadProject()
  await Promise.all([loadEpisode(), loadAssets(), loadVoices()])
  loading.value = false
}

/** 统一的动作执行器（防重复点击 + 统一错误提示） */
async function run(key: string, fn: () => Promise<unknown>, okMsg: string) {
  busy.value = key
  try {
    await fn()
    ui.ok(okMsg)
    await Promise.all([loadEpisode(), loadProject(), loadAssets()])
  } catch (e) {
    ui.err((e as Error).message || '操作失败')
  } finally {
    busy.value = ''
  }
}

const hasScenes = computed(() => scenes.value.length > 0)

function fmt(s?: string) { return s ? s.replace('T', ' ').slice(0, 16) : '—' }

function mediaUrl(p?: string) {
  if (!p) return ''
  return p.startsWith('http') ? p : `/api/files/${p}`
}

onMounted(boot)
</script>

<template>
  <div class="page">
    <!-- 头部 -->
    <header class="page__head">
      <div>
        <div class="crumb">
          <RouterLink :to="{ name: 'drama' }">短剧工坊</RouterLink>
          <span>/</span>
          <span>{{ project?.title || '加载中…' }}</span>
        </div>
        <h1 class="page__title">{{ project?.title || '短剧制作台' }}</h1>
        <p class="page__sub">
          {{ project?.aspect_ratio }} ·
          {{ project?.content_mode === 'story' ? '剧情向' : project?.content_mode }} ·
          {{ project?.generation_mode === 'storyboard' ? '分镜模式' : project?.generation_mode }}
        </p>
      </div>
      <AppButton variant="ghost" @click="boot">刷新</AppButton>
    </header>

    <AppSkeleton v-if="loading" variant="card" :count="2" />

    <template v-else>
      <!-- 制作进度 -->
      <AppCard title="制作进度">
        <AppProgress :percent="progress.percent" :label="`${progress.percent}%`"
                     :detail="`${progress.withImg}/${progress.total} 张分镜图 · `
                       + `${progress.withVid}/${progress.total} 段视频`" />
        <div class="steps">
          <div v-for="(s, i) in progress.steps" :key="s.label" class="step"
               :class="{ done: s.done }">
            <span class="step__n">{{ i + 1 }}</span>
            <span class="step__l">{{ s.label }}</span>
            <AppTag v-if="s.done" kind="success" size="sm">✓</AppTag>
          </div>
        </div>
      </AppCard>

      <!-- 操作条 -->
      <AppCard title="生成操作" class="mt">
        <div class="ops">
          <div class="op">
            <div>
              <b>① 分集规划</b>
              <p class="muted tiny">AI 按导演视角切分集数（依赖小说已有章节）</p>
            </div>
            <AppButton :loading="busy === 'plan'"
                       @click="run('plan', () => dramaApi.plan(bookId, projectId), '分集规划已提交')">
              开始规划
            </AppButton>
          </div>

          <div class="op">
            <div>
              <b>② 生成剧本（第 {{ episode }} 集）</b>
              <p class="muted tiny">把小说源文改编成短剧剧本</p>
            </div>
            <AppButton :loading="busy === 'script'"
                       @click="run('script', () => dramaApi.genScript(bookId, projectId, episode), '剧本生成已提交')">
              生成剧本
            </AppButton>
          </div>

          <div class="op">
            <div>
              <b>③ 生成分镜提示词（第 {{ episode }} 集）</b>
              <p class="muted tiny">
                八段结构：角色 / 背景 / 动作 / 场景 / 镜头 / 光线 / 文字 / 风格
                —— <b>角色段逐字复用资产，保证跨镜一致</b>
              </p>
            </div>
            <AppButton :loading="busy === 'board'"
                       @click="run('board', () => dramaApi.genStoryboard(bookId, projectId, episode), '分镜提示词生成已提交')">
              生成分镜
            </AppButton>
          </div>

          <div class="op">
            <div>
              <b>④ 批量生成媒体</b>
              <p class="muted tiny">分镜图 → 分镜视频 → 配音（建议按序）·
                出图/出视频前自动补全本镜角色的定妆参考图（跨镜一致性锚点）</p>
            </div>
            <div class="row">
              <AppButton size="sm" :disabled="!hasScenes" :loading="busy === 'imgs'"
                         @click="run('imgs', () => dramaApi.generateAll(bookId, projectId, episode, 'image'), '分镜图生成已提交')">
                生成图
              </AppButton>
              <AppButton size="sm" :disabled="!hasScenes" :loading="busy === 'vids'"
                         @click="run('vids', () => dramaApi.generateAll(bookId, projectId, episode, 'video'), '分镜视频生成已提交')">
                生成视频
              </AppButton>
              <AppButton size="sm" :disabled="!hasScenes" :loading="busy === 'auds'"
                         @click="run('auds', () => dramaApi.generateAll(bookId, projectId, episode, 'audio'), '配音生成已提交')">
                生成配音
              </AppButton>
            </div>
          </div>

          <div class="op">
            <div>
              <b>⑤ 导出（第 {{ episode }} 集）</b>
              <p class="muted tiny">成片 = 拼接 + 混音 + 硬字幕；剪映草稿可继续精修</p>
            </div>
            <div class="row">
              <AppButton size="sm" variant="primary" :disabled="!progress.withVid"
                         :loading="busy === 'film'"
                         @click="run('film', () => dramaApi.film(bookId, projectId, episode), '成片导出已提交')">
                导出成片
              </AppButton>
              <AppButton size="sm" :disabled="!hasScenes" :loading="busy === 'jy'"
                         @click="run('jy', () => dramaApi.exportJianying(bookId, projectId, episode), '剪映草稿已生成')">
                剪映草稿
              </AppButton>
            </div>
          </div>
        </div>
      </AppCard>

      <!-- Tab -->
      <AppTabs v-model="tab" :items="TABS" class="mt" />

      <!-- 分集制作 -->
      <AppCard v-if="tab === 'episodes'">
        <div class="epbar">
          <label>集数</label>
          <input v-model.number="episode" class="epnum" type="number" min="1"
                 @change="loadEpisode">
          <AppButton size="sm" :loading="episodeBusy" @click="loadEpisode">加载</AppButton>
          <span class="muted tiny">共 {{ scenes.length }} 个分镜</span>
        </div>

        <AppSkeleton v-if="episodeBusy" variant="card" :count="2" />
        <AppEmpty v-else-if="!scenes.length" icon="🎬" title="这一集还没有分镜"
                  description="先点上面的「③ 生成分镜提示词」。" />
        <div v-else class="shots">
          <article v-for="s in scenes" :key="s.scene_id" class="shot">
            <div class="shot__media">
              <img v-if="s.image_path" :src="mediaUrl(s.image_path)" :alt="s.scene_id" loading="lazy">
              <div v-else class="shot__ph">无图</div>
            </div>
            <div class="shot__body">
              <div class="shot__head">
                <b>{{ s.scene_id }}</b>
                <AppTag v-if="s.video_path" kind="success" size="sm">视频</AppTag>
                <AppTag v-if="s.audio_path" kind="info" size="sm">配音</AppTag>
                <span v-if="s.duration_seconds" class="muted tiny">{{ s.duration_seconds }}s</span>
              </div>
              <p class="shot__desc">{{ s.scene_description || '（无描述）' }}</p>

              <!-- 八段提示词折叠展示 -->
              <details v-if="s.image_prompt" class="shot__prompt">
                <summary class="muted tiny">八段提示词</summary>
                <dl>
                  <template v-for="k in IMG_STORYBOARD_KEYS" :key="k">
                    <div v-if="s.image_prompt[k]">
                      <dt>{{ k }}</dt><dd>{{ s.image_prompt[k] }}</dd>
                    </div>
                  </template>
                </dl>
              </details>

              <div v-if="s.characters_in_scene?.length" class="shot__chars">
                <AppTag v-for="c in s.characters_in_scene" :key="c" size="sm">{{ c }}</AppTag>
              </div>

              <div class="row" style="margin-top:10px">
                <AppButton size="sm" :loading="busy === `img-${s.scene_id}`"
                           @click="run(`img-${s.scene_id}`, () => dramaApi.genScene(bookId, projectId, s.scene_id, 'image'), '分镜图生成已提交')">
                  出图
                </AppButton>
                <AppButton size="sm" :loading="busy === `vid-${s.scene_id}`"
                           @click="run(`vid-${s.scene_id}`, () => dramaApi.genScene(bookId, projectId, s.scene_id, 'video'), '视频生成已提交')">
                  出视频
                </AppButton>
                <AppButton size="sm" :loading="busy === `aud-${s.scene_id}`"
                           @click="run(`aud-${s.scene_id}`, () => dramaApi.genScene(bookId, projectId, s.scene_id, 'audio'), '配音生成已提交')">
                  出配音
                </AppButton>
              </div>
            </div>
          </article>
        </div>
      </AppCard>

      <!-- 资产 -->
      <AppCard v-else-if="tab === 'assets'"
               subtitle="角色与场景是跨镜跨集一致性的锚点 —— 先建资产，再做分镜">
        <div class="row" style="margin-bottom:12px">
          <AppButton size="sm" :loading="busy === 'enrich'"
                     @click="run('enrich', () => dramaApi.enrichAssets(bookId, projectId), '已从正文同步角色')">
            从正文同步角色
          </AppButton>
          <AppButton size="sm" :loading="busy === 'assetimg'"
                     @click="run('assetimg', () => dramaApi.generateAssetImages(bookId, projectId), '定妆图生成已提交')">
            批量生成定妆图
          </AppButton>
        </div>
        <AppEmpty v-if="!assets.length" icon="❖" title="还没有资产"
                  description="点「从正文同步角色」自动提取。" />
        <div v-else class="assets">
          <article v-for="a in assets" :key="a.id" class="asset">
            <!-- ★ 契约：后端资产列表返回 image / image_status（非 image_path） -->
            <img v-if="a.image" :src="mediaUrl(a.image)" :alt="a.name" loading="lazy">
            <div v-else class="asset__ph">{{ a.kind === 'character' ? '人' : '景' }}</div>
            <div class="asset__body">
              <b>{{ a.name }}</b>
              <AppTag size="sm">{{ a.kind === 'character' ? '角色'
                : a.kind === 'scene' ? '场景' : a.kind }}</AppTag>
              <p class="muted tiny">{{ (a.description || '').slice(0, 80) }}</p>
              <AppTag v-if="a.image_status === 'pending'" kind="info" size="sm">定妆图中…</AppTag>
              <AppTag v-else-if="a.image_status === 'error'" kind="danger" size="sm">定妆图失败</AppTag>
              <AppTag v-else-if="a.kind === 'character' && !a.image" size="sm">无定妆图</AppTag>
              <AppTag v-if="a.voice_id" kind="info" size="sm">音色 {{ a.voice_id }}</AppTag>
            </div>
          </article>
        </div>
      </AppCard>

      <!-- 音色 -->
      <AppCard v-else
               subtitle="音色资产化 = 跨集音色一致的保障（参考音频 > 音色名）">
        <AppButton size="sm" :loading="busy === 'auto'"
                   @click="run('auto', () => dramaApi.autoVoiceCast(bookId, projectId), '已自动匹配音色')">
          自动匹配音色
        </AppButton>
        <AppEmpty v-if="!voices.length" icon="♪" title="还没有音色配置"
                  description="点「自动匹配音色」按角色特征分配合适声线。" />
        <table v-else class="tbl">
          <thead><tr><th>角色</th><th>音色</th><th>参考音频</th><th>备注</th></tr></thead>
          <tbody>
            <tr v-for="v in voices" :key="v.character">
              <td><b>{{ v.character }}</b></td>
              <td><AppTag size="sm" kind="info">{{ v.voice_id || '未设置' }}</AppTag></td>
              <td class="muted tiny">{{ v.ref_audio ? '已绑定' : '—' }}</td>
              <td class="muted tiny">{{ v.note || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </AppCard>
    </template>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 1080px; margin: 0 auto; }
.page__head { display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--sp-4); margin-bottom: var(--sp-4); }
.crumb { font-size: var(--fs-xs); color: var(--text-faint); margin-bottom: 6px;
  display: flex; gap: 6px; }
.crumb a { color: var(--text-muted); text-decoration: none; }
.crumb a:hover { color: var(--brand); }
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); }
.mt { margin-top: var(--sp-4); }

.steps { display: flex; gap: var(--sp-4); margin-top: var(--sp-4); flex-wrap: wrap; }
.step { display: flex; align-items: center; gap: 6px; font-size: var(--fs-sm);
  color: var(--text-faint); }
.step.done { color: var(--text); }
.step__n { width: 20px; height: 20px; border-radius: 50%; display: grid; place-items: center;
  background: var(--surface-2); font-size: 11px; }
.step.done .step__n { background: var(--success); color: #fff; }

.ops { display: flex; flex-direction: column; }
.op { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-4);
  padding: var(--sp-3) 0; border-bottom: 1px solid var(--border); }
.op:last-child { border-bottom: none; }
.op b { font-size: var(--fs-sm); }
.op p { margin: 4px 0 0; }
.row { display: flex; gap: var(--sp-2); flex-wrap: wrap; }

.epbar { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: var(--sp-3); }
.epbar label { font-size: var(--fs-sm); color: var(--text-muted); }
.epnum { width: 80px; padding: 6px 10px; border: 1px solid var(--border);
  border-radius: 6px; background: var(--surface); color: var(--text); font-family: inherit; }

.shots { display: flex; flex-direction: column; gap: var(--sp-3); }
.shot { display: grid; grid-template-columns: 160px 1fr; gap: var(--sp-3);
  padding: var(--sp-3); border: 1px solid var(--border); border-radius: 10px; }
.shot__media img { width: 100%; border-radius: 8px; display: block; }
.shot__ph { aspect-ratio: 9/16; display: grid; place-items: center; border-radius: 8px;
  background: var(--surface-2); color: var(--text-faint); font-size: var(--fs-xs); }
.shot__head { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: 6px;
  flex-wrap: wrap; }
.shot__desc { margin: 0 0 8px; font-size: var(--fs-sm); color: var(--text-muted);
  line-height: 1.7; }
.shot__prompt summary { cursor: pointer; }
.shot__prompt dl { margin: 8px 0 0; }
.shot__prompt dl > div { display: grid; grid-template-columns: 82px 1fr; gap: 8px;
  padding: 3px 0; font-size: var(--fs-xs); }
.shot__prompt dt { color: var(--brand); font-family: var(--font-mono, monospace); }
.shot__prompt dd { margin: 0; color: var(--text-muted); }
.shot__chars { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 8px; }

.assets { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: var(--sp-3); }
.asset { border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
.asset img { width: 100%; aspect-ratio: 3/4; object-fit: cover; display: block; }
.asset__ph { aspect-ratio: 3/4; display: grid; place-items: center;
  background: var(--surface-2); color: var(--text-faint); font-size: 22px; }
.asset__body { padding: var(--sp-2); display: flex; flex-direction: column; gap: 4px; }
.asset__body b { font-size: var(--fs-sm); }
.asset__body p { margin: 0; line-height: 1.6; }

.tbl { width: 100%; border-collapse: collapse; margin-top: var(--sp-3); }
.tbl th { text-align: left; font-size: var(--fs-xs); color: var(--text-faint);
  padding: 6px 4px; border-bottom: 1px solid var(--border); font-weight: 500; }
.tbl td { padding: 9px 4px; border-bottom: 1px solid var(--border); font-size: var(--fs-sm); }

@media (max-width: 640px) {
  .shot { grid-template-columns: 1fr; }
  .op { flex-direction: column; align-items: flex-start; }
}
</style>
