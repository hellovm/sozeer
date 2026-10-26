/**
 * 应用入口
 */
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import router from '@/router'
import App from '@/App.vue'

// ★ 样式顺序：tokens 必须最先（其它样式依赖变量）
import '@/styles/tokens.css'
import '@/styles/base.css'

const app = createApp(App)

app.use(createPinia())
app.use(router)

// ★ 全局错误兜底：避免未捕获异常导致白屏（生产环境应上报）
app.config.errorHandler = (err, _inst, info) => {
  console.error('[sozeer] 未捕获异常:', err, info)
}

app.mount('#app')
