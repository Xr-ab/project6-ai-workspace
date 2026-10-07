/** 展示层的两处格式化（Phase 9a 起，Task 6–10 的新页从这里取，不再各抄一份）。
 *  收敛进度（Task 7 之后）：`pages/Evaluation/TraceTree.tsx` 与 `pages/Workflow/WorkflowPage.tsx`
 *  各自那份逐字相同的 formatDuration（只有 ms / s 两档、无分钟档）已删除、改为 import 本模块，
 *  所以**这两页现在 ≥60s 会渲成「N 分 M 秒」而不是「120.0 s」**——口径以本模块为准（Task 7 brief
 *  Step 5 的裁定）。仍在原地的只剩 `pages/Evaluation/EvaluationPage.tsx` 里的 formatCost
 *  （收非空 number、币种写死 ¥，服务的是指标格不是单价格）：改它等于改已经验收过的页面的
 *  渲染文案，而重验那一页不在 9a 的针面里，故登记为已知重复（Task 12 记账）。 */

/** 耗时：<1s 留整数 ms，<60s 换算秒，其余分/秒；null 显示破折号而不是 0。
 *  已知边界（R51 裁定**不改**）：59_950~59_999ms 会渲成「60.0 s」而不是分钟档——
 *  这不是缺陷：按分钟档走会得到「0 分 60 秒」，比 60.0 s 更难读。后到的一档要用前一档的
 *  **取整值**判，改法是给 seconds 先 round 再比，代价是 59.96s 这类读起来一样，故留现状。 */
export function formatDuration(ms: number | null): string {
  if (ms === null) return '—';
  if (ms < 1000) return `${ms} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const mins = Math.floor(seconds / 60);
  return `${mins} 分 ${Math.round(seconds % 60)} 秒`;
}

/** 成本：value 传 null/undefined = 这个数压根不可得（如 `PricingOut.input_price_per_1k` 在
 *  未配置单价时就是 null），显示「未配置单价」而不是 ¥0.0000。
 *  注意与 0 区分：单价配好、这条 run 没花 token，那是 ¥0.0000，是诚实的数字。 */
export function formatCost(value: number | null | undefined, currency: string): string {
  if (value === null || value === undefined) return '未配置单价';
  const sign = currency === 'CNY' ? '¥' : currency === 'USD' ? '$' : `${currency} `;
  return `${sign}${value.toFixed(4)}`;
}

/** Trace 节点 / 任务详情的成本币种。trace 契约里只有 `cost: float`、没有币种字段
 *  （docs/06 §4 的 TraceNodeOut），单条 run 也拿不到 `/stats/usage` 那个聚合口径的 currency。
 *  所以落一个具名常量、不散落字面量：换展示币种时全仓 grep `TRACE_COST_CURRENCY` 这个名字一起改
 *  （R42 勘误：原计划这里写的是「改 `PRICING_CURRENCY` 时全仓 grep 这个名字」——前端没有这个名字，
 *  grep 它零命中。真旋钮是后端 `settings.llm_price_currency`（`app/core/config.py` 的默认值 `CNY`），
 *  它只经 `/stats/*` 的 `currency` 字段出网，trace 载荷里没有，所以详情页这一处只能靠具名常量兜。
 *  这个局限由 Task 12 在 docs/07 §5.6（Task Detail / Trace）成文（R43：07 的 §5.5 是 Agent Tasks，
 *  §5.6 才是 Task Detail——本节引用一律按 07 实况编号）。 */
export const TRACE_COST_CURRENCY = 'CNY';
