import React from 'react';
import ReactDOM from 'react-dom/client';

import App from './App';
import { AuthProvider } from './auth/AuthContext';
// 变量必须先于全局样式引入：global.css 里引用了 variables.css 定义的 token
import './styles/variables.css';
import './styles/global.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('找不到 #root 挂载点，请检查 index.html');
}

ReactDOM.createRoot(container).render(
  <React.StrictMode>
    {/* AuthProvider 在 RouterProvider 外层：路由守卫（RequireAuth）要读登录态。
        登录态变化不直接跳路由，只由守卫据 user 渲染 <Navigate>，故不依赖路由 hook。 */}
    <AuthProvider>
      <App />
    </AuthProvider>
  </React.StrictMode>,
);
