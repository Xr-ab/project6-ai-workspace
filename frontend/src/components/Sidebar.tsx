import { NavLink } from 'react-router-dom';
import type { ComponentProps } from 'react';

import NavIcon from './NavIcon';
import styles from './Sidebar.module.css';

type NavIconName = ComponentProps<typeof NavIcon>['name'];

interface NavItem {
  to: string;
  label: string;
  icon: NavIconName;
  enabled: boolean;
  /** 未放行项的角标：有明确批次去向就写批次（如 '9b'），缺省渲成「后续」。 */
  badge?: string;
}

/**
 * 侧边栏导航项（文档 §3）。**八项全部已放行且都接的是真页**（Phase 9b 起）：
 * Dashboard、AI Chat、知识库、Agent 任务、报告、Workflow、Evaluation、Settings。
 *
 * 「报告」曾是本表唯一的灰态入口（`enabled:false, badge:'9b'`），Phase 9b 建了
 * reports 表 + 三条 API + ReportsPage 之后升成真页。`enabled` / `badge` 两个字段
 * 与下面那条灰态分支**刻意保留**：它们是「后续 Phase 的页面」这套机制的载体
 * （`title` 文案 + 角标），删掉就得在下一个新页面出现时重写一遍。
 */
const NAV_ITEMS: readonly NavItem[] = [
  { to: '/dashboard', label: 'Dashboard', icon: 'dashboard', enabled: true },
  { to: '/chat', label: 'AI Chat', icon: 'chat', enabled: true },
  { to: '/knowledge', label: '知识库', icon: 'knowledge', enabled: true },
  { to: '/agents', label: 'Agent 任务', icon: 'agents', enabled: true },
  // Phase 9b 已落地：报告从"标着 9b 的灰态入口"升成真页面（列表 + 详情）。
  // 本表至此**没有灰态项**了 —— 十页全部联通（docs/10 的 Phase 9 出口那一格据此改判）。
  { to: '/reports', label: '报告', icon: 'reports', enabled: true },
  { to: '/workflows', label: 'Workflow', icon: 'workflows', enabled: true },
  // Phase 6 已上线 EvaluationPage，这里曾漏放行（一直挂"后续"灰态）—— 本次顺手纠正
  { to: '/evaluations', label: 'Evaluation', icon: 'evaluations', enabled: true },
  { to: '/settings', label: 'Settings', icon: 'settings', enabled: true },
];

export default function Sidebar() {
  return (
    <aside className={styles.sidebar}>
      <div className={styles.brand}>
        <span className={styles.brandMark}>AI</span>
        <span className={styles.brandText}>智能工作台</span>
      </div>

      <nav className={styles.nav}>
        {NAV_ITEMS.map((item) =>
          item.enabled ? (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                isActive ? `${styles.item} ${styles.itemActive}` : styles.item
              }
            >
              <NavIcon name={item.icon} />
              <span className={styles.label}>{item.label}</span>
            </NavLink>
          ) : (
            // 用 span 而不是 disabled 的 NavLink：避免被路由跳转，也不需要处理点击
            <span
              key={item.to}
              className={`${styles.item} ${styles.itemDisabled}`}
              title={item.badge ? `该模块在 Phase ${item.badge} 实现` : '该模块在后续 Phase 实现'}
            >
              <NavIcon name={item.icon} />
              <span className={styles.label}>{item.label}</span>
              <span className={styles.badge}>{item.badge ?? '后续'}</span>
            </span>
          ),
        )}
      </nav>

      <div className={styles.footer}>Phase 9a · 工作台（报告页 9b）</div>
    </aside>
  );
}
