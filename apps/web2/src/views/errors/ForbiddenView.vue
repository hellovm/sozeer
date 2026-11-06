<script setup lang="ts">
/**
 * ForbiddenView.vue · 403 无权访问（v0.26.2 G0-6）
 *
 * ★★ 为什么和 404 分开做：
 *   404 = "这东西不存在"（可能地址错了）
 *   403 = "存在，但你没权限"（需要找管理员开权限）
 *   两者的**下一步动作完全不同** —— 混成一个页会让用户白折腾。
 *
 * ★ 必须显示"缺什么权限"，否则用户去找管理员时也说不清。
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'

const route = useRoute()
const router = useRouter()

/** 缺的权限点（由路由守卫带过来） */
const need = computed(() => String(route.query.need || '').split(',').filter(Boolean))
const from = computed(() => String(route.query.from || ''))
</script>

<template>
  <div class="fb">
    <AppCard>
      <div class="fb__box">
        <div class="fb__code">403</div>
        <h1 class="fb__title">无权访问</h1>
        <p class="fb__desc">
          这个页面存在，但当前账号没有访问它的权限。
        </p>

        <div v-if="need.length" class="fb__need">
          <div class="fb__need-t">需要以下权限点：</div>
          <ul class="fb__need-l">
            <li v-for="p in need" :key="p"><code>{{ p }}</code></li>
          </ul>
          <p class="fb__need-hint">
            请把上面的权限点发给所有者（owner），在「权限管理」里为你的角色开启。
          </p>
        </div>

        <div v-if="from" class="fb__from">
          尝试访问：<code>{{ from }}</code>
        </div>

        <div class="fb__actions">
          <AppButton variant="primary" @click="router.back()">返回上一页</AppButton>
          <AppButton variant="ghost" @click="router.push({ name: 'admin-dashboard' })">
            回到看板
          </AppButton>
          <AppButton variant="ghost" @click="router.push({ name: 'intro' })">
            站点首页
          </AppButton>
        </div>
      </div>
    </AppCard>
  </div>
</template>

<style scoped>
.fb { display: grid; place-items: center; min-height: 70vh; padding: 20px; }
.fb__box { text-align: center; max-width: 480px; margin: 0 auto; }
.fb__code {
  font-size: 62px;
  font-weight: 700;
  line-height: 1;
  color: var(--warn, #b8860b);
  opacity: .22;
  letter-spacing: -.04em;
}
.fb__title { font-size: 19px; margin: 12px 0 8px; }
.fb__desc { font-size: 13px; color: var(--text-muted); line-height: 1.85; margin: 0 0 20px; }

.fb__need {
  text-align: left;
  padding: 13px 15px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--surface-2, var(--surface));
  margin-bottom: 16px;
}
.fb__need-t { font-size: 12px; color: var(--text-muted); margin-bottom: 7px; }
.fb__need-l { margin: 0 0 8px; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 5px; }
.fb__need-l code {
  font-size: 12px;
  padding: 3px 8px;
  border-radius: 5px;
  background: var(--surface);
  border: 1px solid var(--border);
  display: inline-block;
}
.fb__need-hint { font-size: 11px; color: var(--text-faint); line-height: 1.75; margin: 0; }

.fb__from {
  font-size: 11px;
  color: var(--text-faint);
  margin-bottom: 20px;
  word-break: break-all;
}
.fb__from code {
  padding: 2px 6px;
  border-radius: 4px;
  border: 1px solid var(--border);
}
.fb__actions { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }
</style>
