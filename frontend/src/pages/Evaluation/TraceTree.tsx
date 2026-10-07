/**
 * Trace 下钻（docs/08 §8）：评测不复制执行痕迹，结果行只存 task_run_id，
 * 点用例时现拉 GET /agents/task-runs/{run_id}/trace（跨页共享走 api/agent.ts，
 * Phase 9a Task 3 兑现 evaluation.ts 头注释的"Agent 页落地时把 getRunTrace 移过去"）。
 *
 * 范围克制（YAGNI，brief Step 4 点名）：按 started_at 排成缩进列表，
 * 能看清"这次执行依次跑了哪些节点、各花多久"即可 ——
 * 不做时间轴缩放、不做节点折叠（那些归 Task 7 的真树组件）。
 *
 * 命名撞车登记（Phase 9a 裁定）：本文件与 Task 7 落地的 components/TraceTree.tsx
 * 同名不同物 —— 本文件是评测页的**平铺**列表（保持既有行为等价：只渲 agent 节点），
 * Task 7 那个才是真树。9b 再让评测下钻复用真树组件，届时本文件退休（记入 Task 12 文档注记）。
 * 耗时展示自 Task 7 起也改取 lib/format 的 formatDuration（本页因此多出「N 分 M 秒」一档）。
 */
import { useCallback, useEffect, useState } from 'react';

import { toReadableError } from '../../api/client';
import { getTaskTrace } from '../../api/agent';
import type { TraceNode, TaskTrace } from '../../api/agent';
import { formatDuration } from '../../lib/format';
import styles from './EvaluationPage.module.css';

export default function TraceTree({ taskRunId }: { taskRunId: string | null }) {
  const [trace, setTrace] = useState<TaskTrace | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!taskRunId) return;
    setLoading(true);
    setError(null);
    setTrace(null);
    try {
      setTrace(await getTaskTrace(taskRunId));
    } catch (err) {
      setError(toReadableError(err));
    } finally {
      setLoading(false);
    }
  }, [taskRunId]);

  // taskRunId 变化即重拉（换了用例就不该看到上一条的 span）
  useEffect(() => {
    void load();
  }, [load]);

  if (!taskRunId) {
    return (
      <p className={styles.hint}>
        点下方失败用例行里的「查看 Trace」，这里会渲出该用例真实执行的节点序列。
      </p>
    );
  }
  if (loading) return <p className={styles.hint}>正在加载 Trace…</p>;
  if (error) {
    return (
      <div className={styles.errorBanner}>
        {error}
        <button type="button" className={styles.actionButton} onClick={() => void load()}>
          重试
        </button>
      </div>
    );
  }
  if (!trace) return null;

  // 后端已按 started_at 升序平铺返回（agent_task_service.merge_trace_nodes），这里不再重排；
  // 但保持与既有行为等价：评测下钻原本就只列 agent span（工具节点属于节点内部细节），
  // 换形状后依旧过滤 kind === 'agent'。真树在 Task 7 的 components/TraceTree 里给。
  const nodes: TraceNode[] = trace.nodes.filter((node) => node.kind === 'agent');
  if (nodes.length === 0) {
    return <p className={styles.hint}>这次执行没有记录到节点。</p>;
  }

  return (
    <ol className={styles.traceList}>
      {nodes.map((node, index) => (
        <li key={node.span_id} className={styles.traceNode}>
          <span className={styles.traceIndex}>{index + 1}</span>
          <span className={styles.traceName}>{node.name}</span>
          <span
            className={`${styles.traceStatus} ${
              node.status === 'ok' ? styles.traceStatusOk : styles.traceStatusError
            }`}
            title={node.error_message ?? undefined}
          >
            {node.status}
          </span>
          <span className={styles.traceMeta}>{formatDuration(node.duration_ms)}</span>
          <span className={styles.traceMeta}>{node.total_tokens} tok</span>
          {node.model && <span className={styles.traceMeta}>{node.model}</span>}
        </li>
      ))}
    </ol>
  );
}
