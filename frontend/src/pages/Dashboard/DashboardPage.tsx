/**
 * Dashboard 总览（Phase 9a，docs/07 §5.2 + spec §4.3）。
 *
 * 数字一律来自后端 `/stats/overview`，本页不做任何聚合：spec §2.1 已用实测否证
 * 前端聚合（`GET /tasks` 是分页的，拿当页算成功率是分母造假）。本页只负责
 * 「拉哪一档、怎么渲、什么时候再拉」。
 *
 * 轮询口径（spec §4.4）：最近任务里还有未落定的行时保持开表，全落定即停。
 * 本页不需要 epoch 重开（R10）——页内没有提交口，切 range 就是天然的 restartKey。
 */
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import { getOverview } from '../../api/stats';
import type { StatsOverview, StatsRange } from '../../api/stats';
import { isTaskSettled } from '../../api/agent';
import { toReadableError } from '../../api/client';
import { usePollUntilSettled } from '../../hooks/usePollUntilSettled';
import { formatCost } from '../../lib/format';
import { RANGE_TABS } from '../../lib/ranges';
import TaskStatusBadge from '../../components/TaskStatusBadge';
import styles from './DashboardPage.module.css';

/** 成功率：后端给的是 0~1 的小数（denom=0 时给 null）。
 *  null 渲「—」而不是 0%：0% 是一个结论，「—」才是「这区间没有终态行」的真话。 */
function rateText(rate: number | null): string {
  return rate === null ? '—' : `${Math.round(rate * 100)}%`;
}

export default function DashboardPage() {
  const [range, setRange] = useState<StatsRange>('today');
  const [data, setData] = useState<StatsOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  /** 只取数、不写 state（R55，同 Task 6 的 `load`）：`pollCycle` 的 `stopped` 守卫查在
   *  `await fetcher()` 之后，写在这里就等于绕过它——切 range 时旧 range 的响应还在挂着，
   *  回来先把旧数字写上，屏幕上「本周」的标签配着「今日」的数据。
   *  写 state 归三处：挂载 effect（带 `cancelled`）、`onData`（核保证不迟到）、`retry`（点击面）。 */
  const load = useCallback(async (): Promise<StatsOverview> => {
    return getOverview(range);
  }, [range]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const next = await load();
        if (!cancelled) setData(next); // R55：写在这里，因为 `cancelled` 只有这里知道
      } catch (err) {
        if (!cancelled) setError(toReadableError(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [load]);

  usePollUntilSettled<StatsOverview>({
    active: !loading && error === null && !!data,
    restartKey: range,
    fetcher: load,
    isSettled: (overview) => overview.recent_tasks.every((t) => isTaskSettled(t.status)),
    onData: setData,
    onFatal: (error) => setError(toReadableError(error)), // R78：与本页其它错误同一个文案口径
  });

  /** 错误屏的「重试」（R54，同 Task 6 的 `retry`）：清 `error` 才出得了 `if (error)` 那屏，
   *  接住异常才不会留 unhandled rejection。`load()` 自己不吃异常是刻意的——它是本表的 `fetcher`，
   *  抛出才算进 `pollCycle` 的连续失败计数。
   *  R70：**这里不需要 Task 6/7 那种身份守卫**（`currentTab`/`currentTaskId` 比对），别照搬过来。
   *  理由是渲染结构给的，不是感觉：下面紧跟着的 `if (loading) return <p>正在加载总览…</p>` 是**整页**
   *  早退，range 切换按钮和重试按钮都在它下面，所以 `retry` 在飞的时候屏幕上根本没有可点的 range——
   *  「点了重试再切 range」这条序列在这页组不出来。Task 6 需要守卫，是因为它的 Tab 条画在
   *  loading 三元**外面**；Task 7 需要，是因为它的返回链接和侧栏不受 `busy` 管。
   *  也就是说：这条免疫挂在渲染结构上。**谁把 range 那排按钮挪出 `loading` 早退，就得回来补 ref。** */
  const retry = async () => {
    setError(null);
    setLoading(true);
    try {
      setData(await load()); // R55：`load` 不写 state，点击面自己写
    } catch (err) {
      setError(toReadableError(err));
    } finally {
      setLoading(false);
    }
  };

  if (loading) return <p className={styles.hint}>正在加载总览…</p>;
  if (error && !data) {
    return (
      <div className={styles.errorCard}>
        <p className={styles.errorText}>{error}</p>
        <button type="button" className={styles.retry} onClick={() => void retry()}>
          重试
        </button>
      </div>
    );
  }
  if (!data) return null; // error 已置且无旧数据可留：上面的分支已返回，这里只是给 TS 收口

  const { cards } = data;
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>Dashboard</h1>
        <p className={styles.subTitle}>
          {data.scope === 'org' ? '统计范围：本组织' : '统计范围：仅本人'}
        </p>
      </header>

      {/* 顶部出错过这里仍是旧档数据：横幅常驻直到下一次成功拉取 */}
      {error && (
        <>
          <p className={styles.errorText}>{error}</p>
          {/* R56：这条横幅原本没有按钮，注释还写着「轮询本身会续」——那是假话。
              `active` 含 `error === null`，三次止损一置错表就关了，不会再有下一次拉取；
              点同一个 range 也不救（同值不改 state，effect 不重跑），唯一出路是切去别的
              range 再切回来。所以旧数字会一直顶着「今日」的标签挂着——按 R54 口径补按钮。 */}
          <button type="button" className={styles.retry} onClick={() => void retry()}>重试</button>
        </>
      )}

      <nav className={styles.rangeBar}>
        {RANGE_TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            className={item.key === range ? `${styles.rangeBtn} ${styles.rangeBtnActive}` : styles.rangeBtn}
            onClick={() => setRange(item.key)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <section className={styles.cards}>
        <Link className={styles.card} to="/agents">
          <span className={styles.cardLabel}>任务数</span>
          <span className={styles.cardValue}>{cards.task_total}</span>
        </Link>
        <Link className={styles.card} to="/agents">
          <span className={styles.cardLabel}>成功率</span>
          <span className={styles.cardValue}>{rateText(cards.success_rate)}</span>
          <span className={styles.cardHint}>
            {cards.success_basis.completed} 成 / {cards.success_basis.failed} 败
          </span>
        </Link>
        <Link className={styles.card} to="/settings">
          <span className={styles.cardLabel}>Token</span>
          <span className={styles.cardValue}>{cards.total_tokens.toLocaleString()}</span>
          <span className={styles.cardHint}>
            输入 {cards.prompt_tokens.toLocaleString()} · 输出 {cards.completion_tokens.toLocaleString()}
          </span>
        </Link>
        <Link className={styles.card} to="/settings">
          <span className={styles.cardLabel}>成本</span>
          <span className={styles.cardValue}>
            {cards.pricing_configured
              ? formatCost(cards.total_cost, cards.currency)
              : '未配置单价'}
          </span>
        </Link>
      </section>

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>
          我的最近任务
          <Link className={styles.link} to="/agents">
            全部任务
          </Link>
        </h2>
        {data.recent_tasks.length === 0 ? (
          // R126（终评 F2）：后端 `stats_service.overview` 取这两列时不传 since（只有 org + user.id + limit），
          // 「这区间」是前端替后端承诺了它没做的事。R19 口径如实说清：最近两列永远是本人的、与 range 无关。
          <p className={styles.hint}>还没有你的任务。（最近列表始终显示你自己的最新记录，与上方区间无关。）</p>
        ) : (
          <table className={styles.table}>
            <thead>
              <tr>
                <th>问题</th>
                <th>状态</th>
                <th>创建时间</th>
              </tr>
            </thead>
            <tbody>
              {data.recent_tasks.map((task) => (
                <tr key={task.id}>
                  <td>
                    <Link className={styles.link} to={`/agents/${task.id}`}>
                      {task.question}
                    </Link>
                  </td>
                  <td><TaskStatusBadge status={task.status} /></td>
                  <td>{new Date(task.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>
          我的最近会话
          <Link className={styles.link} to="/chat">
            进入 Chat
          </Link>
        </h2>
        {data.recent_conversations.length === 0 ? (
          <p className={styles.hint}>还没有会话。</p>
        ) : (
          <ul className={styles.list}>
            {data.recent_conversations.map((conversation) => (
              <li key={conversation.id} className={styles.listItem}>
                <span>{conversation.title || '未命名会话'}</span>
                <span className={styles.muted}>
                  {conversation.last_message_at
                    ? new Date(conversation.last_message_at).toLocaleString()
                    : new Date(conversation.created_at).toLocaleString()}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>快捷入口</h2>
        <div className={styles.quickLinks}>
          <Link className={styles.quickLink} to="/agents">新建分析任务</Link>
          <Link className={styles.quickLink} to="/knowledge">上传文档</Link>
        </div>
      </section>
    </div>
  );
}
