/**
 * 报告域 API（Phase 9b，docs/06 §2.5 的三条路由）。
 *
 * 类型与后端 `schemas/report.py` 一一对应，字段保持 snake_case（约定同其他 api 模块）。
 *
 * 两处**必须写下来的形状差异**（别的列表口不是这样，照老习惯写会炸）：
 *   1. `GET /reports` 回的是 `{items, total}` **信封**，不是裸数组。
 *      全仓其他列表口（conversations / documents / agents/tasks / evaluations）
 *      都是裸数组；本口破例是为了拿总数（列表页要显示「共 N 份」，裸数组拿不到，
 *      拿当页长度当总数就是撒谎）。后端 schema docstring 与 docs/06 §7.1 都登记了这处破例。
 *   2. `GET /reports/{id}` 同时给 `content`（结构化七字段）与 `markdown`（后端渲染的全文）。
 *      `markdown` 为 null **不是**"没有报告"，而是"后端认不出这个形状、渲不出来" ——
 *      此时前端退回按 `content` 渲染（`lib/taskReport.ts` 的归一正是干这个的）。
 *      详情页两个都渲：正文看结构化分节，原文折叠区给库里的 markdown。
 *
 * 没有创建口：报告的写入方是执行链路（终态落库时投影一行）。
 * 手工 POST 一份没有来源执行的报告 = 凭空造交付物，见后端 api/reports.py 头注。
 */
import { request } from './client';
// 时间窗的键型与 /stats 共用一份：后端两个端点接的是同一个 `RangeKey` 声明，
// 前端各写各的字面量联合就会让"加一档只改一边"变成可能（对账针见 ranges.ts 头注）。
import type { StatsRange } from './stats';

/** 报告摘要（ReportOut，列表项）。**不含正文** —— 列表拖着 N 份正文走是胖响应。 */
export interface ReportSummary {
  id: string;
  title: string;
  /** analysis / workflow / summary（由图类型映射，不是 task_type） */
  report_type: string;
  /** final / draft —— 今天只有 final（草稿态没有写入方） */
  status: string;
  reviewer_verdict: string | null;
  total_tokens: number;
  cost: number;
  /** 来源（可空：来源任务被删后 SET NULL，报告本身仍在） */
  task_id: string | null;
  task_run_id: string | null;
  created_at: string;
  updated_at: string;
}

/** 报告详情（ReportDetailOut）：摘要 + 正文两形 */
export interface ReportDetail extends ReportSummary {
  /** 结构化正文（七字段，形状随产品演进 —— 用 unknown 收，归一器负责认） */
  content: Record<string, unknown>;
  /** 后端渲染好的 Markdown 全文；null = 认不出形状，前端退回按 content 渲 */
  markdown: string | null;
}

/** 列表响应信封（ReportListOut）。破例形状，见文件头注。 */
export interface ReportListResult {
  items: ReportSummary[];
  total: number;
}

/** 列表参数。`range` 与 /stats 同一档词表（`lib/ranges.ts` 的 `RANGE_TABS`）。
 *  写成 `type` 而不是 `interface`：后者的隐式索引签名缺失，塞不进 `request` 的
 *  `params: Record<string, string | number | undefined>`（TS2322）。 */
export type ReportListParams = {
  report_type?: string;
  /** 时间窗，默认 'all'（后端默认值）；窗口算法在后端 stats_repo.range_start */
  range?: StatsRange;
  limit?: number;
  offset?: number;
};

/** 报告列表：本组织（member 只见自己的）最近创建在前。 */
export function listReports(params: ReportListParams = {}): Promise<ReportListResult> {
  return request<ReportListResult>('/reports', { params });
}

/** 报告详情。跨 org / 跨 user / 不存在都是同一个 404（REPORT_404001）。 */
export function getReport(reportId: string): Promise<ReportDetail> {
  return request<ReportDetail>(`/reports/${reportId}`);
}

/** 删除报告（204 无 body）。只删报告行，来源任务与 Trace 不动。 */
export function deleteReport(reportId: string): Promise<void> {
  return request<void>(`/reports/${reportId}`, { method: 'DELETE' });
}
