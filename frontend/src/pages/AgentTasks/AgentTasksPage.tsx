/**
 * Agent 分析任务页（Phase 9a，docs/07 §5.5 + §12 那句「提交→202→轮询的前端闭环属本页」）。
 *
 * 三个设计约束（都来自 8b 之后的真实执行模型）：
 * 1. 提交后**不乐观插行**：只有拿到 202 才把返回的 task_id 拉进列表 ——
 *    队列不可用会 503、限流会 429，先插一行就是在展示一个根本没发生的任务（spec §3.4）。
 * 2. 进度靠轮询，不靠 SSE：执行在 worker 独立进程，请求面早返回（docs/07 §8 的 SSE 说法已作废）。
 * 3. member 撞 F1 预检是 403，文案直读后端中文提示，不在前端另造一套说法。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { isTaskSettled, listAgentTasks, submitTask, TASK_STATUSES } from '../../api/agent';
import type { AgentTaskSummary, TaskStatus } from '../../api/agent';
import { toReadableError } from '../../api/client';
import { usePollUntilSettled } from '../../hooks/usePollUntilSettled';
import TaskStatusBadge, { TASK_STATUS_LABELS } from '../../components/TaskStatusBadge';
import styles from './AgentTasksPage.module.css';

/** 列表只拉这么多：分页控件不在 9a 范围（后端 `list_tasks` 的 `limit` 上限 200） */
const LIST_LIMIT = 50;

/** 状态 Tab：'all' + 七值（复用 TASK_STATUSES，加态自动多一格；中文表与徽标同源） */
const TABS: { key: 'all' | TaskStatus; label: string }[] = [
  { key: 'all', label: '全部' },
  ...TASK_STATUSES.map((s) => ({ key: s, label: TASK_STATUS_LABELS[s] })),
];

/** 停表判据不在本页定义：`isTaskSettled` 来自 api/agent.ts（三页同源，见 R9）。
 *  这里只留一个反向命名，读 `isRunning(row.status)` 比读 `!isTaskSettled(...)` 顺。 */
function isRunning(status: TaskStatus): boolean {
  return !isTaskSettled(status);
}

export default function AgentTasksPage() {
  const [tab, setTab] = useState<'all' | TaskStatus>('all');
  const [tasks, setTasks] = useState<AgentTaskSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [question, setQuestion] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  /** 轮询重开计数（R10）：停表后发生新提交时，只有 restartKey 变了表才会再开 */
  const [epoch, setEpoch] = useState(0);

  // R69（与 Task 7 的 R59 同族，评审把这条停在 Task 6 账上、由 Task 7 的裁定统一拾起）：
  // 本页的 Tab 条渲染在 loading/错误卡的条件分支**外面**（见 Step 2 末尾的 `<nav className={styles.tabs}>`
  // 与紧随其后的三元），所以 `retry()` / `create()` 在飞的时候用户照样能切 Tab——而闭包里的 `tab`
  // 是点击那一刻的旧值。旧 Tab 的行写进新 Tab 的屏之后，若新 Tab 的列表恰为空（`isSettled([])` 为真）
  // 表第一拍就停，再没有一次拉取来纠正——这是**永久**错标，不是闪一下。
  // 用 ref 而不是直接比 `tab`：`retry`/`create` 的闭包里那个 `tab` 正是要被比的旧值，自己比自己恒等。
  const currentTab = useRef<'all' | TaskStatus>(tab);
  useEffect(() => {
    currentTab.current = tab;
  }, [tab]);

  /** 只取数、不写 state（R55）：它同时是轮询的 `fetcher`，而 `pollCycle` 的 `stopped` 守卫
   *  查在 `await fetcher()` **之后**——写了 state 就等于绕过那道守卫。
   *  可达路径：切 Tab 时上一 Tab 的请求还在挂着，旧响应回来先 `setTasks` 再被核丢弃，
   *  屏幕上留的是旧 Tab 的行；若新 Tab 的列表恰为空（`isSettled([])` 为真）表第一拍就停，
   *  再没有一次拉取来纠正。
   *  写 state 的四处各有各的守卫（R69 把这句话说实：原本这里写「四处各自持有自己的守卫」，
   *  而 `retry`/`create` 两处其实谁也没守，那正是缺陷本身）：挂载 effect 用 `cancelled`、
   *  轮询用核里的 `stopped`、`retry` 与 `create` 用上面的 `currentTab` 身份比对。
   *  `tabOverride` 只服务 `create` 那一处：提交后要把视图切到「全部」，而 `setTab` 之后本函数的
   *  闭包里 `tab` 还是旧值（React 的 state 更新是异步的），不覆盖就会拿旧 Tab 的行写进新 Tab 的屏。
   *  轮询侧无需担心传错：`pollCycle` 调 fetcher 是零参调用。 */
  const load = useCallback(async (tabOverride?: 'all' | TaskStatus): Promise<AgentTaskSummary[]> => {
    // 静默刷新：列表已有数据时不再回 loading，避免每次轮询闪一次骨架
    const effectiveTab = tabOverride ?? tab;
    return listAgentTasks({
      task_type: 'agent_analysis',
      ...(effectiveTab === 'all' ? {} : { status: effectiveTab }),
      limit: LIST_LIMIT,
    });
  }, [tab]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const rows = await load();
        if (!cancelled) setTasks(rows); // R55：写在这里，因为 `cancelled` 只有这里知道
      } catch (err) {
        if (!cancelled) setError(toReadableError(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [load]);

  // 只有列表里存在未完成任务时才开表；全部落定 → isSettled 真 → 停表（spec §4.4）
  usePollUntilSettled<AgentTaskSummary[]>({
    active: !loading && error === null,
    restartKey: `${tab}:${epoch}`,
    fetcher: load,
    isSettled: (rows) => rows.every((row) => !isRunning(row.status)),
    onData: setTasks,
    onFatal: (error) => setError(toReadableError(error)), // R78：止损文案与本页其他错误同一个函数
  });

  /** 错误卡上的「重试」走这里，不走 `load()`（R54）。
   *  两件事必须由这次点击自己做完：把 `error` 清掉（渲染分支 `error ? …` 看的就是它），以及接住 `load()`
   *  的再次失败。少了前者，一次 fatal 会把页面永久钉在错误卡上——`active` 的条件是
   *  `error === null`，表已经关了，不重开就再也刷不出数据；少了后者，Promise 直接变
   *  unhandled rejection，用户点了个静默。
   *  为什么 `load()` 自己不吃异常：它同时是轮询的 `fetcher`，异常**必须**抛出去，
   *  否则 `pollCycle` 的连续失败计数永远攒不满三次、止损形同虚设（口径见 Task 5 的段③ 针）。
   *  所以吃异常的是这里，不是 `load()`——这与既有 `pages/Evaluation/TraceTree.tsx` 里那个 `load`
   *  的做法同形（那个 `load` 不是轮询 fetcher，才可以在自己体内 try/catch）。 */
  const retry = async () => {
    setError(null);
    setLoading(true);
    try {
      const rows = await load();
      if (currentTab.current === tab) setTasks(rows); // R55：`load` 不写 state；R69：点击面也要身份比对
    } catch (err) {
      // 同一道守卫：切了 Tab 之后，旧 Tab 那趟请求的失败不该在新 Tab 的屏上挂出错误卡
      if (currentTab.current === tab) setError(toReadableError(err));
    } finally {
      // R76：不加 currentTab 守卫是有意的，但代价不是一帧——A 的 finally 会提前关掉 B 的加载态，
      // 屏上显 B 的旧列表直到 B 的请求落回来（一段网络时延）。会自愈的不加守卫（与 R64 同口径）。
      setLoading(false);
    }
  };

  const create = async () => {
    const text = question.trim();
    if (!text || submitting) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      await submitTask(text);
      setQuestion('');
      // 提交成功后重拉列表：queued 行由后端落库给（不是前端插的）。
      // R55 两刀，都为了「刚提交的任务必须在屏幕上出现并被轮询盯着」：
      // ① `setTab('all')`——停在「已完成」等 Tab 时，新的 queued 行会被过滤条件永久挡在外面，
      //    用户清空的输入框对应一个后端正在烧 token 的任务却毫无线索（原来只在注释里承认、没解决）。
      // ② epoch 先加、再拉列表——重开信号若排在 `await load()` 之后，那次拉取一抛就被同一个
      //    catch 吞掉，epoch 没动、`restartKey` 没变，而恰恰在「上一批全落定、表已停」这个
      //    epoch 存在的场景里没人再开表（R10 落空）。
      // 停表交给轮询 hook 的 isSettled——列表里出现未落定的行时它自然继续转，无需额外信号。
      setTab('all');
      setEpoch((e) => e + 1);
      // ③ 清 `error`（R56）：输入框在错误卡**上面**，三次止损期间照样提交得出去；而 `active`
      //    含 `error === null`，不清它 epoch 就白加、新任务连屏幕都上不了（错误卡整块替掉表格）。
      //    走到这一行说明 `submitTask` 已拿到 202，清掉旧错误不是掩盖问题：队列若还坏，
      //    下一拍的连续失败会再把错误屏顶回来。Task 7 的 `act` 一开始就带这条，本页此前漏了。
      setError(null);
      try {
        const rows = await load('all');
        // R69：这一笔取的是「全部」的行（上面已 `setTab('all')`），所以守卫比的是「现在还在不在全部」，
        // 不是「和点击那一刻的 Tab 比」——点击时可能停在「已完成」，那才是故意要离开的。
        // 用户在这一趟飞行里又点了别的 Tab，就把全部的行写成那个 Tab 的内容，永久错标。
        if (currentTab.current === 'all') setTasks(rows);
      } catch {
        // 这次刷新失败不冒充「提交失败」（submitError 的语义只归 submitTask 本身）：
        // 表已经因 epoch 重开（`active` 里的 `error === null` 由上面那句 R56 保证成立），
        // 后续拍次的连续失败会走 pollCycle 的三次止损 → 错误屏 → `retry`。
      }
    } catch (err) {
      // 503（队列不可用）/ 429（限流）/ 403（member 撞工具预检）都从这里出中文
      setSubmitError(toReadableError(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>分析任务</h1>
        <p className={styles.subtitle}>提交问题，Multi-Agent 链路会在后台跑完并生成报告。</p>
      </header>

      <section className={styles.compose}>
        <textarea
          className={styles.textarea}
          value={question}
          rows={3}
          placeholder="例如：统计上个月的销售额，并给出前三名产品的排名"
          onChange={(e) => setQuestion(e.target.value)}
          disabled={submitting}
        />
        <div className={styles.composeBar}>
          <button
            type="button"
            className={styles.primaryButton}
            onClick={() => void create()}
            disabled={submitting || question.trim().length === 0}
          >
            {submitting ? '提交中…' : '新建分析任务'}
          </button>
          {submitError && <p className={styles.submitError}>{submitError}</p>}
        </div>
      </section>

      <nav className={styles.tabs}>
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            className={tab === item.key ? `${styles.tab} ${styles.tabOn}` : styles.tab}
            onClick={() => setTab(item.key)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      {loading ? (
        <p className={styles.hint}>正在加载任务列表…</p>
      ) : error ? (
        <div className={styles.errorCard}>
          <p className={styles.errorText}>{error}</p>
          <button type="button" className={styles.linkButton} onClick={() => void retry()}>
            重试
          </button>
        </div>
      ) : tasks.length === 0 ? (
        <p className={styles.hint}>
          {tab === 'all' ? '还没有任务。在上方输入一个问题即可开始。' : '该状态下暂无任务。'}
        </p>
      ) : (
        <table className={styles.table}>
          <thead>
            <tr>
              <th>任务</th>
              <th>状态</th>
              <th>创建时间</th>
              <th>详情</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr key={task.id}>
                <td className={styles.questionCell}>{task.question}</td>
                <td><TaskStatusBadge status={task.status} /></td>
                <td>{new Date(task.created_at).toLocaleString()}</td>
                <td>
                  <Link className={styles.link} to={`/agents/${task.id}`}>
                    打开
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
