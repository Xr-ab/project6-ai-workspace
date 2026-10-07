import { useEffect, useState } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';

import { useAuth } from '../auth/AuthContext';
import { setRateLimitNoticeHandler } from '../api/client';
import Sidebar from '../components/Sidebar';
import styles from './AppLayout.module.css';

/** 顶部栏标题。路由没列出来时回落到品牌名，不显示空标题 */
const PAGE_TITLES: Record<string, string> = {
  '/chat': 'AI Chat',
  '/dashboard': 'Dashboard',
  '/knowledge': '知识库',
  '/agents': 'Agent 任务',
  '/reports': '报告',
  '/workflows': 'AI Workflow',
  '/evaluations': 'Evaluation',
  '/settings': 'Settings',
};

/** 限流横幅停留时长（8b T11）：比各页 3s 的操作提示久一点，
 *  文案里带 Retry-After 秒数，要来得及读完 */
const RATE_NOTICE_DURATION = 5000;

/** 登录后的整体框架：左侧固定导航 + 右侧（顶部栏 + 主内容区） */
export default function AppLayout() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  /* 限流（8b T11）顶部横幅：client 层解析到 COMMON_429001 经回调送文案进来。
     卸载时把 handler 交回空函数，不留悬挂引用（同 setSessionExpiredHandler 纪律）。
     修复轮 1（m3）：state 存 {message, hit_at} 对象而不是裸字符串——连续两次 429
     文案相同（限流窗口内 Retry-After 一致，几乎必然同串）时 setState 同值被 React
     bail out，下方自隐 effect 不重跑，5s 从首次命中起算、密集限流下横幅提前消失；
     每次命中都是新对象引用 ⇒ effect 必清旧计时器重计。 */
  const [rateNotice, setRateNotice] = useState<{ message: string; hit_at: number } | null>(null);
  useEffect(() => {
    setRateLimitNoticeHandler((message) => setRateNotice({ message, hit_at: Date.now() }));
    return () => setRateLimitNoticeHandler(() => {});
  }, []);
  // 横幅定时自隐（各页 hint 条同款做法；秒数取 5s —— 文案里带着 Retry-After
  // 建议值，停留得比 3s 的操作成功提示久一点才来得及读完）。依赖 rateNotice 对象
  // 引用：每次新 429 命中都换引用 ⇒ cleanup  clearTimeout 后重挂 5s（m3 修复口径）
  useEffect(() => {
    if (!rateNotice) return;
    const timer = window.setTimeout(() => setRateNotice(null), RATE_NOTICE_DURATION);
    return () => window.clearTimeout(timer);
  }, [rateNotice]);
  // 用前缀匹配，这样 /agents/:id 这类子路由也能命中父级标题
  const title =
    Object.entries(PAGE_TITLES).find(([path]) => pathname.startsWith(path))?.[1] ??
    '企业 AI 智能工作台';

  const onLogout = async () => {
    // logout 清空 user → RequireAuth 下一次渲染弹回 /login，这里不自己跳。
    // 但登出可能发生在守卫正在渲的业务页，user 归零会立刻把整棵子树换成
    // /login，无需 await 后再 navigate（那样反而可能双跳）。
    await logout();
    navigate('/login', { replace: true });
  };

  return (
    <div className={styles.layout}>
      <Sidebar />
      <div className={styles.main}>
        <header className={styles.topbar}>
          <h1 className={styles.title}>{title}</h1>
          {/* 顶栏身份（Phase 8a T7）：邮箱 · 角色 + 登出。
              role 目前只有 admin/member 两值（06 §6.3），未知值兜底成"成员"不裸渲 */}
          <div className={styles.identityBox}>
            {user && (
              <span className={styles.identity}>
                {user.email} · {user.role === 'admin' ? '管理员' : '成员'}
              </span>
            )}
            <button type="button" className={styles.logoutButton} onClick={() => void onLogout()}>
              登出
            </button>
          </div>
        </header>
        {/* 限流（COMMON_429001）全局顶部提示：client 层解析后回调送进来的文案 */}
        {rateNotice && (
          <div className={styles.rateBanner} role="alert">
            {rateNotice.message}
          </div>
        )}
        <main className={styles.content}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
