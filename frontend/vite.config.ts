import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    // 端口必须锁死 5173：后端 CORS 只放行 http://localhost:5173。
    // Vite 默认在端口被占用时会自动换一个端口，那样所有请求都会被 CORS 拦掉，
    // 且报错很难定位 —— 所以用 strictPort 让它直接启动失败，问题暴露得更早。
    port: 5173,
    strictPort: true,
  },
});
