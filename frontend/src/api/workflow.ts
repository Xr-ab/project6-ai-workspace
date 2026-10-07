/**
 * Workflow 域 API（Phase 7 Task 8，docs/06 §2.6 + Task 7 已上线的真实形状）。
 * 页面不直接拼 URL，只调这里的方法（约定同 evaluation.ts / document.ts）。
 *
 * 类型与后端 schemas/workflow.py 一一对应，字段保持 snake_case
 * （理由同 types/document.ts：不做驼峰转换，接口改了前端编译期就能对上）。
 *
 * 口径备注（Task 7 落地的现实，别按旧设计稿想当然）：
 *   - GET /workflows 直接返回数组，没有分页壳；
 *   - WorkflowOut.name/description 在 pydantic 层是 `str | None`，
 *     类型照写成 `| null`，卡片渲染不许假设它们必有值；
 *   - trigger 是 202：返回时图刚 spawn，真实进度一律去
 *     GET /agents/tasks/{task_id} 轮询（api/agent.ts，workflow 任务与
 *     agent 任务同构可读）；
 *   - 审批两条路由挂在 agents 家族下（审批资源属于 task），但语义属于
 *     workflow 域，所以收在本文件（brief 钦定的五出口之一，不动）。
 */
import { request } from './client';

/** 编目行（WorkflowOut，列表项与详情同形状） */
export interface Workflow {
  id: string;
  /** 展示名。后端列可空（预置三条有值，自建/半初始化行可能是 NULL） */
  name: string | null;
  description: string | null;
  /** 图的稳定标识（注册表键）：表单/详情按它区分预置图的交互差异 */
  graph_key: string;
  /** 字段名 → 类型声明（如 {question: "string"} / {document_id: "uuid"}）。
   *  触发表单按它渲输入项，每个键都是必填（后端缺键 → 422）。 */
  input_spec: Record<string, string>;
  is_active: boolean;
  created_at: string;
}

/** POST trigger 的即时响应（TriggerOut，202）：两个 id 供轮询/审批寻址 */
export interface TriggerResult {
  task_id: string;
  task_run_id: string;
  /** 恒为落库初值 "pending"（TriggerOut.status 是 str，别按字面量联合收口） */
  status: string;
}

/** 一条审批记录（ApprovalOut）。pending 时 decided_by/decided_at 为 null ——
 *  「还没人决策」的真话，不填占位值 */
export interface Approval {
  id: string;
  task_id: string;
  /** 审批节点名（三张图共用常量 "approval"） */
  graph_node: string;
  /** pending / approved / rejected（列表 pending 在前） */
  status: string;
  decided_by: string | null;
  decided_at: string | null;
  comment: string | null;
  created_at: string;
}

/** 审批决策入参（DecisionIn：extra=forbid，多一个字段都不发；
 *  decision 收窄成二值联合，"maybe" 之类垃圾不许进服务。
 *  decided_by 不在字段里 —— 决策人身份由服务端 dev_identity 注入，body 冒充不了 */
export interface DecisionInput {
  decision: 'approved' | 'rejected';
  comment?: string;
}

/** 编目列表：本 org 的可用 workflow（预置 3 条起步，创建序在前）。无分页壳 */
export function listWorkflows(): Promise<Workflow[]> {
  return request<Workflow[]>('/workflows');
}

/** 编目详情（触发表单靠 input_spec + graph_key）。404 = 不存在/跨租户/已停用同形 */
export function getWorkflow(workflowId: string): Promise<Workflow> {
  return request<Workflow>(`/workflows/${workflowId}`);
}

/** 触发一次执行（202）：inputs 的必填键集 = 编目行 input_spec，缺键/空值 → 422 */
export function triggerWorkflow(
  workflowId: string,
  inputs: Record<string, unknown>,
): Promise<TriggerResult> {
  return request<TriggerResult>(`/workflows/${workflowId}/trigger`, {
    method: 'POST',
    body: { inputs },
  });
}

/** 某任务的审批记录（pending 在前）。非 workflow 任务不特判：200 空数组 */
export function listApprovals(taskId: string): Promise<Approval[]> {
  return request<Approval[]>(`/agents/tasks/${taskId}/approvals`);
}

/** 审批决策：落库并后台从断点续跑。非 waiting_approval 的任务 → 409 */
export function decideApproval(
  taskId: string,
  approvalId: string,
  input: DecisionInput,
): Promise<Approval> {
  return request<Approval>(`/agents/tasks/${taskId}/approvals/${approvalId}`, {
    method: 'POST',
    body: input,
  });
}
