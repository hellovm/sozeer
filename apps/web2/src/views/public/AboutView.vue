<script setup lang="ts">
/**
 * AboutView · 关于我们（v0.26.1）
 */
import { RouterLink } from 'vue-router'

const VALUES = [
  { t: '创作者优先', d: '所有设计决策的第一个问题是"这会让作者少写一个字吗"。工具应该消失，作品应该留下。' },
  { t: '工程诚实', d: '不做"AI 味"的漂亮话。质检报告会告诉你哪里不行，以及怎么改 —— 哪怕不好听。' },
  { t: '数据最小化', d: '只收集让产品跑起来必需的匿名聚合数据，遥测默认开启（可随时关闭），导出随时可用。' },
]

const MILESTONES = [
  { t: '2026 · Q2', d: '项目启动，从零构建长篇一致性引擎。' },
  { t: '2026 · Q3', d: '短剧工坊上线，打通分镜与视频生成。' },
  { t: '2026 · Q4', d: '商业化与开放模型生态，支持自建 ComfyUI。' },
]
</script>

<template>
  <div class="about">
    <header class="hd">
      <p class="hd__eyebrow">关于我们</p>
      <h1 class="hd__title">让每一个认真讲故事的人<br>都被看见</h1>
      <p class="hd__sub">
        我们是一支做工程出身的小团队。见过太多作者把时间花在"跟工具较劲"上，
        而不是花在故事上。溯字要解决的就是这件事。
      </p>
    </header>

    <section class="sec">
      <h2 class="sec__title">我们相信</h2>
      <div class="vals">
        <article v-for="v in VALUES" :key="v.t" class="val">
          <h3 class="val__t">{{ v.t }}</h3>
          <p class="val__d">{{ v.d }}</p>
        </article>
      </div>
    </section>


    <section class="sec">
      <h2 class="sec__title">发展历程</h2>
      <ol class="ms">
        <li v-for="m in MILESTONES" :key="m.t" class="ms__row">
          <span class="ms__t">{{ m.t }}</span>
          <span class="ms__d">{{ m.d }}</span>
        </li>
      </ol>
    </section>

    <section class="sec">
      <h2 class="sec__title">联系我们</h2>
      <div class="contact">
        <p>商务合作、功能建议、问题反馈，都欢迎写信给我们。</p>
        <a class="contact__mail" href="mailto:contact@sozeer.com">contact@sozeer.com</a>
      </div>
    </section>


    <section class="tail">
      <h2 class="tail__title">准备好开始了吗</h2>
      <div class="tail__cta">
        <RouterLink class="btn btn--primary btn--lg" :to="{ name: 'register' }">免费注册</RouterLink>
        <RouterLink class="btn btn--ghost btn--lg" :to="{ name: 'intro' }">了解产品</RouterLink>
      </div>
    </section>
  </div>
</template>

<style scoped>
.hd { padding: var(--sp-6) 0 var(--sp-5); max-width: 700px; }
.hd__eyebrow { color: var(--brand); font-size: var(--fs-xs); letter-spacing: .1em; margin: 0 0 var(--sp-2); }
.hd__title { font-size: clamp(24px, 4vw, 38px); line-height: 1.35; margin: 0 0 var(--sp-3); color: var(--text); }
.hd__sub { color: var(--text-muted); line-height: 1.9; margin: 0; }

.sec { padding: var(--sp-5) 0; }
.sec__title { font-size: var(--fs-lg); margin: 0 0 var(--sp-4); color: var(--text); }

.vals { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--sp-4); }
.val { padding: var(--sp-4); border-radius: var(--radius-3, 12px); background: var(--surface); border: 1px solid var(--border); }
.val__t { margin: 0 0 var(--sp-2); font-size: var(--fs-md); color: var(--brand); }
.val__d { margin: 0; color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.9; }

.mid-ad { margin: var(--sp-5) 0; }

.ms { list-style: none; margin: 0; padding: 0; }
.ms__row { display: grid; grid-template-columns: 120px 1fr; gap: var(--sp-4); padding: var(--sp-3) 0; border-bottom: 1px solid var(--border); }
.ms__t { color: var(--brand); font-size: var(--fs-sm); }
.ms__d { color: var(--text-muted); font-size: var(--fs-sm); }

.contact { color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.9; }
.contact__mail { display: inline-block; margin-top: var(--sp-2); color: var(--brand); font-size: var(--fs-md); text-decoration: none; }
.contact__mail:hover { text-decoration: underline; }

.tail { text-align: center; padding: var(--sp-6) var(--sp-4); border-radius: var(--radius-3, 12px); background: var(--surface-2); margin: var(--sp-5) 0; }
.tail__title { margin: 0 0 var(--sp-4); font-size: var(--fs-xl); color: var(--text); }
.tail__cta { display: flex; gap: var(--sp-3); justify-content: center; flex-wrap: wrap; }

@media (max-width: 840px) { .vals { grid-template-columns: 1fr; } }
@media (max-width: 520px) { .ms__row { grid-template-columns: 1fr; gap: var(--sp-1); } }
</style>
