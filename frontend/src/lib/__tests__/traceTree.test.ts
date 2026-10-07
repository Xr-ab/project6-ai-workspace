/** buildTraceTree / flattenTree / sumRootAgentDurationMs（spec §5.2 的孤儿 / 乱序 / 环三面）。
 *
 *  这三条策略在 lib/traceTree.ts 的 docstring 里写得很细，而注释是给人读的、不是证据：
 *  R45 / R61 两次勘误都是「注释说反话而代码是对的」，靠一条能跑的针才分得开。
 *  尤其第 4 组：`orphan 集合 === {'a','b'}`（只提「自己在环上」的节点，父链伸进环的
 *  正常子孙 c 不提成根）是 R45 收窄判据的那枚钉子，改回「撞到任何重访」它就红。
 */
import { describe, expect, it } from 'vitest';

import { buildTraceTree, flattenTree, sumRootAgentDurationMs } from '../traceTree';
import type { TraceNode } from '../../api/agent';

/** 造一个合法 TraceNode：契约里 13 个字段全是必填（api/agent.ts:85-102 实读），
 *  所以这里收 Partial 再补齐，用例只写自己关心的那几个字段。 */
function node(over: Partial<TraceNode> & Pick<TraceNode, 'span_id'>): TraceNode {
  return {
    kind: 'agent',
    parent_span_id: null,
    name: over.span_id,
    status: 'ok',
    duration_ms: 10,
    total_tokens: 0,
    cost: 0,
    model: null,
    summary: null,
    error_message: null,
    started_at: '2026-10-01T00:00:00Z',
    finished_at: null,
    ...over,
  };
}

const ids = (list: TraceNode[]) => list.map((n) => n.span_id);

describe('buildTraceTree', () => {
  it('正常父子：父为根、子挂其下，都不标 orphan，depth 逐级 +1', () => {
    const roots = buildTraceTree([
      node({ span_id: 'a' }),
      node({ span_id: 'b', parent_span_id: 'a', started_at: '2026-10-01T00:00:01Z' }),
    ]);
    expect(roots.map((r) => r.node.span_id)).toEqual(['a']);
    expect(roots[0].orphan).toBe(false);
    expect(roots[0].children[0].node.span_id).toBe('b');
    expect(roots[0].children[0].depth).toBe(1);
    expect(roots[0].children[0].orphan).toBe(false);   // 有父且父在场 = 不是孤儿
  });

  it('孤儿（声称有父但父不在本批）提升为根并标 orphan，绝不静默丢', () => {
    const roots = buildTraceTree([
      node({ span_id: 'a' }),
      node({ span_id: 'c', parent_span_id: 'missing', started_at: '2026-10-01T00:00:02Z' }),
    ]);
    expect(roots.map((r) => r.node.span_id)).toEqual(['a', 'c']);
    const c = roots.find((r) => r.node.span_id === 'c')!;
    expect(c.orphan).toBe(true);
  });

  it('同级按 started_at 升序（后端给了乱序数组也要渲对）', () => {
    const roots = buildTraceTree([
      node({ span_id: 'late', started_at: '2026-10-01T00:00:09Z' }),
      node({ span_id: 'early', started_at: '2026-10-01T00:00:00Z' }),
      node({ span_id: 'mid', started_at: '2026-10-01T00:00:05Z' }),
    ]);
    expect(ids(roots.map((r) => r.node))).toEqual(['early', 'mid', 'late']);
  });

  it('环 a↔b：环上每个节点都成为 orphan 根，父链伸进环的子孙 c 仍挂在 a 下面且不标 orphan', () => {
    // R45 钉死的挂法：两种渲法都"看得见"，但只有这一种过针。
    const roots = buildTraceTree([
      node({ span_id: 'a', parent_span_id: 'b' }),
      node({ span_id: 'b', parent_span_id: 'a' }),
      node({ span_id: 'c', parent_span_id: 'a', started_at: '2026-10-01T00:00:03Z' }),
    ]);
    const rootIds = new Set(roots.map((r) => r.node.span_id));
    expect(rootIds).toEqual(new Set(['a', 'b']));         // c 不在里面
    const orphans = roots.filter((r) => r.orphan).map((r) => r.node.span_id).sort();
    expect(orphans).toEqual(['a', 'b']);
    const a = roots.find((r) => r.node.span_id === 'a')!;
    expect(ids(a.children.map((ch) => ch.node))).toEqual(['c']);
    expect(a.children[0].orphan).toBe(false);
    // 断环后必是无环森林：flatten 不会转圈（3 个节点各出现一次，有限步终止）
    expect(flattenTree(roots)).toHaveLength(3);
  });

  it('自指节点（parent_span_id === 自己）也是孤儿根，不递归死循环', () => {
    const roots = buildTraceTree([node({ span_id: 'x', parent_span_id: 'x' })]);
    expect(roots).toHaveLength(1);
    expect(roots[0].orphan).toBe(true);
  });

  it('空输入给空树（不是 undefined，渲染层直接 .map）', () => {
    expect(buildTraceTree([])).toEqual([]);
  });
});

describe('flattenTree', () => {
  it('前序：父在子之前，深链按 a,b,c 出', () => {
    const roots = buildTraceTree([
      node({ span_id: 'a' }),
      node({ span_id: 'b', parent_span_id: 'a', started_at: '2026-10-01T00:00:01Z' }),
      node({ span_id: 'c', parent_span_id: 'b', started_at: '2026-10-01T00:00:02Z' }),
    ]);
    expect(ids(flattenTree(roots).map((f) => f.tree.node))).toEqual(['a', 'b', 'c']);
  });

  it('ancestorFailed：祖先链上有 error 的子节点带警告标，自己 error 不算自己失败祖先', () => {
    const roots = buildTraceTree([
      node({ span_id: 'a', status: 'error' }),
      node({ span_id: 'b', parent_span_id: 'a', status: 'ok', started_at: '2026-10-01T00:00:01Z' }),
      node({ span_id: 'c', parent_span_id: 'b', status: 'ok', started_at: '2026-10-01T00:00:02Z' }),
      node({ span_id: 'd', status: 'ok', started_at: '2026-10-01T00:00:03Z' }),
    ]);
    const flags = new Map(flattenTree(roots).map((f) => [f.tree.node.span_id, f.ancestorFailed]));
    expect(flags.get('a')).toBe(false);   // 自己 error，但祖先链空
    expect(flags.get('b')).toBe(true);    // 父 a 是 error
    expect(flags.get('c')).toBe(true);    // 祖父 a 的 error 一路传下来
    expect(flags.get('d')).toBe(false);
  });
});

describe('sumRootAgentDurationMs', () => {
  it('只算 parent_span_id === null 的 agent 根（嵌套 span 不重复计时）', () => {
    const nodes = [
      node({ span_id: 'a', kind: 'agent', duration_ms: 100 }),
      node({ span_id: 'b', kind: 'agent', parent_span_id: 'a', duration_ms: 40 }),
      node({ span_id: 't', kind: 'tool', parent_span_id: 'a', duration_ms: 30 }),
      node({ span_id: 'a2', kind: 'agent', duration_ms: 5 }),
    ];
    expect(sumRootAgentDurationMs(nodes)).toBe(105);
  });

  it('任一根缺 duration_ms 就返回 null（不可得的数不许用 0 顶，与 format.ts 同一规矩）', () => {
    const nodes = [
      node({ span_id: 'a', duration_ms: 100 }),
      node({ span_id: 'b', duration_ms: null }),
    ];
    expect(sumRootAgentDurationMs(nodes)).toBeNull();
  });

  it('没有 agent 根 ⇒ 0 而不是 null（0 是"确实没跑过根节点"这个真事实）', () => {
    expect(sumRootAgentDurationMs([node({ span_id: 't', kind: 'tool' })])).toBe(0);
  });
});
