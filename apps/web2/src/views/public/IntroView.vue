<script setup lang="ts">
/**
 * IntroView · 产品功能介绍页（v0.26.1）
 *
 * ★ 这是 SEO 主入口页：语义化标签 + 完整文本内容（爬虫要能读到）。
 * ★ 转化路径：首屏 CTA → 免费注册；底部 CTA 再收一次。
 */
import { RouterLink } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const FEATURES = [
  {
    icon: '✎', title: '长篇写作台',
    desc: '三栏工作台：章节树 · 对话流 · 设定侧栏。AI 代笔、续写、重拍，'
      + '正文质量实时质检，越写越稳不打脸。',
    points: ['七维质检（含境界体系一致性）', '伏笔债务追踪与提醒', '片段重拍不动其他部分'],
  },
  {
    icon: '▶', title: '短剧工坊',
    desc: '小说一键改编短剧：分集 → 剧本 → 资产 → 分镜 → 视频。'
      + '角色参考图跨镜跨集锁定，音色资产化不换人。',
    points: ['八段式分镜提示词', '角色/场景参考图一致性', '按镜头类型自动选模型'],
  },
  {
    icon: '❖', title: '素材与资产库',
    desc: '角色三视图、场景全景、声纹档案统一沉淀为可复用资产，'
      + '新书新剧直接调用，不再每次重新描述。',
    points: ['跨项目复用资产', '多形态角色（青年/老年）', '声纹跨集延续'],
  },
  {
    icon: '▤', title: '用量与成本',
    desc: '按角色路由模型（强模型写正文、便宜模型做摘要），'
      + '成本看板 + 硬上限，花多少心里有数。',
    points: ['按角色的模型路由', '实时用量趋势', '额度预警与硬上限'],
  },
]

const STEPS = [
  { n: '01', t: '立项', d: '填写题材、目标字数、平台偏好，AI 生成总纲与前三章大纲。' },
  { n: '02', t: '写作', d: '对话式驱动 AI 代笔，逐章质检，伏笔与设定自动入账。' },
  { n: '03', t: '改编', d: '一键改编短剧：自动分集、生成剧本与分镜提示词。' },
  { n: '04', t: '出片', d: '生成参考图与分镜图，按镜头类型路由模型出视频，导出成片。' },
]
</script>

<template>
  <div class="intro">
    <!-- ── Hero ── -->
    <section class="hero">
      <p class="hero__eyebrow">AI 长篇创作 · 短剧生成</p>
      <h1 class="hero__title">
        从一句话到一部剧<br>
        <span class="hero__hl">溯字，让好故事被看见</span>
      </h1>
      <p class="hero__sub">
        面向长篇网文与 AI 漫剧的一体化工作台。写作、质检、改编、出片，
        在一处完成 —— 并且保证角色不换脸、声音不换人、设定不打脸。
      </p>
      <div class="hero__cta">
        <RouterLink v-if="!auth.isLoggedIn" class="btn btn--primary btn--lg"
                    :to="{ name: 'register' }">免费开始创作</RouterLink>
        <RouterLink v-else class="btn btn--primary btn--lg"
                    :to="{ name: 'home' }">进入工作台</RouterLink>
        <RouterLink class="btn btn--ghost btn--lg" :to="{ name: 'tech' }">
          了解核心技术
        </RouterLink>
      </div>
      <p class="hero__note">注册即送体验积分 · 无需信用卡</p>
    </section>

    <!-- ── 功能 ── -->
    <section class="sec">
      <h2 class="sec__title">四大核心能力</h2>
      <p class="sec__sub">覆盖从立项到成片的完整链路，每一环都有工程级保障。</p>
      <div class="grid grid--2">
        <article v-for="f in FEATURES" :key="f.title" class="card">
          <div class="card__ico" aria-hidden="true">{{ f.icon }}</div>
          <h3 class="card__title">{{ f.title }}</h3>
          <p class="card__desc">{{ f.desc }}</p>
          <ul class="card__list">
            <li v-for="p in f.points" :key="p">{{ p }}</li>
          </ul>
        </article>
      </div>
    </section>

    <!-- ── 中部广告 ── -->

    <!-- ── 流程 ── -->
    <section class="sec">
      <h2 class="sec__title">四步出片</h2>
      <p class="sec__sub">不需要拼十个工具，一条流水线走完。</p>
      <ol class="steps">
        <li v-for="s in STEPS" :key="s.n" class="step">
          <span class="step__n">{{ s.n }}</span>
          <h3 class="step__t">{{ s.t }}</h3>
          <p class="step__d">{{ s.d }}</p>
        </li>
      </ol>
    </section>

    <!-- ── 尾 CTA ── -->
    <section class="tail">
      <h2 class="tail__title">现在开始你的第一部作品</h2>
      <p class="tail__sub">注册即可试用全部功能，按用量计费，随时可退。</p>
      <div class="tail__cta">
        <RouterLink v-if="!auth.isLoggedIn" class="btn btn--primary btn--lg"
                    :to="{ name: 'register' }">免费注册</RouterLink>
        <RouterLink v-else class="btn btn--primary btn--lg"
                    :to="{ name: 'home' }">进入工作台</RouterLink>
        <RouterLink class="btn btn--ghost btn--lg" :to="{ name: 'pricing' }">查看价格</RouterLink>
      </div>
    </section>

  </div>
</template>

<style scoped>
.hero { text-align: center; padding: var(--sp-7) 0 var(--sp-6); }
.hero__eyebrow {
  display: inline-block; padding: 4px 12px; border-radius: 999px;
  background: var(--brand-soft); color: var(--brand);
  font-size: var(--fs-xs); margin-bottom: var(--sp-3);
}
.hero__title {
  font-size: clamp(28px, 5vw, 48px); line-height: 1.25;
  margin: 0 0 var(--sp-4); color: var(--text); font-weight: 700;
}
.hero__hl {
  background: linear-gradient(100deg, var(--brand), var(--brand-2, var(--brand)));
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.hero__sub {
  max-width: 620px; margin: 0 auto var(--sp-5);
  color: var(--text-muted); font-size: var(--fs-md); line-height: 1.8;
}
.hero__cta { display: flex; gap: var(--sp-3); justify-content: center; flex-wrap: wrap; }
.hero__note { margin-top: var(--sp-3); color: var(--text-faint); font-size: var(--fs-xs); }

.sec { padding: var(--sp-6) 0; }
.sec__title { font-size: var(--fs-xl); margin: 0 0 var(--sp-2); color: var(--text); }
.sec__sub { color: var(--text-muted); margin: 0 0 var(--sp-5); font-size: var(--fs-sm); }

.grid { display: grid; gap: var(--sp-4); }
.grid--2 { grid-template-columns: repeat(2, 1fr); }
.card {
  padding: var(--sp-5); border: 1px solid var(--border);
  border-radius: var(--radius-3, 12px); background: var(--surface);
}
.card__ico {
  width: 40px; height: 40px; border-radius: 10px; display: grid; place-items: center;
  background: var(--brand-soft); color: var(--brand);
  font-size: 20px; margin-bottom: var(--sp-3);
}
.card__title { margin: 0 0 var(--sp-2); font-size: var(--fs-md); color: var(--text); }
.card__desc { margin: 0 0 var(--sp-3); color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.8; }
.card__list { margin: 0; padding-left: 18px; color: var(--text-muted); font-size: var(--fs-sm); line-height: 2; }

.mid-ad { margin: var(--sp-5) 0; }

.steps { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--sp-4); }
.step { padding: var(--sp-4); border-left: 2px solid var(--brand-soft); }
.step__n { font-size: var(--fs-xs); color: var(--brand); font-weight: 700; letter-spacing: .08em; }
.step__t { margin: var(--sp-2) 0; font-size: var(--fs-md); color: var(--text); }
.step__d { margin: 0; color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.8; }

.tail {
  text-align: center; padding: var(--sp-7) var(--sp-4);
  border-radius: var(--radius-3, 12px); background: var(--surface-2);
  margin: var(--sp-5) 0;
}
.tail__title { margin: 0 0 var(--sp-2); font-size: var(--fs-xl); color: var(--text); }
.tail__sub { margin: 0 0 var(--sp-4); color: var(--text-muted); font-size: var(--fs-sm); }
.tail__cta { display: flex; gap: var(--sp-3); justify-content: center; flex-wrap: wrap; }

@media (max-width: 840px) {
  .grid--2 { grid-template-columns: 1fr; }
  .steps { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 520px) {
  .steps { grid-template-columns: 1fr; }
}
</style>
