/**
 * Workflow 页（Phase 7 Task 8，docs/07 §5.8）：
 *   编目三卡片 → 详情（按 input_spec 渲触发表单）→ 执行记录列表 →
 *   执行详情（七态 badge + 步骤条 + waiting_approval 审批区），全程轮询真实链路。
 *
 * 页面结构 = 一层视图切换（catalog / workflow 详情 / task 详情并列在详情层），
 * 不加路由层级：三块状态互相引用（触发后要立刻盯同一次执行），
 * 拆多页反而要在 URL 里搬状态，YAGNI。
 *
 * 轮询守卫逐字对齐 EvaluationPage 的 useRunPolling：
 * cancelled flag + 终态 stop + 连续 MAX_POLL_FAILURES 次失败 halted 交回人工。
 * 与那边的一处语义差异：waiting_approval 也 stop —— 图停在断点等人，
 * 不是慢，继续每 2s 打后端没有意义；决策后 retry() 原地重启轮询续跑段。
 * 唯一的豁免窗口：决策重启的那次 attempt 首读可能仍撞上决策前的陈旧
 * waiting_approval（后台翻状态有秒级窗口），读到第二次才真停表 —— 见 useTaskPolling。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
// Phase 9b 起本页需要一个跨页跳转（产物区 → 报告详情），这是本页第一次用 Link。
// 为什么用 Link 而不是在页内切视图：报告有自己的路由（/reports/:id）与自己的页面，
// 做成"本页内再嵌一个详情层"会让报告在 Workflow 页和报告页长得不一样。
import { Link } from 'react-router-dom';

import { toReadableError } from '../../api/client';
import { getTaskDetail, getTaskTrace, listAgentTasks } from '../../api/agent';
import type {
  AgentTaskDetail,
  AgentTaskSummary,
  TaskStatus,
  TaskTrace,
} from '../../api/agent';
import { listDocuments } from '../../api/document';
import type { KnowledgeDocument } from '../../types/document';
import {
  decideApproval,
  getWorkflow,
  listApprovals,
  listWorkflows,
  triggerWorkflow,
} from '../../api/workflow';
import type { Approval, Workflow } from '../../api/workflow';
import { formatDuration } from '../../lib/format';
import { reportFromWorkflowRun } from '../../lib/taskReport';
import PromoteToDataset from '../../components/PromoteToDataset';
import ReportView from '../../components/ReportView';
import styles from './WorkflowPage.module.css';

/** 轮询节奏与止损线（同 EvaluationPage）：2s 一次；连续 3 次失败停手交人工 */
const POLL_INTERVAL_MS = 2000;
const MAX_POLL_FAILURES = 3;

/** 成功提示的停留时长（同 EvaluationPage / KnowledgePage 的做法） */
const HINT_DURATION = 3000;

/** Task 状态里"这轮监控可以收工"的三态。waiting_approval 不在此列 ——
 *  它停表等人但不算完（见 useTaskPolling 注释） */
function isTerminalStatus(status: TaskStatus): boolean {
  return status === 'completed' || status === 'failed' || status === 'rejected';
}

/* ---------------- 状态 badge：七值穷尽，default 是编译期锁 ---------------- */

/**
 * status → 文案 + 配色。switch 的判据是窄类型 TaskStatus（api/agent.ts 在读取
 * 边界把后端的裸 string 收口成七值联合（8b T3/T4 扩 queued），集合外取值当场
 * 抛错进轮询失败计数），
 * 所以 default 的 `never` 断言是真守卫：联合新增取值而这里不加 case，
 * `npm run build` 立刻红 —— 机制与 EvaluationPage.dirView 同款。
 *
 * 配色口径：waiting_approval = 橙（--color-warning，等人是醒目事项不是错误）；
 * rejected = 灰描边 —— 拒绝是人为决定不是链路失败（spec §6 同一口径，
 * 后端也不给它进失败分类表），不许渲成红色。
 * queued = 中性灰（8b 新增：已入队等 worker 领取，和 pending 同属"还没开跑"，
 * 与 running 的主色实心必须一眼可辨）；pending 一档文案不动（既有渲染行为保持）。
 */
function taskStatusView(status: TaskStatus): { label: string; className: string } {
  switch (status) {
    case 'completed':
      return { label: '已完成', className: styles.badgeReady };
    case 'failed':
      return { label: '失败', className: styles.badgeFailed };
    case 'running':
      return { label: '执行中', className: styles.badgeProcessing };
    case 'pending':
      return { label: '排队中', className: styles.badgeNeutral };
    case 'queued':
      return { label: '已排队', className: styles.badgeNeutral };
    case 'waiting_approval':
      return { label: '待审批', className: styles.badgeApproval };
    case 'rejected':
      return { label: '已驳回', className: styles.badgeRejected };
    default: {
      // 上面 case 覆盖全部 TaskStatus 后，这里的 status 是 never。
      // 类型层编译期到不了；运行期由 parseTaskStatus 挡在边界外，仅作兜底。
      const _exhaustive: never = status;
      void _exhaustive;
      throw new Error(`taskStatusView: 意外的任务状态取值 ${String(status)}`);
    }
  }
}

function StatusBadge({ status }: { status: TaskStatus }) {
  const view = taskStatusView(status);
  return <span className={`${styles.badge} ${view.className}`}>{view.label}</span>;
}

/* ---------------- 本地格式化（formatDuration 已收口到 lib/format，Phase 9a Task 7；
   formatTime 留在这里：它要的是「月/日 时:分:秒」的紧凑列表档，lib/format 没有这一档） ---------------- */

function formatTime(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

/* ---------------- 轮询 hook（守卫结构逐字对齐 useRunPolling） ---------------- */

function useTaskPolling(taskId: string | null) {
  const [task, setTask] = useState<AgentTaskDetail | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [halted, setHalted] = useState(false);
  // 手动重试的计数器：+1 让 effect 整体重跑（决策后也是它重启轮询）
  const [attempt, setAttempt] = useState(0);
  // 决策重启的豁免标志：retry({ fromDecision: true }) 置位，effect 启动时消费一次。
  // 决策刚落地时后台续跑还没来得及翻 task.status（批准→deliver 要几秒，
  // 驳回→resume 落终态也有窗口），首个 tick 读到的 waiting_approval 是旧值。
  const decisionGraceRef = useRef(false);

  useEffect(() => {
    // taskId 变化 → 上一轮的状态全部作废（重置）
    setTask(null);
    setFailure(null);
    setHalted(false);
    if (!taskId) {
      // 评审 NIT-2 收编：早返也得消费掉豁免标志，否则 ref===true 时遇到
      // taskId 归 null 的 effect 重跑，豁免窗会被留给下一个不相干任务的
      // 首个 attempt（现网 UI 触发不到，零成本防御）。
      decisionGraceRef.current = false;
      return;
    }

    // cancelled 守卫与 EvaluationPage 同理由：taskId 翻转后在飞的那条
    // getTaskDetail 不许再落 setState，否则旧任务的状态可能糊到新任务头上。
    let cancelled = false;
    let consecutiveFailures = 0;
    let timer = 0;
    // 本次 attempt 是否来自审批决策重启：只有它享有首读豁免；
    // 手动「刷新状态」不置位，正常跑动首次到达 waiting_approval 仍立即停表
    const graceFromDecision = decisionGraceRef.current;
    decisionGraceRef.current = false;
    let tickCount = 0;
    const stop = () => window.clearInterval(timer);

    const tick = async () => {
      try {
        const fresh = await getTaskDetail(taskId);
        if (cancelled) return;
        consecutiveFailures = 0;
        tickCount += 1;
        setTask(fresh);
        setFailure(null);
        if (isTerminalStatus(fresh.status)) {
          stop(); // 进终态即停
        } else if (fresh.status === 'waiting_approval') {
          // 待审停表等人，决策后 retry() 重启。例外只有一格：决策重启那次
          // attempt 的首读可能还是决策前的陈旧 waiting_approval，当场停表
          // 轮询就假死到人工刷新为止。所以首读照常渲染但不停，连续第二次
          // 读到才认定真断点；tickCount>1 后豁免自然失效，第二个人工断点
          // 依旧首读即停，语义不破。
          if (!(graceFromDecision && tickCount === 1)) {
            stop();
          }
        }
      } catch (error) {
        if (cancelled) return;
        consecutiveFailures += 1;
        setFailure(toReadableError(error));
        if (consecutiveFailures >= MAX_POLL_FAILURES) {
          stop();
          setHalted(true); // 止损：显示"进度获取失败，点击重试"，不再自动打后端
        }
      }
    };

    void tick(); // 立即拉一次，不等第一个 2s 间隔
    timer = window.setInterval(() => void tick(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true; // 卸载必清（守卫 + 清 timer 两件事在同一处生效）
      stop();
    };
  }, [taskId, attempt]);

  // options 是可选对象参数：给 onClick={retry} 直接传 MouseEvent 时
  // fromDecision 是 undefined，不会误开豁免窗
  const retry = useCallback((options?: { fromDecision?: boolean }) => {
    if (options?.fromDecision === true) {
      decisionGraceRef.current = true;
    }
    setAttempt((a) => a + 1);
  }, []);

  return { task, failure, halted, retry };
}

/* ---------------- Trace（步骤条数据源） ---------------- */

/**
 * 拉最近一次执行的 span 序列。statusKey（= task.status）进依赖：
 * 状态每翻一次重拉一次 —— 断点前已跑完的 span 先落库、续跑段到终态再落，
 * 轮询拼的就是这个增量生长的序列（docs/07 §5.8 的"阶段步骤条"）。
 */
function useTaskTrace(runId: string | null, statusKey: string) {
  const [trace, setTrace] = useState<TaskTrace | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setTrace(null);
    setError(null);
    if (!runId) return;
    let cancelled = false;
    void (async () => {
      try {
        const fresh = await getTaskTrace(runId);
        if (!cancelled) setTrace(fresh);
      } catch (err) {
        if (!cancelled) setError(toReadableError(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, statusKey]);

  return { trace, error };
}

/**
 * 步骤条：agent 节点序列（后端按 started_at 升序平铺，这里不再重排）+
 * 状态驱动的"当前位置"。待审时在已跑完的序列后面补一格虚拟的 approval 步
 * （橙态）—— 图停在 approval 节点**之前**，span 还没落，这一格是
 * "接下来等人"的诚实表达，不是已执行记录。
 *
 * 只渲 kind === 'agent' 的节点：trace 现在含 agent + tool 两类（docs/06 §4 换形状），
 * 步骤条语义是"跑了哪些节点"，工具节点属于节点内部细节，混进来会把一次取数显示成一步、
 * 步骤计数也是假的（工具名不许漏进 step bar，spec §3.2 出口针⑧）。
 */
function StepBar({ trace, status }: { trace: TaskTrace | null; status: TaskStatus }) {
  if (!trace) {
    return <p className={styles.hint}>{status === 'pending' ? '等待执行…' : '暂无节点记录。'}</p>;
  }
  const steps = trace.nodes.filter((node) => node.kind === 'agent');
  if (steps.length === 0 && status !== 'waiting_approval') {
    // 文案不写 "span"（R23）：用户不认识这个词，且它原是旧形状的残留名
    return <p className={styles.hint}>这次执行还没有记录到节点。</p>;
  }
  // Global Constraint（plan:15「凡展示 run 指标处必须同时展示模型名」）：步骤条逐格渲
  // total_tokens，模型名在这下面补一行。数据从已轮询的 trace agent 节点的 model 字段派生
  // （TraceNode.model，TraceTree 渲同一个字段）——去重集合；全是 null（空 trace /
  // 纯规则图）不显；多模型斜杠连接并带「跨模型不可比」，沿用评测页「不可比」措辞口径
  // （EvaluationPage:1328/1377），不另造一套说法。
  const models = [...new Set(steps.map((node) => node.model).filter((m): m is string => !!m))];
  return (
    <>
      <ol className={styles.steps}>
        {steps.map((node, index) => (
          <li key={node.span_id} className={styles.step}>
            <span className={styles.stepIndex}>{index + 1}</span>
            <span className={styles.stepName}>{node.name}</span>
            <span className={`${styles.stepStatus} ${node.status === 'ok' ? styles.stepOk : styles.stepError}`} title={node.error_message ?? undefined}>
              {node.status}
            </span>
            <span className={styles.stepMeta}>{formatDuration(node.duration_ms)}</span>
            <span className={styles.stepMeta}>{node.total_tokens} tok</span>
          </li>
        ))}
        {status === 'waiting_approval' && (
          <li className={`${styles.step} ${styles.stepPending}`}>
            <span className={styles.stepIndex}>{steps.length + 1}</span>
            <span className={styles.stepName}>approval</span>
            <span className={`${styles.badge} ${styles.badgeApproval}`}>待审批</span>
          </li>
        )}
      </ol>
      {models.length > 0 && (
        <p className={styles.hint}>
          模型 {models.join(' / ')}
          {models.length > 1 ? ' —— 跨模型不可比' : ''}
        </p>
      )}
    </>
  );
}

/* ---------------- 审批区（waiting_approval 时才挂载） ---------------- */

function ApprovalPanel({ taskId, onDecided }: { taskId: string; onDecided: () => void }) {
  const [approvals, setApprovals] = useState<Approval[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [comment, setComment] = useState('');
  const [submitting, setSubmitting] = useState(false);
  // 决策成功后的留言回执：审批区会随任务态翻回 running 而卸载，先本地留一句
  const [decidedTip, setDecidedTip] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setError(null);
    try {
      // 后端保证 pending 在前，这里不重排
      setApprovals(await listApprovals(taskId));
    } catch (err) {
      setError(toReadableError(err));
    }
  }, [taskId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const decide = async (approvalId: string, decision: 'approved' | 'rejected') => {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await decideApproval(taskId, approvalId, {
        decision,
        // 空留言不发键：comment 是可选列，发 "" 不如不发（后端 max_length 对 ""
        // 也照收，但留言框留白语义就是"没留言"）
        ...(comment.trim() ? { comment: comment.trim() } : {}),
      });
      setDecidedTip(decision === 'approved' ? '已放行，任务从断点续跑中…' : '已驳回。');
      setComment('');
      onDecided(); // 重启父层轮询：task.status 会翻回 running 并跑向终态
      await reload();
    } catch (err) {
      setError(toReadableError(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className={styles.approvalBox}>
      <div className={styles.detailTitle}>审批区</div>
      {decidedTip && <p className={styles.successBanner}>{decidedTip}</p>}
      {error && (
        <div className={styles.errorBanner}>
          {error}
          <button type="button" className={styles.actionButton} onClick={() => void reload()}>
            重试
          </button>
        </div>
      )}
      {!approvals && !error && <p className={styles.hint}>正在加载审批记录…</p>}
      {approvals && approvals.length === 0 && (
        <p className={styles.hint}>这个任务没有审批记录（可能刚落断点，点上方"刷新状态"再看）。</p>
      )}
      {approvals?.map((approval) => {
        const pending = approval.status === 'pending';
        return (
          <div key={approval.id} className={styles.approvalRow}>
            <span className={styles.stepName}>{approval.graph_node}</span>
            <StatusLikeApprovalBadge status={approval.status} />
            {approval.comment && <span className={styles.approvalComment}>留言：{approval.comment}</span>}
            {pending && (
              <>
                <button
                  type="button"
                  className={styles.primaryButton}
                  disabled={submitting}
                  onClick={() => void decide(approval.id, 'approved')}
                >
                  批准通过
                </button>
                <button
                  type="button"
                  className={styles.rejectButton}
                  disabled={submitting}
                  onClick={() => void decide(approval.id, 'rejected')}
                >
                  驳回
                </button>
              </>
            )}
          </div>
        );
      })}
      {approvals?.some((a) => a.status === 'pending') && (
        <label className={styles.formLabel}>
          审批留言（可选）
          <input
            className={styles.input}
            value={comment}
            maxLength={2000}
            onChange={(e) => setComment(e.target.value)}
            placeholder="给这条决策留个原因"
          />
        </label>
      )}
    </div>
  );
}

/** 审批记录自己的三态小徽标（pending/approved/rejected）——
 *  和任务六态是两套语义，不复用 taskStatusView */
function StatusLikeApprovalBadge({ status }: { status: string }) {
  const className =
    status === 'approved'
      ? styles.badgeReady
      : status === 'rejected'
        ? styles.badgeRejected
        : styles.badgeApproval;
  const label = status === 'approved' ? '已放行' : status === 'rejected' ? '已驳回' : '待审批';
  return <span className={`${styles.badge} ${className}`}>{label}</span>;
}

/* ---------------- 触发表单（按 input_spec 渲字段） ---------------- */

function TriggerForm({
  workflow,
  onTriggered,
}: {
  workflow: Workflow;
  onTriggered: (taskId: string) => void;
}) {
  const specKeys = Object.keys(workflow.input_spec);
  const [values, setValues] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 下拉数据只在表单真出现 document_id 键时才拉（W1/W2 没有这个键）
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);

  useEffect(() => {
    if (!('document_id' in workflow.input_spec)) return;
    let cancelled = false;
    void (async () => {
      try {
        const rows = await listDocuments();
        if (!cancelled) setDocuments(rows);
      } catch (err) {
        if (!cancelled) setError(toReadableError(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workflow]);

  // 后端必填闸 = input_spec 每个键都必填且非空（缺/空 → 422），前端同口径收按钮
  const ready =
    specKeys.length > 0 &&
    specKeys.every((key) => (values[key] ?? '').trim() !== '') &&
    !submitting;

  const submit = async () => {
    if (!ready) return;
    setSubmitting(true);
    setError(null);
    try {
      const inputs: Record<string, string> = {};
      for (const key of specKeys) inputs[key] = values[key].trim();
      const result = await triggerWorkflow(workflow.id, inputs);
      // 202 只给 id：真实进度交给父层的任务轮询（trigger 完直接盯这次执行）
      onTriggered(result.task_id);
      setValues({});
    } catch (err) {
      setError(toReadableError(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className={styles.formBlock}>
      <div className={styles.formTitle}>发起执行（参数按 input_spec 必填）</div>
      {error && <div className={styles.errorBanner}>{error}</div>}
      {specKeys.map((key) => {
        const typeHint = workflow.input_spec[key];
        if (key === 'question') {
          return (
            <label key={key} className={styles.formLabel}>
              {key}（{typeHint}）
              <textarea
                className={styles.textarea}
                rows={3}
                value={values[key] ?? ''}
                onChange={(e) => setValues((v) => ({ ...v, [key]: e.target.value }))}
                placeholder="要问这张 Workflow 的问题"
              />
            </label>
          );
        }
        if (key === 'document_id') {
          return (
            <label key={key} className={styles.formLabel}>
              {key}（{typeHint}）
              <select
                className={styles.select}
                value={values[key] ?? ''}
                onChange={(e) => setValues((v) => ({ ...v, [key]: e.target.value }))}
              >
                <option value="">请选择知识库文档</option>
                {documents.map((doc) => (
                  <option key={doc.id} value={doc.id}>
                    {doc.filename}
                  </option>
                ))}
              </select>
            </label>
          );
        }
        // 其余键给通用文本框：input_spec 的值只是给人看的类型声明，
        // 后端也只判"在不在 + 空不空"不判形状（_require_spec_inputs 口径），
        // 前端不自造第二套校验。
        return (
          <label key={key} className={styles.formLabel}>
            {key}（{typeHint}）
            <input
              className={styles.input}
              value={values[key] ?? ''}
              onChange={(e) => setValues((v) => ({ ...v, [key]: e.target.value }))}
            />
          </label>
        );
      })}
      <button type="button" className={styles.primaryButton} disabled={!ready} onClick={() => void submit()}>
        {submitting ? '触发中…' : '触发执行'}
      </button>
    </div>
  );
}

/* ---------------- 执行详情面板 ---------------- */

function TaskDetailPanel({
  taskId,
  task,
  failure,
  halted,
  retry,
  retryAfterDecision,
  onBack,
}: {
  taskId: string;
  task: AgentTaskDetail | null;
  failure: string | null;
  halted: boolean;
  retry: () => void;
  // 审批决策专用的重启入口：带 fromDecision 标记，享有陈旧待审首读豁免
  retryAfterDecision: () => void;
  onBack: () => void;
}) {
  const runId = task?.latest_run?.id ?? null;
  const { trace, error: traceError } = useTaskTrace(runId, task?.status ?? '');

  return (
    <section className={styles.section}>
      <div className={styles.sectionTitle}>
        执行详情
        {task && <StatusBadge status={task.status} />}
        <button type="button" className={styles.actionButton} onClick={onBack}>
          返回列表
        </button>
        {/* 待审时也常备"刷新状态"：审批行刚落库有先后，轮询此时是停表的 */}
        {(halted || task === null || task.status === 'waiting_approval') && (
          <button type="button" className={styles.actionButton} onClick={retry}>
            刷新状态
          </button>
        )}
      </div>
      {failure && (
        <div className={styles.errorBanner}>
          进度获取失败：{failure}
          {halted && '（已停止自动轮询，点「刷新状态」重来）'}
        </div>
      )}
      {!task && !failure && <p className={styles.hint}>正在加载任务…</p>}
      {task && (
        <>
          <p className={styles.hint}>
            {task.title ?? task.question}
            {' · '}第 {task.latest_run?.run_no ?? 0} 次执行 · 创建于 {formatTime(task.created_at)}
          </p>
          {task.latest_run && (
            <div className={styles.progressBar}>
              <div
                className={styles.progressFill}
                style={{ width: `${Math.min(100, Math.max(0, task.latest_run.progress))}%` }}
              />
            </div>
          )}
          <div className={styles.detailTitle}>步骤条（节点序列随 Trace 轮询刷新）</div>
          {traceError && <div className={styles.errorBanner}>{traceError}</div>}
          <StepBar trace={trace} status={task.status} />
          {task.status === 'waiting_approval' && (
            <ApprovalPanel taskId={taskId} onDecided={retryAfterDecision} />
          )}
          {task.latest_run?.error_message && (
            <div className={styles.errorBanner}>
              失败分类 {task.latest_run.failure_category ?? '—'}：{task.latest_run.error_message}
            </div>
          )}
          {task.status === 'completed' && <ResultBlock task={task} />}
          {task.status === 'rejected' && (
            <p className={styles.hint}>审批被驳回，没有产出成品 —— 这是人为决定，不是链路失败。</p>
          )}
          {/* D9 回放沉淀：终态 run 才给入口（与 Agent 任务详情共用同一个组件） */}
          {runId && isTerminalStatus(task.status) && <PromoteToDataset runId={runId} />}
        </>
      )}
    </section>
  );
}

/** 终态产物：workflow 的 result 落在 latest_run.meta.result（deliver 装配）。
 *  meta 是透传壳，形状随图而变 —— 真没 result 才说"没记录到"；
 *  认得出的形状说人话；认不出就把 result 摊开，不静默吞也不误报。
 *  认得出的形状有**两**种（R56 收口时补齐）：doc_summary 形是 `result.summary`（一段散文），
 *  sales_analysis 形是 `result.report`（七字段结构化字典，走 ReportView）。 */
function ResultBlock({ task }: { task: AgentTaskDetail }) {
  const meta = task.latest_run?.meta ?? null;
  const result = (meta as { result?: Record<string, unknown> } | null)?.result;
  // 欠账 #11：review_timeout 由 workflow deliver 节点写进 run.meta
  // （business_qa.py / sales_analysis.py：终审非 pass 却走到终态 = retry 超限降级放行）。
  // 终态照旧 completed、报告带警告，所以徽标只能在产物区补一句"存疑"，不碰状态 badge。
  const reviewTimeout = (meta as { review_timeout?: unknown } | null)?.review_timeout === true;
  const summary = typeof result?.summary === 'string' ? (result.summary as string) : null;
  const sources = Array.isArray(result?.sources) ? (result.sources as unknown[]) : null;
  // R56 同族收口：`sales_analysis` 的 deliver 是 Phase 5 那个 report 节点，它产出的是
  // **七个字段的结构化字典**（`app/ai/graph/nodes/report.py:55`）。旧代码只从里面挑
  // `executive_summary` 一段出来渲，剩下六段只在下面「执行 meta」的裸 JSON 里看得见 ——
  // 与 Agent 详情页被判成字符串的是同一个根因（消费侧按臆想的形状取），一并走同一个归一器。
  const report = reportFromWorkflowRun(task.latest_run);
  const dataSources = Array.isArray(result?.data_sources) ? (result.data_sources as unknown[]) : null;
  const hasResult = result !== null && result !== undefined;
  return (
    <div className={styles.detailBlock}>
      <div className={styles.detailTitle}>
        产物
        {reviewTimeout && <span className={styles.reviewBadge}>存疑（复核超限）</span>}
      </div>
      {summary ? (
        <>
          <p className={styles.resultText}>{summary}</p>
          {sources && sources.length > 0 && (
            <p className={styles.hint}>来源引用 {sources.length} 条（详见 meta JSON）</p>
          )}
        </>
      ) : report ? (
        <>
          <ReportView
            report={report}
            title=""
            rawLabel="报告原文（result.report）"
            raw={report.raw}
          />
          {dataSources && dataSources.length > 0 && (
            <p className={styles.hint}>数据源 {dataSources.length} 条（详见 meta JSON）</p>
          )}
        </>
      ) : hasResult ? (
        <pre className={styles.metaPre}>{JSON.stringify(result, null, 2)}</pre>
      ) : (
        <p className={styles.hint}>这次执行没有记录到 result。</p>
      )}
      {/* Phase 9b：报告升成独立资源后的入口（与 Agent 任务详情同一个去处）。
          判据是该任务最新一份报告 id，不是这一屏的 result 形状 —— 认不出 result 形状时
          报告照样可能存在（后端投影看的是 task_run.meta.report，不是这里的 result）。 */}
      {task.report_id && (
        <p className={styles.hint}>
          这次执行产出过报告：<Link className={styles.link} to={`/reports/${task.report_id}`}>看报告全文</Link>
        </p>
      )}
      <details className={styles.metaDetails}>
        <summary className={styles.hint}>执行 meta（workflow_input / result 原文）</summary>
        <pre className={styles.metaPre}>{JSON.stringify(meta ?? {}, null, 2)}</pre>
      </details>
    </div>
  );
}

/* ---------------- 页面主体 ---------------- */

export default function WorkflowPage() {
  // 编目（GET /workflows 直出数组，预置三条起步）
  const [workflows, setWorkflows] = useState<Workflow[] | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  // 详情层：选中哪条编目（null = 编目卡片视图）
  const [selected, setSelected] = useState<Workflow | null>(null);
  // 执行记录（task_type=workflow 的任务列表；后端没有按编目过滤的口径，给全局最近执行）
  const [tasks, setTasks] = useState<AgentTaskSummary[] | null>(null);
  const [tasksError, setTasksError] = useState<string | null>(null);
  const [tasksNonce, setTasksNonce] = useState(0);
  // 执行详情层：正在盯哪次执行（null = 只看表单和记录列表）
  const [taskId, setTaskId] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);

  const { task, failure, halted, retry } = useTaskPolling(taskId);

  // 记录列表跟着被轮询任务的状态翻转走：每翻一次重拉一次列表（nonce+1），
  // 列表行状态滞后不超过一次轮询周期。只在"变化"时拉 —— 每个 2s tick 无脑
  // 重拉是拿 50 行换 1 行的进度，纯浪费；taskId 切换后 task 先归 null，
  // 新任务首读不算翻转，不会触发多余重拉。
  const lastPolledStatusRef = useRef<TaskStatus | null>(null);
  // 评审 MINOR-1 收编：列表拉取后才进详情的场景里，首读没有"前一状态"可比，
  // 行会滞留旧态——进任务后的首个成功读数改为与列表行现状对账，不等补拉一次。
  const entryReconciledRef = useRef<string | null>(null);
  const polledStatus = task?.status ?? null;
  useEffect(() => {
    const prev = lastPolledStatusRef.current;
    lastPolledStatusRef.current = polledStatus;
    if (prev !== null && polledStatus !== null && prev !== polledStatus) {
      setTasksNonce((n) => n + 1);
      return;
    }
    if (taskId && polledStatus !== null && tasks !== null && entryReconciledRef.current !== taskId) {
      // 归属守卫（复审 MINOR-NEW-1）：A→B 直切的那一次 flush 里 task 还是 A 的末态
      // （hook 的 setTask(null) 下一 commit 才生效），不守卫就会拿 A 的状态烧掉
      // B 的首读对账预算。只对"读数确实属于本 taskId"的对账生效。
      if (!task || task.id !== taskId) return;
      entryReconciledRef.current = taskId;
      const row = tasks.find((t) => t.id === taskId);
      if (row && row.status !== polledStatus) setTasksNonce((n) => n + 1);
    }
  }, [taskId, polledStatus, tasks, task]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const rows = await listWorkflows();
        if (!cancelled) setWorkflows(rows);
      } catch (err) {
        if (!cancelled) setCatalogError(toReadableError(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    setTasks(null);
    setTasksError(null);
    void (async () => {
      try {
        const rows = await listAgentTasks({ task_type: 'workflow', limit: 50 });
        if (!cancelled) setTasks(rows);
      } catch (err) {
        if (!cancelled) setTasksError(toReadableError(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selected, tasksNonce]);

  // 提示条定时自隐（同 EvaluationPage）
  useEffect(() => {
    if (!hint) return;
    const timer = window.setTimeout(() => setHint(null), HINT_DURATION);
    return () => window.clearTimeout(timer);
  }, [hint]);

  const openWorkflow = async (workflow: Workflow) => {
    setTaskId(null);
    setSelected(workflow);
    // 详情用 getWorkflow 重取一遍：卡片来自列表，编目可能已被别处改过
    // （input_spec 变了表单就错），进详情是唯一的强一致时机
    try {
      const fresh = await getWorkflow(workflow.id);
      setSelected(fresh);
    } catch {
      // 重取失败不打断浏览：列表项形状相同，凑合用旧数据渲表单
    }
  };

  const backToCatalog = () => {
    setSelected(null);
    setTaskId(null);
  };

  const onTriggered = (newTaskId: string) => {
    setTaskId(newTaskId);
    setTasksNonce((n) => n + 1); // 记录列表里马上能看见这条新执行
    setHint('已触发，正在后台执行…');
  };

  return (
    <div className={styles.page}>
      <div className={styles.container}>
        <header className={styles.header}>
          <h1 className={styles.title}>AI Workflow</h1>
          <p className={styles.subtitle}>
            预置 Workflow 编目 → 填参触发 → 轮询进度（含人工审批断点）。
            执行在后台跑，关掉页面不影响它，回来在执行记录里找。
          </p>
        </header>

        {catalogError && (
          <div className={styles.errorCard}>
            <p className={styles.errorCardText}>{catalogError}</p>
            <button
              type="button"
              className={styles.primaryButton}
              onClick={() => {
                setCatalogError(null);
                void listWorkflows().then(setWorkflows, (err) => setCatalogError(toReadableError(err)));
              }}
            >
              重试
            </button>
          </div>
        )}

        {!workflows && !catalogError && <p className={styles.hint}>正在加载编目…</p>}

        {/* ---- 第一层：编目三卡片 ---- */}
        {workflows && !selected && (
          <div className={styles.cardGrid}>
            {workflows.map((workflow) => (
              <button
                key={workflow.id}
                type="button"
                className={styles.card}
                onClick={() => void openWorkflow(workflow)}
              >
                <span className={styles.cardName}>{workflow.name ?? workflow.graph_key}</span>
                <span className={styles.cardGraph}>{workflow.graph_key}</span>
                <span className={styles.cardDesc}>{workflow.description ?? '（无描述）'}</span>
                <span className={styles.cardSpec}>
                  入参：
                  {Object.keys(workflow.input_spec).length
                    ? Object.entries(workflow.input_spec)
                        .map(([k, v]) => `${k}(${v})`)
                        .join('、')
                    : '无'}
                </span>
                {!workflow.is_active && <span className={styles.cardOff}>已停用</span>}
              </button>
            ))}
          </div>
        )}

        {/* ---- 第二层：详情（触发表单 + 执行记录） ---- */}
        {selected && (
          <section className={styles.section}>
            <div className={styles.sectionTitle}>
              {selected.name ?? selected.graph_key}
              <button type="button" className={styles.actionButton} onClick={backToCatalog}>
                返回编目
              </button>
            </div>
            <p className={styles.hint}>{selected.description ?? '（无描述）'}</p>
            <TriggerForm workflow={selected} onTriggered={onTriggered} />

            <div className={styles.detailTitle}>执行记录（workflow 家族最近 50 条）</div>
            {hint && <p className={styles.successBanner}>{hint}</p>}
            {tasksError && (
              <div className={styles.errorBanner}>
                {tasksError}
                <button
                  type="button"
                  className={styles.actionButton}
                  onClick={() => setTasksNonce((n) => n + 1)}
                >
                  重试
                </button>
              </div>
            )}
            {!tasks && !tasksError && <p className={styles.hint}>正在加载执行记录…</p>}
            {tasks && tasks.length === 0 && <p className={styles.hint}>还没有执行记录。</p>}
            {tasks && tasks.length > 0 && (
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th>任务</th>
                    <th>状态</th>
                    <th>创建时间</th>
                  </tr>
                </thead>
                <tbody>
                  {tasks.map((row) => (
                    <tr
                      key={row.id}
                      className={`${styles.clickableRow} ${row.id === taskId ? styles.selectedRow : ''}`}
                      onClick={() => setTaskId(row.id)}
                    >
                      <td className={styles.cellClamp} title={row.question}>
                        {row.title ?? row.question}
                      </td>
                      <td>
                        <StatusBadge status={row.status} />
                      </td>
                      <td className={styles.cellMuted}>{formatTime(row.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        )}

        {/* ---- 第三层：执行详情（badge + 步骤条 + 审批区） ---- */}
        {selected && taskId && (
          <TaskDetailPanel
            taskId={taskId}
            task={task}
            failure={failure}
            halted={halted}
            retry={retry}
            retryAfterDecision={() => retry({ fromDecision: true })}
            onBack={() => setTaskId(null)}
          />
        )}
      </div>
    </div>
  );
}
