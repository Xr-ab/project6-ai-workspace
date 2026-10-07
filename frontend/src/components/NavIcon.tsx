import type { ReactElement } from 'react';

/** 侧边栏导航图标。图标一律内联 SVG（文档 §1.3），不引图标库。 */
type IconName =
  | 'dashboard'
  | 'chat'
  | 'knowledge'
  | 'agents'
  | 'reports'
  | 'workflows'
  | 'evaluations'
  | 'settings';

const PATHS: Record<IconName, ReactElement> = {
  dashboard: (
    <>
      <rect x="2" y="2" width="5" height="5" rx="1" />
      <rect x="9" y="2" width="5" height="5" rx="1" />
      <rect x="2" y="9" width="5" height="5" rx="1" />
      <rect x="9" y="9" width="5" height="5" rx="1" />
    </>
  ),
  chat: <path d="M2 3.5h12v7.5H6.5L2 14z" />,
  knowledge: (
    <>
      <path d="M2.5 3A1.5 1.5 0 0 1 4 1.5h4v13H4A1.5 1.5 0 0 0 2.5 16z" />
      <path d="M13.5 3A1.5 1.5 0 0 0 12 1.5H8v13h4A1.5 1.5 0 0 1 13.5 16z" />
    </>
  ),
  agents: (
    <>
      <rect x="3.5" y="4.5" width="9" height="8" rx="2" />
      <path d="M8 1.5v3M8 12.5v2M1.5 8.5h2M12.5 8.5h2" />
    </>
  ),
  reports: (
    <>
      <path d="M3.5 1.5h6l3 3v10h-9z" />
      <path d="M9.5 1.5v3h3M6 8h4M6 11h4" />
    </>
  ),
  workflows: (
    <>
      <circle cx="4" cy="4" r="2" />
      <circle cx="12" cy="12" r="2" />
      <path d="M6 4h4.5a1.5 1.5 0 0 1 1.5 1.5V10" />
    </>
  ),
  evaluations: <path d="M2.5 14V9M6.5 14V3.5M10.5 14V6.5M14 14V2" />,
  settings: (
    <>
      <path d="M2 5h12M2 11h12" />
      <circle cx="6" cy="5" r="1.8" />
      <circle cx="10.5" cy="11" r="1.8" />
    </>
  ),
};

export default function NavIcon({ name }: { name: IconName }) {
  return (
    <svg
      viewBox="0 0 16 16"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {PATHS[name]}
    </svg>
  );
}
