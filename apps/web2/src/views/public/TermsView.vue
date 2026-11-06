<script setup lang="ts">
/**
 * TermsView · 用户协议（v1.0-E4.11 · E1 备案审查项）
 *
 * ★ 合规定位：注册时必须同意本文档（RegisterView 勾选框 + 后端 agreed_terms_at）。
 *   条文按"平台是 BSL 源码开放的自托管工具"口径撰写，不虚构主体资质；
 *   主体名称/联系方式从站点配置读，留空时显示占位符。
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
    /* 公开页容错：配置拿不到就用默认文案 */
  }
})

const SECTIONS = [
  {
    t: '一、协议的接受与修改',
    ps: [
      '欢迎使用本平台。请您在使用本平台前仔细阅读本协议。您注册账号、登录、使用本平台服务的，即视为已阅读并同意本协议的全部内容。',
      '我们可能根据法律法规变化或产品更新对本协议进行修订，修订后的协议将通过站内公告等方式公布。若您在协议修订后继续使用本平台，视为接受修订后的协议。',
    ],
  },
  {
    t: '二、账号与安全',
    ps: [
      '您承诺以真实身份注册账号，并妥善保管账号密码。因您主动泄露密码或遭受他人攻击导致的损失，由您自行承担。',
      '首次注册的账号在系统无既有用户时将自动获得管理员权限（自托管部署特性），请知悉并妥善使用。',
      '您可随时注销账号。注销后账号不可登录，但依据法律法规要求，相关交易记录将依法留存。',
    ],
  },
  {
    t: '三、用户内容与权利归属',
    ps: [
      '您在本平台生成的小说文本、分镜、图像、视频等作品内容的权利归属，以您与内容生成服务条款约定为准。您承诺对提交给本平台的原始素材拥有合法权利。',
      '您不得利用本平台生成、传播违反法律法规、危害国家安全、侵犯他人合法权益的内容。平台有权依法对违规内容采取隐藏、删除等措施。',
      '本平台对您上传的内容不做长期备份承诺，请通过导出功能（TXT/Markdown/EPUB/DOCX/PDF）自行保存作品。',
    ],
  },
  {
    t: '四、AI 生成内容的说明',
    ps: [
      '本平台部分功能基于人工智能模型生成内容。AI 生成内容可能存在虚构、错误或不准确之处，请您自行判断并核实后再使用。',
      '依照《生成式人工智能服务管理暂行办法》等规定，AI 生成内容不得用于伪造事实、误导公众等用途。平台按国家相关规定完成算法备案与安全评估后方可对外提供服务。',
    ],
  },
  {
    t: '五、付费服务与积分',
    ps: [
      '本平台部分功能按积分计费。积分规则（赠送、消耗、有效期）以定价页公示为准。',
      '已消耗的积分除法律法规另有规定外，一般不予退还；因平台原因导致服务不可用的部分，我们将按规则补偿。',
    ],
  },
  {
    t: '六、免责声明',
    ps: [
      '因不可抗力、网络故障、第三方服务（如云端视频 API、大模型供应商）中断等原因导致服务暂时不可用的，平台不承担由此产生的间接损失。',
      '本平台以"现状"提供服务，在法律允许的最大范围内不对内容的持续性、准确性作出担保。',
    ],
  },
  {
    t: '七、法律适用与争议解决',
    ps: [
      '本协议的订立、执行与解释适用中华人民共和国法律。',
      '因本协议产生的争议，双方应友好协商解决；协商不成的，可向平台运营者所在地有管辖权的人民法院提起诉讼。',
    ],
  },
]
</script>

<template>
  <div class="terms">
    <header class="hd">
      <p class="hd__eyebrow">法律文件</p>
      <h1 class="hd__title">用户协议</h1>
      <p class="hd__sub">最近更新：2026 年 10 月 · 版本 v1</p>
    </header>

    <article class="doc">
      <p class="doc__intro">
        本协议是您与 {{ siteName }} 运营者之间关于使用本平台服务的约定。
        阅读本协议的过程中，如您不同意任何条款，请停止注册或使用本平台。
        如有疑问请联系：{{ contactEmail || '（站点管理员邮箱，可在后台配置 system.contact_email）' }}
      </p>
      <section v-for="s in SECTIONS" :key="s.t" class="doc__sec">
        <h2>{{ s.t }}</h2>
        <p v-for="(p, i) in s.ps" :key="i">{{ p }}</p>
      </section>
    </article>
  </div>
</template>

<style scoped>
.terms { max-width: 760px; margin: 0 auto; }
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
.doc__sec p { color: var(--text-muted); line-height: 1.9; margin-bottom: var(--sp-2); font-size: var(--fs-sm); }
</style>
