<script setup lang="ts">
/**
 * PrivacyView · 隐私政策（v1.0-E4.11 · E1 备案审查项）
 *
 * ★ 合规定位：与用户协议配套，注册勾选时一并同意。
 *   口径与平台真实数据流一致：遥测默认开启（仅匿名聚合统计）、数据自托管、密钥加密存储——
 *   不写做不到的承诺（"工程诚实"原则同样适用于法律文本）。
 */
import { onMounted, ref } from 'vue'
import http from '@/api/http'

const siteName = ref('溯字 SOZEER')
const contactEmail = ref('')

interface SiteInfo { site_name?: string; contact_email?: string }
onMounted(async () => {
  try {
    const r = await http.get<SiteInfo>('/api/public/site', undefined, { skipAuth: true, silent: true })
    siteName.value = r.site_name || siteName.value
    contactEmail.value = r.contact_email || ''
  } catch {
    /* 公开页容错 */
  }
})

const SECTIONS = [
  {
    t: '一、我们收集哪些信息',
    items: [
      ['账号信息', '注册时提供的用户名、密码（加密存储）、可选邮箱与手机号。'],
      ['使用数据', '操作日志、登录 IP 与时间（用于安全风控与故障排查）。'],
      ['创作内容', '您创建的书稿、设定、章节、生成的图像/视频/音频等。'],
      ['设备与技术信息', '为保障服务安全所必需的最小化技术日志。'],
    ],
  },
  {
    t: '二、我们如何使用信息',
    items: [
      ['提供服务', '创建与存储您的作品、执行生成任务、计算积分消耗。'],
      ['安全保障', '识别异常登录、注册风控、内容审计（违规内容依法处置）。'],
      ['产品改进', '以**聚合统计**方式分析功能使用情况。遥测上报**默认开启**（仅匿名聚合计数，不含任何作品内容与用户信息）；你可以在「设置 → 数据与隐私 → 遥测」随时关闭，关闭不影响任何功能。'],
    ],
  },
  {
    t: '三、信息的存储与保护',
    items: [
      ['存储位置', '本平台为自托管部署形态：您的创作数据存储在站点运营者配置的服务器数据库中。'],
      ['加密措施', '密码单向哈希存储；第三方 API 密钥加密保存；登录态使用轮换刷新令牌机制。'],
      ['留存期限', '创作内容在您注销账号前持续保留；交易与风控记录依法定期限留存（如会计凭证 5 年）。'],
    ],
  },
  {
    t: '四、信息的共享',
    items: [
      ['基本承诺', '我们不会向第三方出售您的个人信息。'],
      ['必要共享', '仅当您使用依赖第三方的功能时（如云端视频生成 API），平台会把**完成任务所必需**的提示词与参数提交给对应服务商；提交前您已主动触发该功能。'],
      ['法律要求', '依据法律法规或司法机关的强制性要求，可能依法提供必要信息。'],
    ],
  },
  {
    t: '五、您的权利',
    items: [
      ['查询与更正', '您可在个人中心查看并修改昵称、邮箱等资料。'],
      ['导出', '您可随时通过导出功能带走全部作品数据（TXT/Markdown/EPUB/DOCX/PDF）。'],
      ['删除与注销', '您可删除作品与账号；注销后账号不可登录，法律要求留存的记录除外。'],
    ],
  },
  {
    t: '六、未成年人保护',
    items: [
      ['提示', '本平台面向专业创作者，不面向未满 14 周岁的未成年人提供服务。若发现未经监护人同意的未成年人注册，我们将依法处置其账号。'],
    ],
  },
  {
    t: '七、政策更新与联系方式',
    items: [
      ['更新', '本政策如有修订，将通过站内公告方式通知。'],
      ['联系', '如有隐私相关问题，请联系站点管理员（邮箱见页脚或后台 system.contact_email 配置）。'],
    ],
  },
]
</script>

<template>
  <div class="privacy">
    <header class="hd">
      <p class="hd__eyebrow">法律文件</p>
      <h1 class="hd__title">隐私政策</h1>
      <p class="hd__sub">最近更新：2026 年 10 月 · 版本 v1</p>
    </header>

    <article class="doc">
      <p class="doc__intro">
        {{ siteName }}（下称"本平台"）深知个人信息对您的重要性，
        将按照法律法规要求，以最小必要原则处理您的个人信息。
      </p>
      <section v-for="s in SECTIONS" :key="s.t" class="doc__sec">
        <h2>{{ s.t }}</h2>
        <div v-for="(it, i) in s.items" :key="i" class="doc__item">
          <strong>{{ it[0] }}</strong>
          <p>{{ it[1] }}</p>
        </div>
      </section>
    </article>
  </div>
</template>

<style scoped>
.privacy { max-width: 760px; margin: 0 auto; }
.hd { margin-bottom: var(--sp-5); }
.hd__eyebrow { font-size: var(--fs-xs); color: var(--brand); margin-bottom: var(--sp-1); }
.hd__title { font-size: var(--fs-2xl); margin-bottom: var(--sp-2); }
.hd__sub { font-size: var(--fs-sm); color: var(--text-muted); }
.doc {
  background: var(--bg-elevated); border: 1px solid var(--border);
  border-radius: var(--r-lg); padding: var(--sp-6);
}
.doc__intro { color: var(--text-muted); line-height: 1.8; margin-bottom: var(--sp-5); }
.doc__sec { margin-bottom: var(--sp-5); }
.doc__sec h2 { font-size: var(--fs-lg); margin-bottom: var(--sp-2); }
.doc__item { margin-bottom: var(--sp-3); }
.doc__item strong { font-size: var(--fs-sm); color: var(--text); }
.doc__item p { color: var(--text-muted); line-height: 1.9; margin-top: 4px; font-size: var(--fs-sm); }
</style>
