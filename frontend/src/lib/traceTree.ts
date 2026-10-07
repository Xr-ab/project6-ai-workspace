/**
 * Trace 组树（Phase 9a，spec §4.2：纯函数、零 React、可脱离 DOM 断言）。
 *
 * 输入是后端 docs/06 §4 的扁平 nodes（带 parent_span_id），输出是渲染用的嵌套树。
 * 为什么组树要抽出来：孤儿与环是最容易出错的两块，而它们**不该**住在渲染组件里——
 * 住在组件里就只能靠浏览器手点复现，抽成纯函数才能用 node 直接断言（scratch/p9a_trace_tree_harness.mjs）。
 *
 * 住在 `lib/` 而不是 plan 原定的 `components/`（Task 7 落地勘误）：渲染组件叫
 * `components/TraceTree.tsx`，两者只差首字母大小写，而 Windows/macOS 的文件系统大小写不敏感——
 * `import '../../components/TraceTree'` 会先探到 `components/traceTree.ts`（`.ts` 早于 `.tsx`），
 * 于是 tsc 报「has no default export / differs only in casing」，编译不过。同目录并存这两个名字
 * 只在区分大小写的 Linux 上成立。挪到 `lib/` 既解掉撞名，也和"零 React 的纯逻辑归 lib/（format.ts）、
 * 可脱离 DOM 断言的核单独一个文件（hooks/pollCycle.ts）"的既有做法一致。
 *
 * 三条策略（都是"宁可难看也不骗人"）：
 * - 根 = parent_span_id 为 null；
 * - 孤儿（声称有父但父不在本批，例如 worker 崩在半路只落了子）**提升为根并标 orphan**，
 *   绝不静默丢：丢了就是"树里没有这一步"，用户会读成"没跑这一步"；
 * - 环（父指针互相指向，理论不该出现，落库 bug 会造出来）同理提升为 orphan 根，
 *   断环的方式是「环上的每个节点都成为根」——背边因此全部断掉，剩下的挂载关系必是无环森林，
 *   所以 `flattenTree` 的递归不会转圈。**只提「自己在环上」的节点**：父链只是伸进环的正常子孙
 *   （如 c→a、a↔b 里的 c）不提成根，仍挂在已被提为根的 a 下面（R45：这是 Step 1
 *   第 4 组那条 `orphan 集合 === 'a,b'` 的断言钉死的挂法，两种挂法渲出来都看得见，但只有这一种过针）。
 */
import type { TraceNode } from '../api/agent';

export interface TreeNode {
  node: TraceNode;
  children: TreeNode[];
  depth: number;
  /** true = 它声称有父，但父不在本批 / 父链成环，被提升为根 */
  orphan: boolean;
}

export interface FlatNode {
  tree: TreeNode;
  /** 祖先链上（不含自己）有 status==='error' 的节点 → 渲染时挂警告标 */
  ancestorFailed: boolean;
}

/** 这个节点自己在不在环上：从它的父开始上溯，绕回自己 = 在环上。用循环不用递归，栈安全。
 *  三种「不是环」都判 false：撞到不在本批的父（`parentOf` 查不到 → null，那是孤儿面，
 *  由 brokenParent 分支处理）、链走到真根（`parent_span_id === null`，正常的深节点）、
 *  **步数预算用尽**（上限 = 节点数；链比节点数还长就必然在绕环，但绕的是**别人的**环、
 *  并没有回到自己，所以它是环的正常子孙，不该被提成根）。
 *  （R61 勘误——原来这里写「步数上限 = 节点数，超预算一律按环处理」，和函数末尾的
 *  `return false` 正好说反话。代码是对的（第 4 组 `orphan 集合 === 'a,b'` 那枚针就靠它；
 *  评审的随机探针拿 `x→m→a↔b` 实测只把 a/b 提成根）。留着反话，后人会照着注释把代码改回去。）
 *  （R45 勘误——原片段是 `leadsToCycle`，`seen` 集一撞就 `return true`，而循环自然结束（即链走到
 *  真根）时也落到函数末尾的 `return true`：**每一个父链正常的子节点都会被误判成环**，被提升为
 *  orphan 根。那会让本 Task 的 Step 1 第 1 组「正常节点不标 orphan」、第 3 组「多根都保留」、
 *  第 6 组「失败节点的子孙都带警告标」三条同时红——计划自带的测试否证计划自带的实现，
 *  属于「派活前就该发现」的那一类，故在此改正。判据同时收窄为「绕回**自己**」而不是
 *  「撞到任何重访」，否则 c→a（a↔b 的子孙）也会被提为根，与第 4 组 `orphan 集合 === 'a,b'` 矛盾。） */
function isOnCycle(
  node: TraceNode,
  parentOf: Map<string, string | null>,
  total: number,
): boolean {
  let cur = node.parent_span_id;
  for (let steps = 0; cur !== null && steps <= total; steps += 1) {
    if (cur === node.span_id) return true;
    cur = parentOf.get(cur) ?? null; // 父不在本批 → 链到头，不是环
  }
  return false;
}

export function buildTraceTree(nodes: TraceNode[]): TreeNode[] {
  const wrapped = new Map<string, TreeNode>();
  for (const node of nodes) {
    wrapped.set(node.span_id, { node, children: [], depth: 0, orphan: false });
  }
  // 只有"真的存在这个 span"才算合法父指针：自指与指向别的批次的 id 都进 notFound 分支
  const parentOf = new Map<string, string | null>();
  for (const node of nodes) parentOf.set(node.span_id, node.parent_span_id);

  const roots: TreeNode[] = [];
  const asRoot = new Set<string>();
  for (const node of nodes) {
    const tree = wrapped.get(node.span_id)!;
    const parent = node.parent_span_id ? wrapped.get(node.parent_span_id) : undefined;
    const brokenParent =
      node.parent_span_id !== null && (parent === undefined || parent === tree);
    if (!node.parent_span_id || brokenParent || isOnCycle(node, parentOf, nodes.length)) {
      tree.orphan = node.parent_span_id !== null; // 真空根不叫孤儿
      roots.push(tree);
      asRoot.add(node.span_id);
    }
  }
  for (const node of nodes) {
    if (asRoot.has(node.span_id)) continue;
    const parent = wrapped.get(node.parent_span_id!);
    parent!.children.push(wrapped.get(node.span_id)!);
  }

  const sortRec = (list: TreeNode[]) => {
    list.sort((a, b) => a.node.started_at.localeCompare(b.node.started_at));
    list.forEach((child) => sortRec(child.children));
  };
  sortRec(roots);

  const depthRec = (list: TreeNode[], depth: number) => {
    for (const item of list) {
      item.depth = depth;
      depthRec(item.children, depth + 1);
    }
  };
  depthRec(roots, 0);
  return roots;
}

/** 前序摊平给渲染层（缩进列表比递归 JSX 好写也好测）。 */
export function flattenTree(roots: TreeNode[]): FlatNode[] {
  const out: FlatNode[] = [];
  const walk = (list: TreeNode[], ancestorFailed: boolean) => {
    for (const item of list) {
      out.push({ tree: item, ancestorFailed });
      walk(item.children, ancestorFailed || item.node.status === 'error');
    }
  };
  walk(roots, false);
  return out;
}

/** 根 Agent 节点的墙钟求和（Trace 汇总卡用）。
 *  只算 parent_span_id === null 的 agent 节点：嵌套 span 的子节点时间已被父节点罩住，
 *  全量求和会重复计时。实况今天没有 agent 侧父子链接（写入器只给 tool 行写 parent），
 *  这道过滤今天等价于全量——它防的是 9b 引入 llm/workflow 级嵌套 span 之后的失真。
 *  任一根节点缺 duration_ms 时返回 null（不可得的数不许用 0 顶，见 lib/format.ts 的同一规矩）。 */
export function sumRootAgentDurationMs(nodes: TraceNode[]): number | null {
  let sum = 0;
  for (const node of nodes) {
    if (node.kind !== 'agent' || node.parent_span_id !== null) continue;
    if (node.duration_ms === null) return null;
    sum += node.duration_ms;
  }
  return sum;
}
