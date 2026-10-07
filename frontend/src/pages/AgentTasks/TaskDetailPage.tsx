/**
 * 任务详情 + Trace 真树（Phase 9a，docs/07 §5.6 + §8）。
 * 轮询：任务未到终态时同时重拉详情与 trace —— trace 节点是执行过程中逐条落库的，
 * 所以"增量生长"就是每次重拉全集（docs/07 §8 说的 SSE span 事件在 8b 之后不存在）。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { getTaskDetail, getTaskTrace, isTaskSettled, rerunTask, followUpTask } from '../../api/agent';
import type { AgentTaskDetail, TaskTrace } from '../../api/agent';
import { toReadableError } from '../../api/client';
import { decideApproval, listApprovals } from '../../api/workflow';
import type { Approval } from '../../api/workflow';
import { POLL_INTERVAL_MS } from '../../hooks/pollCycle';
import { usePollUntilSettled } from '../../hooks/usePollUntilSettled';
import { formatCost, formatDuration, TRACE_COST_CURRENCY } from '../../lib/format';
import { reportFromRun } from '../../lib/taskReport';
import { sumRootAgentDurationMs } from '../../lib/traceTree';
import PromoteToDataset from '../../components/PromoteToDataset';
import ReportView from '../../components/ReportView';
import TaskStatusBadge from '../../components/TaskStatusBadge';
import TraceTree from '../../components/TraceTree';
import styles from './TaskDetailPage.module.css';

/** 一次刷新的三份数据（R55）：`refresh` 只取数并返回这个包，写 state 归 `apply`。
 *  为什么不用 `AgentTaskDetail` 直接当轮询的 `T`：detail/trace/approvals 三份都要跟着同一拍走，
 *  分开写就会出现「detail 已是新任务、trace 还是旧 run」的半屏。 */
interface TraceBundle {
  task: AgentTaskDetail;
  trace: TaskTrace | null;
  approvals: Approval[];
}

/** 终态但报告区空着时的话（R56：原来这种情况页面**什么都不说**，用户读成"报告就这样"）。
 *  三种"空"各有各的实话，不合并成一句：
 *   - 还在跑 → 下面那行轮询提示（`!isTaskSettled`）
 *   - 跑完了但这条 run 没有报告记录 → `没有记录到报告`（工具型任务/评测 run 本就是这形状）
 *   - 跑完了、meta 里有 report 但认不出形状 → 照实说认不出，别硬渲也别装没有
 *  判据用「meta 里有没有 report 这个键」而不是真值：空壳字典也是"有键、认不出"。 */
function reportEmptyHint(hasReportKey: boolean): string {
  return hasReportKey
    ? '这次执行记录到了一份报告，但形状不在本页能识别的范围内（原文见执行 meta）。'
    : '这次执行没有记录到报告。';
}

export default function TaskDetailPage() {
  const { taskId = '' } = useParams<{ taskId: string }>();
  const [detail, setDetail] = useState<AgentTaskDetail | null>(null);
  const [trace, setTrace] = useState<TaskTrace | null>(null);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  /** rerun / 追问 / 审批放行都会重新制造未完成对象：epoch 变 → 表重开（R10） */
  const [epoch, setEpoch] = useState(0);
  const [followUp, setFollowUp] = useState('');
  const [busy, setBusy] = useState(false);

  /** 一次刷新的三份数据打成一个包（R55）。
   *  为什么必须打包：`refresh` 是轮询的 `fetcher`，而 `pollCycle` 的 `stopped` 守卫查在
   *  `await fetcher()` **之后**——原来那版在函数体内直接 `setDetail/setTrace/setApprovals`，
   *  且 `onData` 是空的 `() => {}`，等于**所有**写 state 都发生在守卫之前：换 `taskId`（从列表点开另一条）
   *  或 epoch 重开时，上一条任务还在挂着的响应回来就把上一任务详情写进了当前屏幕，
   *  URL 是 A 而内容是 B。取数与写 state 分成两步，写这一步才归持有 `cancelled`/`onData` 的地方管。 */
  const refresh = useCallback(async (): Promise<TraceBundle> => {
    const task = await getTaskDetail(taskId);
    // trace 挂在最近一次 run 上；还没建 run（理论上 202 之后必有）就不拉
    const nextTrace = task.latest_run ? await getTaskTrace(task.latest_run.id) : null;
    // 非等待态一律给空数组：原来靠读 `approvals` state 来「只有非空才清空」，
    // 既要把 approvals 排除在依赖外（自激），又只是省一次同值写入——直接返回 [] 更诚实。
    const nextApprovals = task.status === 'waiting_approval' ? await listApprovals(task.id) : [];
    return { task, trace: nextTrace, approvals: nextApprovals };
  }, [taskId]);

  /** 唯一的写入口：三处调用各自负责自己的时机（挂载 effect 带 `cancelled`、轮询带 `stopped`、
   *  点击面带下面这道 `currentTaskId` 身份比对——R59 之前这里写的是「点击面自己」，而点击面
   *  其实谁也没管，那就是缺陷本身）。 */
  const apply = (bundle: TraceBundle) => {
    setDetail(bundle.task);
    setTrace(bundle.trace);
    setApprovals(bundle.approvals);
  };

  // R59：`/agents/A` → `/agents/B` 命中同一条路由、同一个 element，React Router **不会** remount
  // 本页。在 A 上点「重跑一次」的续体可以等到 URL 已经是 B 之后才落地：`busy` 只 disable 了
  // 页内按钮，表头的返回链接和侧边栏照样能点。挂载 effect 有 `cancelled`、轮询有 `stopped`，
  // 唯独 `act()` / `retry()` 这两个点击面没守卫。
  // 为什么必须在页内拦：B 若已落终态，它的表第一拍就 `isSettled → stop()`，此后无人再刷，
  // A 的数据会永久停在 B 的屏幕上（不是"闪一下自愈"那种）。
  // 用 ref 而不是闭包变量：闭包里的 `taskId` 正是那个已经过期的旧值，比它自己比不出任何东西。
  const currentTaskId = useRef(taskId);
  useEffect(() => {
    currentTaskId.current = taskId;
  }, [taskId]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const bundle = await refresh();
        if (!cancelled) apply(bundle); // R55：写在这里，因为 `cancelled` 只有这里知道
      } catch (err) {
        if (!cancelled) setError(toReadableError(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [refresh]);

  usePollUntilSettled<TraceBundle>({
    active: !loading && error === null && !!detail,
    restartKey: `${taskId}:${epoch}`,
    fetcher: refresh,
    isSettled: (bundle) => isTaskSettled(bundle.task.status),
    onData: apply,
    onFatal: (error) => setError(toReadableError(error)), // R78：止损文案与本页其他错误同一个函数
  });

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      // 这三种动作（rerun / 追问 / 审批放行）都会把任务从终态或等待态推回未落定，
      // 表若已经因 isSettled 停过，只有 restartKey 变了才会再开（R10）。
      // R55：信号先落，再刷屏——`await refresh()` 若排在 epoch 之前，它一抛就被同一个 catch 吞掉，
      // 重开信号跟着没了，正是 R10 要救的那个场景。
      setEpoch((e) => e + 1);
      const bundle = await refresh();
      // R59：`refresh` 取的是点击那一刻闭包里的 `taskId`（A）。若此刻屏幕已是 B，这笔写入丢掉——
      // 屏幕上的内容由 B 自己的挂载/轮询负责，别拿 A 的详情去盖它。
      if (currentTaskId.current === taskId) apply(bundle);
    } catch (err) {
      // 同一个守卫：换任务了，A 的失败不该在 B 的屏幕上出声（`error` 一置还会顺手把 B 的表关掉）
      if (currentTaskId.current === taskId) setError(toReadableError(err));
    } finally {
      // R76（与 Task 6 同口径）：不加 currentTaskId 守卫是有意的，但代价不是一帧——A 的迟到 finally
      // 会提前关掉 B 的 busy，B 那趟动作还在飞的这一拍里按钮又可点。会自愈的不加守卫（与 R64 同口径）。
      setBusy(false);
    }
  };

  /** 错误卡的「重试」（R54 同形，R55 补写入口）：`active` 的条件里有 `error === null`，
   *  一次三次止损就把表永久关掉；不清 `error` 就没人再拉数据——本页比 Task 6 更静，
   *  因为旧详情还在屏幕上，用户只会看到一排永远不动的数字。
   *  异常在这里接，不在 `refresh()` 里接：它是本表的 `fetcher`，抛出才算进连续失败计数。
   *  写法归 `apply`（R55：`refresh` 只取数），点击面自己写 state 是三条写入口之一，
   *  所以它同样要吃 R59 那道身份守卫。 */
  const retry = async () => {
    setError(null);
    setLoading(true);
    try {
      const bundle = await refresh();
      if (currentTaskId.current === taskId) apply(bundle); // R59
    } catch (err) {
      if (currentTaskId.current === taskId) setError(toReadableError(err)); // R59
    } finally {
      // R76：同 `act` 的 finally——A 的迟到 finally 会提前关掉 B 的 loading，
      // 屏上显 B 的旧详情直到 B 的请求落回来。会自愈的不加守卫（与 R64 同口径）。
      setLoading(false);
    }
  };

  if (loading) return <p className={styles.hint}>正在加载任务…</p>;
  if (!detail) {
    return (
      <div className={styles.errorCard}>
        <p className={styles.errorText}>{error ?? '任务不存在或不属于你'}</p>
        {/* R54：首屏取数失败也在这张卡上，不清 error 就出不去——没有重试就是死屏 */}
        <button type="button" className={styles.linkButton} onClick={() => void retry()}>重试</button>
        <Link className={styles.link} to="/agents">回任务列表</Link>
      </div>
    );
  }

  const run = detail.latest_run;
  // R56 收口：产品自 Phase 5 起写进 `meta.report` 的是七个字段的**结构化字典**
  //（`app/ai/graph/nodes/report.py:55` 的 `model_dump()` → `app/application/task_runner.py:217`），
  // 而降级支路（`report.py:59`）写的是 `{executive_summary, content}`、散文串也见过。
  // 旧代码在这里判 `typeof report === 'string'`，等于把主路径整条判掉 —— 报告区从不出现。
  // 归一交给 `lib/taskReport.ts`（纯函数、有针），本页只画它返回的东西。
  const report = reportFromRun(run);
  const reportKeyPresent = !!run?.meta && typeof run.meta === 'object' &&
    (run.meta as Record<string, unknown>).report !== undefined;
  // R126（终评 F3）：墙钟只汇总**根 agent 节点**（`sumRootAgentDurationMs`，lib/traceTree.ts）——
  // 嵌套 span 的子节点时间被父节点罩住，全量求和会重复计时；任一根节点缺 duration_ms 给 null
  // 走下面的「—」，不用 0 顶（今日 agent 侧无父子链接，此改动是口径加固而非在修屏上可见的 bug）。
  const totalDurationMs = trace ? sumRootAgentDurationMs(trace.nodes) : null;
  // R62：trace 还没拿到时给 **null** 而不是 0。同一张卡里「耗时」走的正是 null→「—」，
  // 而 0 会渲成「Token 0」和带币种符号的「0.0000」——`lib/format.ts` 自己立的规矩是
  // 「单价配好、这条 run 没花 token，那个 0 是诚实数字；不可得的数不许用 0 顶」
  // （同族问题在 `pages/Evaluation/MetricCharts.tsx` 的注释里也点过一次）。
  // 还没跑、还没拉 trace 时"0 token"是假话，「—」才是实话。
  // R73：这条注释刻意不出现币种字面量——Step 6 的门禁针按文本命中，分不清代码与注释；
  // 页面文件里留着币种字面量就等于「这页自己管币种」，那正是那条针要拦的东西。
  const totalTokens = trace ? trace.nodes.reduce((sum, node) => sum + node.total_tokens, 0) : null;
  const totalCost = trace ? trace.nodes.reduce((sum, node) => sum + node.cost, 0) : null;

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Link className={styles.link} to="/agents">← 任务列表</Link>
        <h1 className={styles.title}>{detail.title ?? detail.question}</h1>
        <div className={styles.headRow}>
          <TaskStatusBadge status={detail.status} />
          <span className={styles.metaText}>{detail.question}</span>
        </div>
        <dl className={styles.stats}>
          <div><dt>执行次数</dt><dd>{detail.run_count}</dd></div>
          <div><dt>耗时</dt><dd>{formatDuration(totalDurationMs)}</dd></div>
          <div><dt>Token</dt><dd>{totalTokens ?? '—'}</dd></div>
          {/* R62：成本这里**不能**直接把 null 交给 `formatCost`——它收到 null 渲的是「未配置单价」，
              那说的是单价没配（后端 `PricingOut` 的 null 口径），不是「trace 还没取到」。
              两种不可得各有各的话，破折号才是这一种的实话。 */}
          <div><dt>成本</dt><dd>{totalCost === null ? '—' : formatCost(totalCost, TRACE_COST_CURRENCY)}</dd></div>
          <div><dt>模型</dt><dd>{trace?.nodes.find((node) => node.model)?.model ?? '—'}</dd></div>
        </dl>
        <div className={styles.actions}>
          <button type="button" className={styles.linkButton} disabled={busy} onClick={() => void act(() => rerunTask(detail.id))}>
            重跑一次
          </button>
          {/* D9 回放沉淀：只挂终态 run（服务端同样拒绝未跑完的）；workflow 任务的入口
              在 Workflow 页的执行详情，两处共用 PromoteToDataset 组件 */}
          {run && isTaskSettled(detail.status) && <PromoteToDataset runId={run.id} />}
          {/* Phase 9b：报告升成独立资源后的入口。判据是 detail.report_id（后端 TaskOut 给的
              最新一份），不是 run —— 报告行属于**任务**的最近一次执行，而 report 区渲的是
              这一屏的 run；两者指向同一份，但入口该按资源走而不是按屏幕状态猜。 */}
          {detail.report_id && (
            <Link className={styles.link} to={`/reports/${detail.report_id}`}>看报告全文</Link>
          )}
          {run?.error_message && <span className={styles.errorText}>{run.error_message}</span>}
        </div>
      </header>

      {error && (
        <div className={styles.errorCard}>
          <p className={styles.errorText}>{error}</p>
          {/* R54：`active` 含 `error === null`，一次三次止损就把表永久关掉，旧详情会永远停在屏幕上 */}
          <button type="button" className={styles.linkButton} onClick={() => void retry()}>重试</button>
        </div>
      )}

      {approvals.length > 0 && (
        <section className={styles.approvals}>
          <h2 className={styles.sectionTitle}>待审批</h2>
          {approvals.map((item) => (
            <div key={item.id} className={styles.approvalRow}>
              <span>{item.graph_node}</span>
              <span className={styles.metaText}>{item.status}</span>
              {item.status === 'pending' && (
                <>
                  <button type="button" className={styles.linkButton} disabled={busy}
                          onClick={() => void act(() => decideApproval(detail.id, item.id, { decision: 'approved' }))}>
                    放行
                  </button>
                  <button type="button" className={styles.linkButton} disabled={busy}
                          onClick={() => void act(() => decideApproval(detail.id, item.id, { decision: 'rejected' }))}>
                    驳回
                  </button>
                </>
              )}
            </div>
          ))}
        </section>
      )}

      <section>
        <h2 className={styles.sectionTitle}>执行 Trace</h2>
        <TraceTree nodes={trace?.nodes ?? []} currency={TRACE_COST_CURRENCY} />
      </section>

      {report ? (
        <ReportView report={report} rawLabel="报告原文（meta.report）" raw={run?.meta?.report} />
      ) : (
        !isTaskSettled(detail.status) ? (
          <p className={styles.hint}>任务还在后台跑，页面每 {POLL_INTERVAL_MS / 1000} 秒自动刷新。</p>
        ) : (
          // R56：终态而报告区空着时**必须出声**。旧代码在这种情况下什么都不渲
          // （条件全 false），用户看到的是"报告区不存在"，与"报告是空的"读起来一样。
          <p className={styles.hint}>{reportEmptyHint(reportKeyPresent)}</p>
        )
      )}

      <section className={styles.compose}>
        <h2 className={styles.sectionTitle}>追问（带上本任务的历史结论）</h2>
        <textarea className={styles.textarea} rows={3} value={followUp}
                  placeholder="例如：把前三名按区域再拆一次"
                  onChange={(e) => setFollowUp(e.target.value)} disabled={busy} />
        <button type="button" className={styles.primaryButton} disabled={busy || followUp.trim().length === 0}
                onClick={() => void act(async () => { await followUpTask(detail.id, followUp.trim()); setFollowUp(''); })}>
          提交追问
        </button>
      </section>
    </div>
  );
}
