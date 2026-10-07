/**
 * Trace 树渲染（docs/07 §8）。组树在 lib/traceTree.ts（纯函数，零 React），这里只负责：
 * 缩进 + kind 标记 + 耗时/Token/成本 + 失败红框 + 父链警告标 + 点选展开详情。
 *
 * 与 docs/07 §8 的两处偏离（spec §0 裁定② / §9 登记）：
 *   时间线双视图不做（后端无 llm 级 span）；增量来自轮询而不是 SSE span 事件。
 */
import { useMemo, useState } from 'react';

import type { TraceNode } from '../api/agent';
import { buildTraceTree, flattenTree } from '../lib/traceTree';
import { formatCost, formatDuration } from '../lib/format';
import styles from './TraceTree.module.css';

/** 节点类型标记：字母徽标而不是图标字体（本仓不引图标库，NavIcon 是手绘 SVG） */
function kindLabel(kind: TraceNode['kind']): string {
  return kind === 'agent' ? 'A' : 'T';
}

export default function TraceTree({ nodes, currency }: { nodes: TraceNode[]; currency: string }) {
  const [selected, setSelected] = useState<string | null>(null);
  const flat = useMemo(() => flattenTree(buildTraceTree(nodes)), [nodes]);
  const selectedNode = flat.find((item) => item.tree.node.span_id === selected)?.tree.node ?? null;

  if (flat.length === 0) {
    return <p className={styles.hint}>这次执行还没有记录到节点。</p>;
  }

  return (
    <div className={styles.wrapper}>
      <ol className={styles.rows}>
        {flat.map(({ tree, ancestorFailed }) => {
          const node = tree.node;
          const failed = node.status === 'error';
          return (
            <li key={node.span_id}>
              <button
                type="button"
                className={[
                  styles.row,
                  failed ? styles.rowError : '',
                  selected === node.span_id ? styles.rowOn : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
                style={{ marginLeft: tree.depth * 18 }}
                onClick={() => setSelected((prev) => (prev === node.span_id ? null : node.span_id))}
              >
                <span className={styles.kind} title={node.kind}>{kindLabel(node.kind)}</span>
                <span className={styles.name}>{node.name}</span>
                {tree.orphan && <span className={styles.orphanTag} title="父节点不在本次记录里，已按根节点展示">父缺失</span>}
                {ancestorFailed && <span className={styles.warnTag} title="这条记录的上游有失败节点">上游失败</span>}
                <span className={failed ? styles.statusBad : styles.status}>{node.status}</span>
                <span className={styles.meta}>{formatDuration(node.duration_ms)}</span>
                <span className={styles.meta}>{node.total_tokens} tok</span>
                <span className={styles.meta}>{formatCost(node.cost, currency)}</span>
              </button>
            </li>
          );
        })}
      </ol>

      {selectedNode && (
        <section className={styles.detail}>
          <h3 className={styles.detailTitle}>
            {selectedNode.name}
            {selectedNode.model ? ` · ${selectedNode.model}` : ''}
          </h3>
          <dl className={styles.detailList}>
            <div><dt>开始</dt><dd>{new Date(selectedNode.started_at).toLocaleString()}</dd></div>
            <div><dt>结束</dt><dd>{selectedNode.finished_at ? new Date(selectedNode.finished_at).toLocaleString() : '—'}</dd></div>
            <div><dt>耗时</dt><dd>{formatDuration(selectedNode.duration_ms)}</dd></div>
            <div><dt>成本</dt><dd>{formatCost(selectedNode.cost, currency)}</dd></div>
            <div>
              <dt>输出摘要</dt>
              <dd>{selectedNode.summary ?? '（无摘要）'}</dd>
            </div>
            {selectedNode.error_message && (
              <div><dt>错误</dt><dd className={styles.detailError}>{selectedNode.error_message}</dd></div>
            )}
          </dl>
        </section>
      )}
    </div>
  );
}
