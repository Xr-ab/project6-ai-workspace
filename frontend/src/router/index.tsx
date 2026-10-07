import {
  createBrowserRouter,
  Navigate,
  useLocation,
  type RouteObject,
} from 'react-router-dom';
import type { ReactNode } from 'react';

import { useAuth } from '../auth/AuthContext';
import AppLayout from '../layouts/AppLayout';
import LoginPage from '../pages/Login/LoginPage';
import AgentTasksPage from '../pages/AgentTasks/AgentTasksPage';
import TaskDetailPage from '../pages/AgentTasks/TaskDetailPage';
import DashboardPage from '../pages/Dashboard/DashboardPage';
import SettingsPage from '../pages/Settings/SettingsPage';
import ChatPage from '../pages/chat/ChatPage';
import EvaluationPage from '../pages/Evaluation/EvaluationPage';
import KnowledgePage from '../pages/Knowledge/KnowledgePage';
import ReportsPage from '../pages/Reports/ReportsPage';
import WorkflowPage from '../pages/Workflow/WorkflowPage';

/**
 * 路由表（文档 §4 + Phase 8a T7 登录闭环）。
 * /login 全屏独立、不加守卫；其余业务路由整块包在 RequireAuth 里，未登录一律弹登录页。
 *
 * 暂不做 React.lazy 分包：目前真实页面都在首屏导航里，
 * 拆包只会多一次网络往返，等页面多起来再加（文档 §3 的懒加载策略保持不变）。
 */

/** 挂载探测期占位：initializing 时既不渲业务页也不弹登录，避免闪一下再跳。 */
function AuthSplash() {
  return (
    <div
      style={{
        height: '100%',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: 'var(--color-text-muted)',
        fontSize: 'var(--font-size-md)',
      }}
    >
      正在恢复登录状态…
    </div>
  );
}

/**
 * 路由守卫（套在 AppLayout 外层）：
 *   - initializing（拿本地 token 换 /auth/me 还没落地）→ 渲占位
 *   - 无 user → <Navigate to="/login">，把来源路径塞进 state，登录后可回跳
 *   - 已登录 → 原样渲 children（AppLayout）
 * 会话过期由 AuthContext 把 user 置空，这里下一次渲染就会自动弹回 /login。
 */
function RequireAuth({ children }: { children: ReactNode }) {
  const { user, initializing } = useAuth();
  const location = useLocation();
  if (initializing) return <AuthSplash />;
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return <>{children}</>;
}

/** 已登录访问 /login 直接回 Dashboard，不给人看两张重复的门。 */
function LoginGate() {
  const { user, initializing } = useAuth();
  if (initializing) return <AuthSplash />;
  if (user) return <Navigate to="/dashboard" replace />;
  return <LoginPage />;
}

const guardedRoutes: RouteObject[] = [
  { index: true, element: <Navigate to="/chat" replace /> },
  { path: 'chat', element: <ChatPage /> },
  { path: 'dashboard', element: <DashboardPage /> },
  { path: 'knowledge', element: <KnowledgePage /> },
  { path: 'agents', element: <AgentTasksPage /> },
  { path: 'agents/:taskId', element: <TaskDetailPage /> },
  // Phase 9b：报告从占位页升成真页面（列表 + 详情）。
  // 详情走**路由参数**而不是页内 state：报告有 id，可分享可刷新（docs/07 §4）。
  { path: 'reports', element: <ReportsPage /> },
  { path: 'reports/:reportId', element: <ReportsPage /> },
  { path: 'workflows', element: <WorkflowPage /> },
  { path: 'evaluations', element: <EvaluationPage /> },
  { path: 'settings', element: <SettingsPage /> },
];

export const router = createBrowserRouter([
  { path: '/login', element: <LoginGate /> },
  {
    path: '/',
    // 整个登录后框架（含侧边栏）都在守卫之后：未登录时连布局都不该露出
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: guardedRoutes,
  },
  // 未知路径交给 '/' 分支：登录后业务路由自然被守卫接到 /login
  { path: '*', element: <Navigate to="/chat" replace /> },
]);
