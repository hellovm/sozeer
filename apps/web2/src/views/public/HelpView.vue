<script setup lang="ts">
/**
 * HelpView · 帮助中心（v0.26.1）
 *
 * ★ 纯静态内容页（无需接口）—— 但必须能搜到、能跳转。
 * ★ 内容组织原则：按"用户卡在哪"分，而不是按功能模块分。
 */
import { computed, ref } from 'vue'
import AppCard from '@/components/AppCard.vue'
import AppInput from '@/components/AppInput.vue'
import AppEmpty from '@/components/AppEmpty.vue'

const q = ref('')

const FAQ = [
  {
    cat: '入门',
    items: [
      { q: '怎么开始写第一本书？', a: '进入「写书」→ 新建作品 → 填写题材与目标字数。'
        + '系统会生成总纲与前三章大纲，之后在写作台用对话驱动 AI 代笔。' },
      { q: 'AI 代笔的内容会直接进正文吗？', a: '不会。生成的是**草稿**，'
        + '需要你点「采纳」才会写入正文。采纳前可以重写或丢弃。' },
      { q: '质检报告里的问题必须全改吗？', a: '不必须。质检给的是**可执行建议**，'
        + '不是硬性要求。但境界越界、伏笔超期这类问题建议优先处理，'
        + '否则后期会越写越难圆。' },
    ],
  },
  {
    cat: '短剧',
    items: [
      { q: '短剧是怎么从小说生成的？', a: '四步：① 分集规划 → ② 生成剧本 → '
        + '③ 生成分镜提示词（八段结构）→ ④ 批量出图/出视频。'
        + '每一步依赖上一步的产物，建议按序执行。' },
      { q: '为什么角色在不同镜头里长得不一样？', a: '最常见原因是**提示词里重复描述了五官**，'
        + '与参考图形成"双信号打架"。正确做法：角色外貌交给参考图，'
        + '提示词只写动作、镜头、光线。' },
      { q: '怎么保证角色跨集不变脸？', a: '① 建立角色三视图（正面/侧面/特写）；'
        + '② 每个镜头引用同一份资产；③ 优先用「图生视频」而非纯文生视频 —— '
        + '身份由首帧锁定最稳。' },
      { q: '配音怎么做到跨集音色一致？', a: '用**参考音频**而不是音色名。'
        + '在「音色」标签为每个角色绑定一段干净人声（10~60 秒无 BGM），'
        + '后续所有集复用同一份配置。' },
    ],
  },
  {
    cat: '模型与费用',
    items: [
      { q: '积分是怎么消耗的？', a: '按实际调用的模型与用量扣除。'
        + '写正文、生成图片、生成视频单价不同，明细在「用量与账单」里。' },
      { q: '可以接入自己的 ComfyUI 吗？', a: '可以。管理员在后台「模型配置」'
        + '选 ComfyUI 模板（LTX-2.5 / MiniMax H3），填地址与 token 即可。' },
      { q: '模型提示词要用中文还是英文？', a: '取决于模型：MiniMax H3 原生支持中文，'
        + '可直接中文写（对白尤其）；LTX-2.5 的文本编码器主要针对英文优化，'
        + '建议改用英文提示词。' },
    ],
  },
  {
    cat: '账号与数据',
    items: [
      { q: '我的作品版权归谁？', a: '完全归你。平台不对你的创作内容主张任何权利。' },
      { q: '怎么导出我的数据？', a: '「安全设置」→「导出我的数据」，'
        + '会打包下载作品、章节与账号信息。' },
      { q: '怀疑账号被盗怎么办？', a: '立即去「安全设置」：① 修改密码（旧令牌会立刻失效）'
        + '② 查看登录设备并下线可疑会话 ③ 建议开启二次验证。' },
    ],
  },
]

const SHORTCUTS = [
  { k: '⌘/Ctrl + Enter', d: '发送对话消息' },
  { k: '⌘/Ctrl + K', d: '打开命令面板' },
  { k: '⌘/Ctrl + S', d: '保存当前编辑内容' },
  { k: 'Esc', d: '关闭弹窗 / 取消当前操作' },
]

const filtered = computed(() => {
  const k = q.value.trim().toLowerCase()
  if (!k) return FAQ
  return FAQ.map(g => ({
    ...g,
    items: g.items.filter(i =>
      i.q.toLowerCase().includes(k) || i.a.toLowerCase().includes(k)),
  })).filter(g => g.items.length)
})

/**
 * ★ v1.0-E4：分类锚点导航。
 *
 * ★ 为什么需要：FAQ 会长到十几屏，没有导航用户只能盲滚。
 *   ★ 为什么用锚点而不是分页/子路由：
 *     ① 帮助内容是**一次性浏览**场景，分页会打断"顺带看到别的答案"的偶然学习
 *     ② 锚点对 SEO 友好（内容在同一页，权重集中）
 *     ③ 零路由改动，风险最低
 */
function scrollTo(cat: string) {
  try {
    const el = document.getElementById('faq-' + encodeURIComponent(cat))
    el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  } catch {
    /* 滚不动也不影响阅读 */
  }
}

/** ★ 搜索无结果时的引导：直接带着关键词去反馈（闭环） */
const feedbackHref = computed(() => {
  const k = q.value.trim()
  return k ? `/app/help?ask=${encodeURIComponent(k)}` : ''
})
</script>

<template>
  <div class="page">
    <header class="page__head">
      <h1 class="page__title">帮助中心</h1>
      <p class="page__sub">常见问题 · 快捷键 · 联系支持</p>
      <div class="search">
        <AppInput v-model="q" placeholder="搜索问题…（如：一致性、积分、导出）" clearable />
      </div>
      <!-- ★ v1.0-E4：分类锚点导航（长内容必需，否则只能盲滚） -->
      <nav v-if="!q" class="cats" aria-label="问题分类">
        <button
          v-for="g in FAQ" :key="g.cat" type="button"
          class="cats__item" @click="scrollTo(g.cat)"
        >
          {{ g.cat }}
          <span class="cats__n">{{ g.items.length }}</span>
        </button>
      </nav>
    </header>

    <AppEmpty v-if="!filtered.length" icon="🔍"
              :title="`没有找到与「${q}」相关的内容`"
              :description="feedbackHref
                ? '换个关键词，或直接把问题反馈给我们，会在 1 个工作日内回复。'
                : '换个关键词试试，或直接联系我们。'">
      <template v-if="feedbackHref" #action>
        <a class="btn btn--primary" :href="feedbackHref">去反馈这个问题</a>
      </template>
    </AppEmpty>

    <AppCard v-for="g in filtered" :key="g.cat" :title="g.cat" class="mt">
      <template #default>
        <div :id="'faq-' + encodeURIComponent(g.cat)" class="anchor" />
        <details v-for="(i, idx) in g.items" :key="i.q" class="faq" :open="idx === 0">
          <summary>{{ i.q }}</summary>
          <p>{{ i.a }}</p>
        </details>
      </template>
    </AppCard>

    <AppCard title="键盘快捷键" class="mt">
      <dl class="keys">
        <div v-for="s in SHORTCUTS" :key="s.k">
          <dt><kbd>{{ s.k }}</kbd></dt>
          <dd>{{ s.d }}</dd>
        </div>
      </dl>
    </AppCard>

    <AppCard title="还需要帮助？" class="mt">
      <p class="muted">
        没找到答案？写信给我们，会在 1 个工作日内回复。
      </p>
      <a class="mail" href="mailto:support@sozeer.com">support@sozeer.com</a>
    </AppCard>
  </div>
</template>

<style scoped>
.page { padding: var(--sp-5); max-width: 780px; margin: 0 auto; }
.page__head { margin-bottom: var(--sp-4); }
.page__title { font-size: var(--fs-xl); margin-bottom: var(--sp-1); }
.page__sub { font-size: var(--fs-sm); color: var(--text-muted); }
.search { margin-top: var(--sp-4); max-width: 420px; }

/* ★ v1.0-E4：分类锚点导航 */
.cats {
  display: flex; flex-wrap: wrap; gap: var(--sp-2);
  margin-top: var(--sp-3);
}
.cats__item {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 12px; cursor: pointer;
  border: 1px solid var(--border); border-radius: 999px;
  background: var(--surface); color: var(--text-muted);
  font-size: var(--fs-xs); transition: border-color .15s, color .15s;
}
.cats__item:hover { border-color: var(--brand); color: var(--brand); }
.cats__n {
  padding: 0 5px; border-radius: 999px;
  background: var(--surface-2); color: var(--text-faint);
  font-size: 10px;
}
/* 锚点偏移：避免跳转后被固定头部遮住标题 */
.anchor { position: relative; top: -68px; display: block; height: 0; }

.mt { margin-top: var(--sp-4); }
.faq { padding: var(--sp-3) 0; border-bottom: 1px solid var(--border); }
.faq:last-child { border-bottom: none; }
.faq summary { cursor: pointer; font-size: var(--fs-sm); color: var(--text);
  list-style: none; display: flex; align-items: center; gap: 8px; }
.faq summary::before { content: '▸'; color: var(--brand); transition: transform .15s; }
.faq[open] summary::before { transform: rotate(90deg); }
.faq p { margin: var(--sp-2) 0 0 20px; color: var(--text-muted);
  font-size: var(--fs-sm); line-height: 1.9; }
.keys { display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--sp-3); margin: 0; }
.keys > div { display: flex; align-items: center; gap: var(--sp-3); }
.keys dt { margin: 0; }
.keys dd { margin: 0; font-size: var(--fs-sm); color: var(--text-muted); }
kbd { display: inline-block; padding: 3px 8px; border: 1px solid var(--border);
  border-bottom-width: 2px; border-radius: 5px; background: var(--surface-2);
  font-family: var(--font-mono, monospace); font-size: var(--fs-xs); }
.mail { display: inline-block; margin-top: var(--sp-2); color: var(--brand);
  font-size: var(--fs-md); text-decoration: none; }
.mail:hover { text-decoration: underline; }
@media (max-width: 560px) { .keys { grid-template-columns: 1fr; } }
</style>
