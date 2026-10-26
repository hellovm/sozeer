<script setup lang="ts">
/**
 * TechView · 创作能力页（v1.0-E4.3 重写）
 *
 * ★★ 为什么重写：原版是**给开发者看的技术说明**
 *   （"资产化一致性引擎"、"ZPOPMIN 原子出队"、"权重轮询熔断"…）
 *   —— 这些是我们的**实现手段**，不是客户的**收益**。
 *
 *   客户关心的是：
 *     · 写出来的小说**好不好看**（质量）
 *     · 前后**接不接得上**（一致性 / 伏笔）
 *     · 结构**清不清晰**（架构）
 *     · 出的视频**是不是"一张图动起来"**（成片质量）
 *
 * ★ 判据：**页面讲"你能得到什么"，不讲"我们怎么做到"**。
 *   实现细节留给文档；这里只回答"为什么值得用"。
 *
 * ★ 结构：两大能力（写小说 / 出视频）各讲三个客户能感知的点，
 *   每个点用"客户的语言 + 可验证的证据"表述。
 */
import { RouterLink } from 'vue-router'

/** 能力 ①：AI 写小说 —— 客户能感知的四个价值 */
const NOVEL_POINTS = [
  {
    icon: '◆',
    title: '成稿质量接近人写',
    desc: '不是"能生成"，而是"敢发出去"。同一章可以多轮打磨：'
      + '先出草稿、再逐段重写、最后润色 —— 每一轮都保留你的原意。',
    proof: '支持片段级重写，改一处不动其他部分',
  },
  {
    icon: '⛓',
    title: '前后文紧密关联',
    desc: '写到第 80 章，它还记得第 3 章埋的那句话。人物性格、世界规则、'
      + '时间线全部自动入账，续写时会主动参考，不是"每次从零开始"。',
    proof: '跨章记忆自动检索，长文不失忆',
  },
  {
    icon: '◈',
    title: '伏笔自动回收',
    desc: '埋下的坑会一直记着，到该兑现的时候主动提醒你；'
      + '如果一条伏笔拖太久没回收，系统会标出"债务"，避免烂尾。',
    proof: '伏笔台账 + 逾期提醒，烂尾风险可视化',
  },
  {
    icon: '▦',
    title: '架构清晰可控',
    desc: '总纲 → 分卷 → 章节 → 场景，四级结构一眼看清。'
      + '故事主线、支线、人物弧光都有地方放，不会写着写着失控。',
    proof: '三级大纲 + 情绪曲线 + 关系图谱',
  },
]

/** 能力 ②：AI 出视频 —— 强调"导演视角"而非"图生视频" */
const VIDEO_POINTS = [
  {
    step: '01',
    title: '读懂剧本',
    desc: '先把小说拆成分集、剧本、场次 —— 理解"谁在什么地方、'
      + '对谁做了什么、情绪是什么"，而不是按段落随机切图。',
  },
  {
    step: '02',
    title: '专业分镜',
    desc: '按导演逻辑拆分镜：景别（远/中/近/特）、机位、运镜、'
      + '光线氛围都写进提示词。该给特写的地方给特写，该拉远景的地方拉远景。',
  },
  {
    step: '03',
    title: '角色不换脸',
    desc: '同一个角色跨镜、跨集、跨场景保持同一张脸、同一套服装。'
      + '上一镜的结尾帧自动成为下一镜的开头，画面连贯不断裂。',
  },
  {
    step: '04',
    title: '成片可交付',
    desc: '按镜头类型自动匹配模型（动作镜头 / 静态对话 / 空镜各用所长），'
      + '音画对齐、字幕配音齐备，导出即为可发布的成片。',
  },
]

/** 对比：普通"图生视频" vs 溯字的做法 —— 让客户一眼看出差别 */
const CONTRAST = [
  { dim: '理解方式', plain: '按段落机械切图', ours: '先读懂剧本再分镜' },
  { dim: '镜头语言', plain: '固定景别、固定机位', ours: '景别/机位/运镜专业设计' },
  { dim: '角色一致性', plain: '每一镜都是新面孔', ours: '跨镜跨集锁定同一角色' },
  { dim: '画面连贯', plain: '镜头之间生硬跳切', ours: '末帧传递，前后衔接' },
  { dim: '产出结果', plain: '一堆零散片段', ours: '可发布的完整成片' },
]
</script>

<template>
  <div class="cap">
    <!-- ═══════════ Hero ═══════════ -->
    <header class="hero">
      <p class="hero__eyebrow">创作能力</p>
      <h1 class="hero__title">
        不只是"生成"，<br>
        <span class="hero__hl">是能交付的作品</span>
      </h1>
      <p class="hero__sub">
        从一句灵感，到一部能发布的小说与短剧 ——
        中间所有"需要人来盯"的环节，我们都做进了流程里。
      </p>
      <div class="hero__cta">
        <RouterLink class="btn btn--primary btn--lg" :to="{ name: 'register' }">
          免费开始创作
        </RouterLink>
        <RouterLink class="btn btn--ghost btn--lg" :to="{ name: 'pricing' }">
          查看价格
        </RouterLink>
      </div>
    </header>

    <!-- ═══════════ ① 写小说 ═══════════ -->
    <section class="sec">
      <div class="sec__hd">
        <span class="sec__num">01</span>
        <div>
          <h2 class="sec__title">AI 写小说</h2>
          <p class="sec__sub">长篇连载最难的不是开头，是第 50 章还站得住。</p>
        </div>
      </div>

      <div class="pgrid">
        <article v-for="p in NOVEL_POINTS" :key="p.title" class="pcard">
          <span class="pcard__ico" aria-hidden="true">{{ p.icon }}</span>
          <h3 class="pcard__title">{{ p.title }}</h3>
          <p class="pcard__desc">{{ p.desc }}</p>
          <p class="pcard__proof">
            <span class="pcard__check" aria-hidden="true">✓</span>{{ p.proof }}
          </p>
        </article>
      </div>
    </section>


    <!-- ═══════════ ② 出视频 ═══════════ -->
    <section class="sec">
      <div class="sec__hd">
        <span class="sec__num">02</span>
        <div>
          <h2 class="sec__title">AI 出短剧</h2>
          <p class="sec__sub">
            不是"一张图动起来"，而是<strong>以导演的视角把故事讲出来</strong>。
          </p>
        </div>
      </div>

      <ol class="flow">
        <li v-for="v in VIDEO_POINTS" :key="v.step" class="flow__item">
          <span class="flow__step">{{ v.step }}</span>
          <div class="flow__body">
            <h3 class="flow__title">{{ v.title }}</h3>
            <p class="flow__desc">{{ v.desc }}</p>
          </div>
        </li>
      </ol>

      <!-- 对比表：一眼看出"图生视频"与"成片级"的差别 -->
      <div class="cmp">
        <div class="cmp__hd">
          <h3 class="cmp__title">为什么不一样</h3>
          <p class="cmp__sub">同样是"AI 生成视频"，出来的东西可以是两回事。</p>
        </div>
        <div class="cmp__table">
          <div class="cmp__row cmp__row--head">
            <span></span>
            <span class="cmp__plain-h">普通图生视频</span>
            <span class="cmp__ours-h">溯字 SOZEER</span>
          </div>
          <div v-for="c in CONTRAST" :key="c.dim" class="cmp__row">
            <span class="cmp__dim">{{ c.dim }}</span>
            <span class="cmp__plain">{{ c.plain }}</span>
            <span class="cmp__ours">{{ c.ours }}</span>
          </div>
        </div>
      </div>
    </section>

    <!-- ═══════════ 尾 CTA ═══════════ -->
    <section class="tail">
      <h2 class="tail__title">现在开始你的第一部作品</h2>
      <p class="tail__sub">注册即送体验积分 · 无需信用卡 · 作品版权完全归你</p>
      <div class="tail__cta">
        <RouterLink class="btn btn--primary btn--lg" :to="{ name: 'register' }">
          免费开始
        </RouterLink>
        <RouterLink class="btn btn--ghost btn--lg" :to="{ name: 'intro' }">
          返回介绍
        </RouterLink>
      </div>
    </section>

  </div>
</template>

<style scoped>
.cap { padding-bottom: var(--sp-6); }

/* ─────────── Hero ─────────── */
.hero {
  position: relative;
  padding: var(--sp-10) 0 var(--sp-8);
  max-width: 760px;
}
/* ★ 装饰性光晕：让首屏有"设计感"而非纯文字堆砌 */
.hero::before {
  content: '';
  position: absolute; top: -40px; left: -120px;
  width: 420px; height: 420px; pointer-events: none;
  background: radial-gradient(circle, rgba(43, 108, 176, .10), transparent 68%);
  filter: blur(8px);
}
.hero__eyebrow {
  position: relative;
  display: inline-flex; align-items: center; gap: 8px;
  padding: 5px 14px; margin: 0 0 var(--sp-4);
  border: 1px solid var(--brand-200); border-radius: var(--r-full);
  background: var(--brand-50); color: var(--brand-600);
  font-size: var(--fs-xs); letter-spacing: .06em;
}
.hero__eyebrow::before {
  content: ''; width: 6px; height: 6px; border-radius: 50%;
  background: var(--brand-500);
}
.hero__title {
  position: relative;
  font-size: clamp(28px, 5vw, 46px); line-height: 1.24;
  margin: 0 0 var(--sp-4); color: var(--text); letter-spacing: -.01em;
}
.hero__hl {
  background: linear-gradient(100deg, var(--brand-500), var(--brand-700) 60%, var(--accent));
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent; color: var(--brand-600);
}
.hero__sub {
  position: relative;
  color: var(--text-muted); line-height: 1.95; margin: 0 0 var(--sp-5);
  font-size: var(--fs-base); max-width: 620px;
}
.hero__cta { position: relative; display: flex; gap: var(--sp-3); flex-wrap: wrap; }

/* ─────────── 区块 ─────────── */
.sec { padding: var(--sp-8) 0; }
.sec__hd {
  display: flex; align-items: flex-start; gap: var(--sp-4);
  margin-bottom: var(--sp-6);
}
.sec__num {
  flex: none; font-family: var(--font-mono);
  font-size: var(--fs-sm); color: var(--brand);
  padding-top: 4px; letter-spacing: .08em;
}
.sec__title { margin: 0 0 var(--sp-2); font-size: clamp(22px, 3vw, 30px); color: var(--text); }
.sec__sub { margin: 0; color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.9; }
.sec__sub strong { color: var(--text); }

.ad { margin: var(--sp-5) 0; }

/* ─────────── 小说：四宫格 ─────────── */
.pgrid { display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--sp-4); }
.pcard {
  position: relative; overflow: hidden;
  padding: var(--sp-5);
  border: 1px solid var(--border); border-radius: 16px;
  background: var(--bg-elevated);
  transition: transform var(--dur-base) var(--ease),
              box-shadow var(--dur-base) var(--ease),
              border-color var(--dur-base) var(--ease);
}
.pcard:hover {
  transform: translateY(-3px);
  border-color: var(--brand-200);
  box-shadow: 0 16px 40px -22px rgba(43, 108, 176, .45);
}
/* ★ 顶部渐变细线：低成本的"精致感" */
.pcard::after {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, var(--brand-500), transparent 70%);
  opacity: 0; transition: opacity var(--dur-base) var(--ease);
}
.pcard:hover::after { opacity: 1; }
.pcard__ico {
  display: grid; place-items: center;
  width: 38px; height: 38px; margin-bottom: var(--sp-3);
  border-radius: 10px;
  background: var(--brand-50); color: var(--brand-600);
  font-size: 17px;
}
.pcard__title { margin: 0 0 var(--sp-2); font-size: var(--fs-md); color: var(--text); }
.pcard__desc {
  margin: 0 0 var(--sp-3); color: var(--text-muted);
  font-size: var(--fs-sm); line-height: 1.95;
}
.pcard__proof {
  display: flex; align-items: flex-start; gap: 7px;
  margin: 0; padding-top: var(--sp-3);
  border-top: 1px solid var(--border);
  color: var(--text-subtle); font-size: var(--fs-xs); line-height: 1.7;
}
.pcard__check { color: var(--success); font-weight: 700; flex: none; }

/* ─────────── 视频：四步流程 ─────────── */
.flow {
  list-style: none; margin: 0 0 var(--sp-6); padding: 0;
  display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--sp-4);
  counter-reset: step;
}
.flow__item {
  position: relative;
  display: flex; flex-direction: column; gap: var(--sp-3);
  padding: var(--sp-5) var(--sp-4);
  border: 1px solid var(--border); border-radius: 16px;
  background: linear-gradient(180deg, var(--bg-elevated), var(--bg-sunken));
}
/* ★ 连接线：表达"这是一条流水线"而非四个孤立步骤 */
.flow__item:not(:last-child)::after {
  content: ''; position: absolute; top: 42px; right: -10px;
  width: 18px; height: 1px;
  background: linear-gradient(90deg, var(--brand-200), transparent);
}
.flow__step {
  font-family: var(--font-mono); font-size: var(--fs-xs);
  color: var(--brand); letter-spacing: .1em;
}
.flow__body { display: flex; flex-direction: column; gap: 6px; }
.flow__title { margin: 0; font-size: var(--fs-sm); color: var(--text); font-weight: 600; }
.flow__desc { margin: 0; color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.9; }

/* ─────────── 对比表 ─────────── */
.cmp {
  padding: var(--sp-5);
  border: 1px solid var(--border); border-radius: 18px;
  background: var(--bg-elevated);
}
.cmp__hd { margin-bottom: var(--sp-4); }
.cmp__title { margin: 0 0 6px; font-size: var(--fs-lg); color: var(--text); }
.cmp__sub { margin: 0; color: var(--text-muted); font-size: var(--fs-sm); }
.cmp__table { display: flex; flex-direction: column; }
.cmp__row {
  display: grid; grid-template-columns: 1.1fr 1.4fr 1.4fr;
  gap: var(--sp-3); align-items: center;
  padding: var(--sp-3) var(--sp-2);
  border-top: 1px solid var(--border);
  font-size: var(--fs-sm);
}
.cmp__row--head { border-top: 0; padding-top: 0; font-size: var(--fs-xs); }
.cmp__dim { color: var(--text-subtle); font-size: var(--fs-xs); }
.cmp__plain-h, .cmp__ours-h {
  padding: 5px 10px; border-radius: var(--r-md);
  font-size: var(--fs-xs); text-align: center; font-weight: 600;
}
.cmp__plain-h { color: var(--text-subtle); background: var(--bg-sunken); }
.cmp__ours-h { color: #fff; background: var(--brand-500); }
.cmp__plain { color: var(--text-subtle); }
.cmp__plain::before { content: '· '; color: var(--gray-300); }
.cmp__ours { color: var(--text); font-weight: 500; }
.cmp__ours::before { content: '✓ '; color: var(--success); font-weight: 700; }

/* ─────────── 尾 CTA ─────────── */
.tail {
  position: relative; overflow: hidden;
  text-align: center; padding: var(--sp-8) var(--sp-4);
  border-radius: 20px; margin: var(--sp-6) 0;
  background: linear-gradient(160deg, var(--brand-50), var(--bg-sunken));
  border: 1px solid var(--brand-100);
}
.tail__title { margin: 0 0 var(--sp-2); font-size: clamp(20px, 3vw, 28px); color: var(--text); }
.tail__sub { margin: 0 0 var(--sp-5); color: var(--text-muted); font-size: var(--fs-sm); }
.tail__cta { display: flex; gap: var(--sp-3); justify-content: center; flex-wrap: wrap; }

/* ─────────── 响应式 ─────────── */
@media (max-width: 1000px) {
  .flow { grid-template-columns: repeat(2, 1fr); }
  .flow__item:not(:last-child)::after { display: none; }
}
@media (max-width: 760px) {
  .pgrid { grid-template-columns: 1fr; }
  .cmp__row { grid-template-columns: 1fr 1fr; }
  .cmp__dim { grid-column: 1 / -1; margin-bottom: 4px; }
}
@media (max-width: 560px) {
  .flow { grid-template-columns: 1fr; }
  .hero { padding: var(--sp-6) 0 var(--sp-5); }
  .hero::before { display: none; }
}
</style>
