/**
 * 只读统计面（Phase 9a，契约见 docs/06 §2.8）。
 * 页面不拼 URL；类型内联在本模块（惯例同 api/agent.ts —— types/ 只装跨页共享的 chat/document）。
 *
 * 口径四条（后端 stats_repo 模块头是唯一定义源，这里只照形状收）：
 *   scope（admin=本组织 / member=只自己）、评测排除、滚动窗、成功率分母 completed/(completed+failed)。
 */
import { request } from './client';
import { parseTaskStatus, type AgentTaskSummary } from './agent';
// R40：recent_conversations 的六字段壳 `types/chat.ts::Conversation` 已经有了（逐字同形，
// 且 `api/chat.ts:4` 就是这样引的）——这里再内联一份就是同一契约两处定义，改一边忘一边。
import type { Conversation } from '../types/chat';

export type StatsRange = 'today' | 'week' | 'month' | 'all';
export type StatsGroupBy = 'task_type' | 'model' | 'day';
export type StatsScope = 'org' | 'personal';

export interface SuccessBasis {
  completed: number;
  failed: number;
}

export interface OverviewCards {
  task_total: number;
  /** null = 这区间没有终态行（分母 0），渲染成「—」而不是 0% */
  success_rate: number | null;
  success_basis: SuccessBasis;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_cost: number;
  currency: string;
  /** false 时 total_cost 恒 0 且不可解释：要显示「未配置单价」 */
  pricing_configured: boolean;
}

export interface StatsOverview {
  range: StatsRange;
  scope: StatsScope;
  cards: OverviewCards;
  recent_tasks: AgentTaskSummary[];
  /** R40：直接用 `types/chat.ts::Conversation`（六字段逐字同形），不在这里内联第二份契约 */
  recent_conversations: Conversation[];
}

export interface UsageGroup {
  key: string;
  tasks: number;
  total_tokens: number;
  total_cost: number;
}

/** 单价摘要：**这里没有也不该有密钥字段**（后端 PricingOut 是手写白名单） */
export interface PricingSummary {
  llm_model: string;
  embedding_model: string;
  embedding_dim: number;
  currency: string;
  pricing_configured: boolean;
  input_price_per_1k: number | null;
  output_price_per_1k: number | null;
}

export interface StatsUsage {
  range: StatsRange;
  scope: StatsScope;
  group_by: StatsGroupBy;
  groups: UsageGroup[];
  total: UsageGroup;
  pricing: PricingSummary;
}

/** 原始壳：status 在 JSON 里只是 string，recent_tasks 过 parseTaskStatus 才进窄类型
 *  （与 api/agent.ts 同一个边界，未知状态照样当场抛，页面不会渲出假状态）。
 *  括号必须打在交叉类型外面——`A & {…}[]` 会被解析成 `A & ({…}[])`，元素类型反而没被改 */
interface RawStatsOverview extends Omit<StatsOverview, 'recent_tasks'> {
  recent_tasks: (Omit<AgentTaskSummary, 'status'> & { status: string })[];
}

export async function getOverview(range: StatsRange): Promise<StatsOverview> {
  const raw = await request<RawStatsOverview>('/stats/overview', { params: { range } });
  return {
    ...raw,
    recent_tasks: raw.recent_tasks.map((t) => ({ ...t, status: parseTaskStatus(t.status) })),
  };
}

export function getUsage(range: StatsRange, groupBy: StatsGroupBy): Promise<StatsUsage> {
  return request<StatsUsage>('/stats/usage', { params: { range, group_by: groupBy } });
}
