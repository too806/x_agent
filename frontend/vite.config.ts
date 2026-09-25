import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发时浏览器只跟 Vite 说话，/api 由 Vite 转给 FastAPI。
//
// 这样做的意义不只是"少配一个 CORS"：前端代码里的所有请求都是**同源相对路径**
// （`/api/...`），因此生产构建里换成任何反代都不需要改一行代码，也不存在把
// 后端地址硬编码进产物的风险。
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true, // 端口被占用时直接失败，不要静默换到 5174
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
