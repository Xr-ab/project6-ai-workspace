/**
 * 分页算式（Phase 9b 报告列表的翻页器）。
 *
 * 为什么抽成纯函数而不是写在组件里（与 `lib/traceTree.ts`、`lib/taskReport.ts` 同一条纪律）：
 * 翻页器看着人畜无害，实际每一支都会静默出错 ——「第 999 页」跳到空列表、
 * 输入框里打 `abc` 跳成第 1 页、`total` 为 0 时算出 `maxPage=0` 再让按钮永远禁用。
 * 这些都不会报错，只会让用户以为"没有更多数据了"。抽到 `lib/` 后 vitest
 * （environment: 'node'）能零 DOM 直接断言，收据可重跑。
 *
 * 一条不骗人的规矩：**认不出的输入返回 null，不猜**。跳页框打了 `abc` 就什么也不做
 * （停在当前页），而不是"顺手跳回第 1 页"——后者会把用户正在看的那一页弄丢，
 * 还让他以为是自己点错的。
 */

/** 总共几页。`total=0` 也是 1 页（列表页要显示"第 1 / 1 页"，0 页是个不存在的页码）。 */
export function pageCount(total: number, pageSize: number): number {
  if (pageSize <= 0) return 1;
  return Math.max(1, Math.ceil(total / pageSize));
}

/** 把任意页码收进 [1, page_count] —— 首页/末页/上一页/下一页四条按钮都经这里。 */
export function clampPage(page: number, pages: number): number {
  return Math.min(Math.max(1, Math.floor(page)), Math.max(1, pages));
}

/** 页码 → 请求用的 offset。 */
export function offsetForPage(page: number, pageSize: number): number {
  return (clampPage(page, Number.MAX_SAFE_INTEGER) - 1) * pageSize;
}

/**
 * 跳页输入框的原始文本 → 目标页码；不是"一个 1..pages 的整数"就返回 null。
 *
 * 刻意**不做夹带**：`999`（超出末页）与 `0`/`-3`/`2.5`/`abc`/空串一律返回 null。
 * 为什么不夹到末页：夹带会"看起来能用"，但用户输入的和实际跳到的不是同一页 ——
 * 有末页按钮在，跳不到不存在的那一页是事实而不是故障，如实不响应比替他猜诚实。
 */
export function parseJumpPage(raw: string, pages: number): number | null {
  const text = raw.trim();
  if (!/^\d+$/.test(text)) return null;
  const page = Number(text);
  return page >= 1 && page <= Math.max(1, pages) ? page : null;
}
