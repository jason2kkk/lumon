import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const devHost = env.VITE_DEV_HOST?.trim() || '127.0.0.1'

  return {
    plugins: [react(), tailwindcss()],
    server: {
      // 本地自托管默认只监听回环地址；远程联调需显式设置 VITE_DEV_HOST。
      host: devHost,
      allowedHosts: ['localhost', '127.0.0.1'],
      proxy: {
        '/api': {
          // 默认 8001：本地开发后端（scripts/start-local-dev.sh）
          // 8001 专用于本地开发，避免占用单容器或静态托管常用的 8000。
          target: `http://127.0.0.1:${env.VITE_DEV_API_PORT || '8001'}`,
          changeOrigin: true,
        },
      },
    },
  }
})
