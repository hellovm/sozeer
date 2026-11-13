import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// ★★ v0.27.0 主域名切换（原 v0.25.9 双轨并行）：
//   新版前台已提升到 `/`，后台在 `/admin`，旧版降级到 `/legacy/`（回滚用）。
//   因此 base 必须是 '/'（否则资源 404）。
//   ★ 构建产物目录仍叫 static/app2 —— 改名会牵动部署脚本/测试多处引用，
//     收益远小于风险（漏改一处 → 静默 503 白屏）。
export default defineConfig({
  base: '/',
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: '../api/static/app2',
    emptyOutDir: true,
    // ★ 不做 sourcemap 到线上（体积 + 源码泄露）；本地调试用 dev server
    sourcemap: false,
    // ★ 目标浏览器：现代浏览器（项目面向国内 PC 端，兼容到 Chrome 90+）
    target: 'es2020',
    rollupOptions: {
      output: {
        // ★ 手动分包：把体积大的库拆开，避免单文件过大
        // Vite 8（底层 Rolldown 取代 Rollup）：manualChunks 只接受**函数**形式，
        // 旧的**对象**形式 `{ vue: [...] }` 会被当函数调用 → "manualChunks is not a function" 崩。
        //   函数语义等价：命中 vue 生态（vue / @vue/* / vue-router / pinia）归入 'vue' chunk。
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('vue') || id.includes('pinia')) {
              return 'vue'
            }
          }
        },
        // ★ 带 hash 便于长缓存（index.html 用 no-cache）
        chunkFileNames: 'assets/[name]-[hash].js',
        entryFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
  server: {
    port: 5273,
    // 本地开发代理到后端（避免 CORS）
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/v1': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
