import styles from './PlaceholderPage.module.css';

interface PlaceholderPageProps {
  title: string;
  description: string;
}

/**
 * 后续 Phase 页面的占位。
 * 路由里保留入口是为了让"完整产品闭环"一眼可见（文档 §11），
 * 但明确标注未实现，避免看起来像坏掉的页面。
 *
 * **Phase 9b 起本组件没有任何调用方**（`/reports` 是最后一个占位页，已升成真页）。
 * 刻意不删：它与 `components/Sidebar.tsx` 的灰态分支（`enabled:false` + `badge`）
 * 是同一套机制的两半 —— 下一个"先露入口、后落页面"的 Phase 会同时用到这两处，
 * 删掉就得重写一遍。**它现在是死代码，且是登记在案的死代码**（这句注释就是登记）：
 * 谁要清理死代码，请连 `Sidebar.tsx` 的 `enabled` / `badge` 字段与那条灰态分支一起清。
 */
export default function PlaceholderPage({ title, description }: PlaceholderPageProps) {
  return (
    <div className={styles.wrapper}>
      <div className={styles.card}>
        <span className={styles.tag}>后续 Phase</span>
        <h2 className={styles.title}>{title}</h2>
        <p className={styles.description}>{description}</p>
      </div>
    </div>
  );
}
