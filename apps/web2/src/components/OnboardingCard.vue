<script setup lang="ts">
/**
 * OnboardingCard · 新手引导卡片（v1.0-E4 运营准备）
 *
 * ★★ 设计立场（见 apps/api/onboarding.py 模块头）：
 *   · **不打断** —— 不弹窗、不遮罩、不阻断操作，就是工作台顶部一张卡
 *   · **可关闭** —— 右上角 × 一键隐藏，且提供"重新显示"入口
 *   · **基于事实** —— 进度来自真实数据（有没有建过书），不是"点没点过按钮"
 *     → 用户跳过引导自己摸会了，这里也会自动打勾
 *   · **完成即消失** —— 4 步全完成后卡片自动隐藏（不给已上手的人添堵）
 *
 * ★ 为什么用"折叠"而不是"全展开"：
 *   展开的 4 条清单会占掉半个屏幕，老用户会烦。
 *   默认收起成一行进度条，想看细节再展开。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import AppCard from '@/components/AppCard.vue'
import AppButton from '@/components/AppButton.vue'
import meApi, { type OnboardingState } from '@/api/modules/me'

const router = useRouter()

const state = ref<OnboardingState | null>(null)
const expanded = ref(false)
const busy = ref(false)

/** ★ 只在"该显示"时渲染（未上线/已完成/已隐藏都不显示） */
const visible = computed(() => Boolean(state.value?.show))
const pct = computed(() => state.value?.percent ?? 0)

onMounted(async () => {
  try {
    state.value = await meApi.onboarding()
    // ★ 未完成时默认展开（新用户需要看到）；进度过半则收起（别挡路）
    expanded.value = (state.value?.done_count ?? 0) === 0
  } catch {
    state.value = null            // ★ fail-soft：引导拉不到就不显示
  }
})

async function onHide() {
  busy.value = true
  try {
    await meApi.hideOnboarding()
    if (state.value) state.value = { ...state.value, show: false, hidden: true }
  } catch {
    /* 失败也不报错：下次进来自会再次显示 */
  } finally {
    busy.value = false
  }
}

function go(link: string) {
  router.push(link)
}
</script>

<template>
  <AppCard v-if="visible" padding="md" class="ob">
    <div class="ob__hd">
      <div class="ob__title">
        <b>开始使用</b>
        <span class="ob__count">{{ state?.done_count }} / {{ state?.total }}</span>
      </div>
      <div class="ob__acts">
        <button class="ob__link" type="button" @click="expanded = !expanded">
          {{ expanded ? '收起' : '展开' }}
        </button>
        <AppButton variant="ghost" size="sm" :disabled="busy" @click="onHide">
          不再显示
        </AppButton>
      </div>
    </div>

    <!-- ★ 进度条：一眼看到"还差几步" -->
    <div class="ob__bar" :aria-valuenow="pct" role="progressbar"
         aria-valuemin="0" aria-valuemax="100">
      <i :style="{ width: pct + '%' }" />
    </div>

    <p v-if="!expanded" class="ob__hint">
      还差 {{ (state?.total ?? 0) - (state?.done_count ?? 0) }} 步就完成上手了
    </p>

    <ol v-else class="ob__list">
      <li v-for="s in state?.steps" :key="s.key" :class="{ 'is-done': s.done }">
        <span class="ob__mark" aria-hidden="true">{{ s.done ? '✓' : '○' }}</span>
        <div class="ob__body">
          <div class="ob__name">{{ s.title }}</div>
          <div class="ob__desc">{{ s.desc }}</div>
          <div v-if="s.hint && !s.done" class="ob__tip">{{ s.hint }}</div>
        </div>
        <AppButton
          v-if="!s.done"
          variant="ghost" size="sm"
          @click="go(s.link)"
        >
          {{ s.action }}
        </AppButton>
      </li>
    </ol>
  </AppCard>
</template>

<style scoped>
.ob { margin-bottom: var(--sp-4); }
.ob__hd { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3); }
.ob__title { display: flex; align-items: baseline; gap: var(--sp-2); }
.ob__title b { color: var(--text); font-size: var(--fs-md); }
.ob__count { color: var(--text-faint); font-size: var(--fs-xs); }
.ob__acts { display: flex; align-items: center; gap: var(--sp-2); }
.ob__link {
  background: none; border: 0; cursor: pointer; padding: 0 4px;
  color: var(--brand); font-size: var(--fs-xs);
}
.ob__bar {
  margin: var(--sp-3) 0 0; height: 4px; border-radius: 999px;
  background: var(--border); overflow: hidden;
}
.ob__bar i { display: block; height: 100%; background: var(--brand); transition: width .3s; }
.ob__hint { margin: var(--sp-2) 0 0; color: var(--text-muted); font-size: var(--fs-xs); }

.ob__list { list-style: none; margin: var(--sp-3) 0 0; padding: 0; }
.ob__list li {
  display: flex; align-items: flex-start; gap: var(--sp-3);
  padding: var(--sp-3) 0; border-top: 1px dashed var(--border);
}
.ob__list li.is-done { opacity: .55; }
.ob__mark { color: var(--brand); font-size: var(--fs-sm); line-height: 1.6; }
.ob__body { flex: 1; min-width: 0; }
.ob__name { color: var(--text); font-size: var(--fs-sm); }
.ob__list li.is-done .ob__name { text-decoration: line-through; }
.ob__desc { color: var(--text-muted); font-size: var(--fs-xs); margin-top: 2px; line-height: 1.7; }
.ob__tip { color: var(--text-faint); font-size: var(--fs-xs); margin-top: 4px; }
</style>
