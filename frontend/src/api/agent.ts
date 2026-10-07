/**
 * Agent 任务家族的最小读取口（Phase 7 Task 8 建，GET /agents/tasks 一组）。
 *
 * 为什么存在：Workflow 触发出的是 task_type="workflow" 的 Task，
 * 进度/终态/Trace 全部复用 agents 家族端点（Task 7 的 202 口径：trigger 只给
 * task_id，之后靠轮询 GET /agents/tasks/{task_id}）。evaluation.ts 头注释里
 * 早就登记过「src/api/ 还没有 agent.ts（Agent 任务页尚未做）—— Agent 页落地时
 * 把 getRunTrace 移过来」；Phase 9a Task 3 兑现此债：getRunTrace 与其
 * TraceSpan/RunTrace 类型已从 evaluation.ts 退休，跨页共享走本模块。
 *
 * 类型与后端 schemas/agent_task.py 一一对应，snake_case 纪律同其他 api 模块。
 */
import { ApiError, request } from './client';

/**
 * 任务状态的七值联合（Phase 7 Task 2 六态 + Phase 8b T3/T4 扩 queued 一档）：
 *   queued（agent 面 8b 落库初值：已入队等 worker 领取）→ running → …
 *   pending（workflow 面既有落库初值，trigger 语义 8b 不变）→ running → …
 *   running → (停在断点) waiting_approval → (决策放行) running → completed
 *                                    └→ (驳回) rejected          failed = 链路炸了
 * 后端列是 String(20)、响应模型是 `status: str`——类型层没有字面量可依赖，
 * 所以收窄在读取边界做（parseTaskStatus）：出了这个边界，页面拿到的
 * status 只会是这七个值之一，渲染层的 switch 才配用 never 做穷尽断言。
 */
export const TASK_STATUSES = [
  'pending',
  'queued',
  'running',
  'waiting_approval',
  'completed',
  'failed',
  'rejected',
] as const;

export type TaskStatus = (typeof TASK_STATUSES)[number];

/**
 * 边界归一：不在七值集合里的 status 一律当场抛错，绝不静默降级成某个已知值。
 * 理由与 evaluation.ts 的 'unknown' 归一同源（不许把"未知"当"已知"渲），差别在于
 * 任务状态没有更中性的第八态可加（8b T3 扩 queued 是契约驱动，不是兜底槽）：
 * 抛错让轮询守卫计一次失败（连续 3 次 → halted 交回人工），页面顶部出可读横幅，
 * 而不是把没见过的状态渲成"待执行"骗人。
 */
export function parseTaskStatus(raw: string): TaskStatus {
  if ((TASK_STATUSES as readonly string[]).includes(raw)) return raw as TaskStatus;
  throw new ApiError(`后端返回了前端未知的任务状态："${raw}"`, 0);
}

/** 任务摘要（TaskOut，列表项） */
export interface AgentTaskSummary {
  id: string;
  title: string | null;
  question: string;
  /** "workflow" | "agent_analysis"（本列表只用 workflow 过滤后的） */
  task_type: string;
  status: TaskStatus;
  created_at: string;
  /** Phase 9b：该任务**最新一份**报告 id（没有报告时 null）。
   *  列表页据此直接给「看报告」入口，不必先拉详情再按 task_run_id 反查。 */
  report_id: string | null;
}

/** 一次执行（TaskRunOut）。waiting_approval 期间 run 保持 running
 *  （同一条 run 续跑，挂起态体现在 Task.status 上）；meta 里含
 *  report / workflow_input 等，形状随图而变，按透传壳收。
 *  run 自身 status 不做七值收窄：页面只在 task 层渲状态 badge，
 *  这里多一层窄化只会给未来的 run 态扩展添堵。 */
export interface TaskRunInfo {
  id: string;
  run_no: number;
  status: string;
  progress: number;
  failure_category: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  meta: Record<string, unknown> | null;
}

/** 任务详情（TaskDetailOut）：摘要 + 最近一次执行 */
export interface AgentTaskDetail extends AgentTaskSummary {
  latest_run: TaskRunInfo | null;
  run_count: number;
}

/** Trace 的一个节点：agent 执行或工具调用（后端 docs/06 §4 的 nodes 形状）。
 *  kind 只有两种：库里没有 llm 级独立 span，节点类型也不外乎这两类（Phase 9a 裁定）。 */
export interface TraceNode {
  kind: 'agent' | 'tool';
  span_id: string;
  /** NULL 表示根节点；工具节点的 parent 指向所属 Agent 的 span_id */
  parent_span_id: string | null;
  name: string;
  status: 'ok' | 'error' | string;
  duration_ms: number | null;
  /** 工具节点恒 0：tool_calls 表没有 token 列 */
  total_tokens: number;
  cost: number;
  /** 工具节点为 null */
  model: string | null;
  summary: string | null;
  error_message: string | null;
  started_at: string;
  finished_at: string | null;
}

/** 一次执行的 Trace（TraceOut）：run 概要 + 统一节点序列（后端已按 started_at 升序）。 */
export interface TaskTrace {
  trace_id: string | null;
  task_run_id: string;
  status: TaskStatus;
  nodes: TraceNode[];
}

/** 原始响应壳：status 在 JSON 里只是 string，parse 后才进窄类型 */
interface RawTaskOut extends Omit<AgentTaskSummary, 'status'> {
  status: string;
}
interface RawTaskDetailOut extends Omit<AgentTaskDetail, 'status'> {
  status: string;
}
/** Trace 的原始响应壳：status 在 JSON 里只是 string，parse 后才进窄类型 */
interface RawTaskTrace extends Omit<TaskTrace, 'status'> {
  status: string;
}

/** 任务列表：task_type 过滤（"workflow" = 只看 Workflow 触发出来的执行） */
export async function listAgentTasks(params: {
  status?: string;
  task_type?: string;
  limit?: number;
  offset?: number;
} = {}): Promise<AgentTaskSummary[]> {
  const rows = await request<RawTaskOut[]>('/agents/tasks', { params });
  return rows.map((raw) => ({ ...raw, status: parseTaskStatus(raw.status) }));
}

/** 任务详情（本页面轮询的就是这条；status 进七值窄类型） */
export async function getTaskDetail(taskId: string): Promise<AgentTaskDetail> {
  const raw = await request<RawTaskDetailOut>(`/agents/tasks/${taskId}`);
  return { ...raw, status: parseTaskStatus(raw.status) };
}

/** 一次执行的 Trace：步骤条数据源。断点前已跑完的 span 先落库、
 *  续跑段到终态再落（同 trace_id 自然累加），所以轮询期间重拉它是增量的 */
export async function getTaskTrace(taskRunId: string): Promise<TaskTrace> {
  const raw = await request<RawTaskTrace>(`/agents/task-runs/${taskRunId}/trace`);
  return { ...raw, status: parseTaskStatus(raw.status) };
}

/** 终态集合（三页轮询的停表判据唯一定义源）：AgentTasks 列表、Task Detail、
 *  Dashboard 概览都要问「这条还有必要继续盯吗」，判据必须同源——各页各抄一份
 *  列表，将来加态时就会出现「列表停表了、详情还在转」这种自相矛盾。
 *  waiting_approval 刻意不在这里：它在等人，页面要一直显示着等，
 *  但它是"人停表"不是"机器停表"，所以轮询照开（审批通过后同一条 run 续跑）。 */
export const TASK_SETTLED: readonly TaskStatus[] = ['completed', 'failed', 'rejected'];

export function isTaskSettled(status: TaskStatus): boolean {
  return TASK_SETTLED.includes(status);
}

/** POST /agents/tasks 系列三口的响应（TaskSubmitOut，202）。
 *  status 恒为落库初值 "queued"（8b T4 收口：建行即回，执行在 worker 进程）。
 *  前端不许乐观插行 —— 只有真拿到 202 才有那一行（spec §3.4）。 */
export interface TaskSubmitResult {
  task_id: string;
  task_run_id: string;
  status: string;
}

/** 提交一次分析任务。task_type 显式带上：后端白名单只认 agent_analysis
 *  （agents.py 的 AGENT_GRAPH_TASK_TYPES），写死比依赖默认值更不容易在扩档时踩坑。 */
export function submitTask(question: string): Promise<TaskSubmitResult> {
  return request<TaskSubmitResult>('/agents/tasks', {
    method: 'POST',
    body: { question, task_type: 'agent_analysis' },
  });
}

/** 原地重跑（202）：同一 task 叠一条新 run，历史不覆盖 */
export function rerunTask(taskId: string): Promise<TaskSubmitResult> {
  return request<TaskSubmitResult>(`/agents/tasks/${taskId}/rerun`, { method: 'POST' });
}

/** 追问（202）：记忆由后端按该 task 的历史快照装配，前端只发问题 */
export function followUpTask(taskId: string, question: string): Promise<TaskSubmitResult> {
  return request<TaskSubmitResult>(`/agents/tasks/${taskId}/follow-up`, {
    method: 'POST',
    body: { question },
  });
}
