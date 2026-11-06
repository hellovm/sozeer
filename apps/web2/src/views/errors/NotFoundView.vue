<script setup lang="ts">
/** NotFoundView · 404（v0.26.1） */
import { useRouter } from 'vue-router'
import AppButton from '@/components/AppButton.vue'

const router = useRouter()
</script>

<template>
  <div class="nf">
    <div class="nf__box">
      <div class="nf__code">404</div>
      <h1 class="nf__title">页面不存在</h1>
      <p class="nf__sub">
        地址可能拼错了，或者这个页面已经被移走。
      </p>
      <div class="nf__actions">
        <AppButton variant="primary" @click="router.push({ name: 'intro' })">
          回到首页
        </AppButton>
        <AppButton variant="ghost" @click="router.back()">返回上一页</AppButton>
        <AppButton variant="ghost" @click="router.push({ name: 'help' })">
          帮助中心
        </AppButton>
      </div>
    </div>
  </div>
</template>

<style scoped>
.nf { min-height: 60vh; display: grid; place-items: center; padding: var(--sp-6); }
.nf__box { text-align: center; max-width: 440px; }
.nf__code { font-size: 72px; font-weight: 700; line-height: 1;
  color: var(--brand); opacity: .18; letter-spacing: -.04em; }
.nf__title { font-size: var(--fs-xl); margin: var(--sp-3) 0 var(--sp-2); }
.nf__sub { color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.8;
  margin: 0 0 var(--sp-5); }
.nf__actions { display: flex; gap: var(--sp-2); justify-content: center; flex-wrap: wrap; }
</style>
