/**
 * 评测域 API（Phase 6 Evaluation，docs/06 §2.7 + docs/08 §7 的 10 个端点 + D9 回放沉淀 1 个）。
 * 页面不直接拼 URL，只调这里的方法（约定同 document.ts / chat.ts）。
 *
 * 类型与后端 schemas/evaluation.py 一一对应，字段保持 snake_case
 * （理由同 types/document.ts：不做驼峰转换，接口改了前端编译期就能对上）。
 * 可空字段全部写成 `| null` 而不是靠 undefined 猜：
 * "未配置单价"与"未覆盖指标"要靠这些显式 null 渲出来（D5 / Global Constraint 2）。
 *
 * 域内没有编辑/删除用例的端点（后端只有新增即 upsert，裁定 D13），
 * 也没有重跑/续跑端点 —— 前端不许自造接口。
 */
import { request } from './client';

/** docs/08 §2 的五类评测对象（后端 CaseIn.type 按这个集合校验） */
export type CaseCategory = 'chat' | 'rag' | 'tool_calling' | 'multi_agent' | 'workflow';

/** docs/06 §2.7 的 Run target 取值 */
export type RunTarget = 'agent' | 'prompt' | 'workflow';

/** Run 状态四态。只有终态（completed/failed）有指标；listRuns 只出终态 */
export type RunStatus = 'pending' | 'running' | 'completed' | 'failed';

/** 一条用例（CaseOut）。expected 里常见形状是 {key_points: string[]} */
export interface EvalCase {
  id: string;
  code: string;
  category: string;
  input: string;
  expected: Record<string, unknown>;
  references: Record<string, unknown>;
  judgement: Record<string, unknown>;
  tags: unknown[];
}

/** 数据集摘要（列表项与创建响应同形状，DatasetOut） */
export interface Dataset {
  id: string;
  name: string;
  description: string | null;
  category_scope: string[];
  created_at: string;
  case_count: number;
}

/** 数据集详情 = 摘要 + 用例明细（DatasetDetailOut，code 正序） */
export interface DatasetDetail extends Dataset {
  cases: EvalCase[];
}

/** POST /runs 的即时响应（RunSubmitOut）：执行在后台，之后靠 getRun 轮询 */
export interface RunSubmit {
  evaluation_id: string;
  status: RunStatus;
}

/** Run 状态/进度（RunOut），轮询的就是这条 */
export interface Run {
  id: string;
  dataset_id: string | null;
  target: string;
  target_version: string;
  note: string | null;
  status: RunStatus;
  progress: number;
  case_total: number;
  case_done: number;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
}

/** GET /runs 的列表项（RunSummaryOut）= 状态字段 + created_at + metrics。
 * 只出终态 run（repo 口径）；completed 的 metrics 必有值，failed 的可能是 null。 */
export interface RunSummary extends Run {
  created_at: string;
  metrics: RunMetrics | null;
}

/**
 * 指标（compute_metrics 的原样透传，键名逐字）。
 * 三个"必须显式渲出来"的可空字段：
 *   total_cost: null        → 单价未配置（cost_available=false，绝不显示 0，D5）
 *   retrieval_quality: null → 未覆盖，原因登记在 not_applicable
 *   其余 rate 类 null       → 本批无可用样本（分母为 0），渲"未覆盖"灰态
 */
export interface RunMetrics {
  // ---- docs/06 §2.7 的六个契约键 ----
  task_success_rate: number | null;
  tool_success_rate: number | null;
  latency_avg_ms: number | null;
  total_tokens: number;
  total_cost: number | null;
  failure_category_dist: Record<string, number>;
  // ---- docs/08 §4 其余口径 ----
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  agent_completion_rate: number | null;
  reviewer_error_detection_rate: number | null;
  reviewer_miss_rate: number | null;
  final_answer_quality_avg: number | null;
  retrieval_quality: number | null;
  cases_total: number;
  cases_passed: number;
  cases_error: number;
  reviewed_cases: number;
  cost_available: boolean;
  quality_pass_score: number;
  category_scope: string[];
  not_applicable: string[];
  /** {case_no: passed}，回归对比里逐用例翻转的数据源（键在 JSONB 里是字符串） */
  case_results: Record<string, boolean>;
}

/** 一条用例的结果（EvalResultCaseOut）。case_no 是 int，与 compare 侧同型 */
export interface ResultCase {
  case_id: string | null;
  case_no: number;
  /** ok（跑完）/ error（链路炸了）（D12） */
  status: string;
  passed: boolean;
  score: number | null;
  trace_id: string | null;
  /** 下钻入口：评测不复制执行痕迹，只引用生产 Trace（§8） */
  task_run_id: string | null;
  note: string | null;
  /** judge 的分档理由（§5.3 可解释性） */
  reasons: string[] | null;
  latency_ms: number | null;
  /** null = 未配单价、这轮成本不可得，不是 0（D5） */
  cost: number | null;
}

/** 逐用例结果（RunResultOut） */
export interface RunResult {
  cases: ResultCase[];
}

/**
 * diff 的方向，七个取值。missing_base / missing_head 是"单边缺席"（裁定 D14）：
 * 把它渲成 same（持平）= 向用户承诺"没有回归"，而真相是"不可比"。
 *
 * W2 补两个（修前后四支全被 `else: same` 吞成"持平"）：
 *   unmeasured 两侧键都在、但任一侧是 null —— **没量出来**，持平不成立
 *   changed    两侧都是非数值且值不相等（旗标翻了 / 集合换构成）—— 变了，方向无意义
 * 与 D14 同一条纪律：「持平」只能来自真实量出来的相等，不能来自"没量"或"量不出方向"。
 * 取值域与后端两份登记逐字对齐（DIFF_DIRS / COMPARE_DIRS），后端新增取值而这里没加，
 * compareRuns 的运行时归一会把它改写成 'unknown'、dirView 的 never 断言会编译红。
 */
export type CompareDir =
  | 'up'
  | 'down'
  | 'same'
  | 'missing_base'
  | 'missing_head'
  | 'unmeasured'
  | 'changed';

/**
 * 边界归一后的 dir：后端的七值 + 只由前端产生的 'unknown'（I3 修复轮②）。
 * TypeScript 的联合类型约束不了运行时 JSON —— 后端若新增 dir 取值，
 * 原样透传的响应在渲染端没有 case 接得住。compareRuns 里把集合外的值
 * 统一改写成 'unknown'（见 normalizeCompareDirs），UI 明确渲"无法比较"，
 * 绝不把"未知"当"持平"（那是 D14 那一族的谎）。
 * 后端真新增取值时 EvaluationPage 的 dirView 末尾 never 断言会编译红（I3 修复轮①）。
 */
export type NormalizedCompareDir = CompareDir | 'unknown';

/** 单个指标的 diff（diff_metrics 的一项）。base/head 是原始指标值，可能非数值 */
export interface MetricDiff {
  base: unknown;
  head: unknown;
  /** 只有两侧都是真数值时才有量出来的 delta；缺席/未量出/口径变化恒 null */
  delta: number | null;
  dir: NormalizedCompareDir;
  /** 方向语义由后端单点给（latency/token/cost 越小越好），前端别自己猜。
   *  W2 裁定：不在前端再养一张方向表 —— 那是第二个真相源，两张表必然漂移。 */
  lower_is_better: boolean;
  /**
   * 该键的方向有没有在后端登记过（LOWER_IS_BETTER | HIGHER_IS_BETTER）。
   * false 时 `lower_is_better` 的 false 含义是"**没登记**"，不是"越大越好"——
   * 计数类（cases_total / reviewed_cases）与元信息类（not_applicable / dist /
   * cost_available）都在这支队伍里。渲染端必须据此出中性"方向未登记"，
   * 绝不给绿/红（`!== true` 判，字段整个缺席时也走中性，安全方向唯一）。
   */
  direction_registered: boolean;
}

/** 逐用例翻转（case_no 在 service 读取边界归一回 int） */
export interface CaseFlip {
  case_no: number;
  from: 'pass' | 'fail';
  to: 'pass' | 'fail';
}

/**
 * 覆盖账（W3，后端 schemas.CASE_COVERAGE_KEYS 同域）：case_flips 只统计
 * **两侧都跑过**的用例，所以 base 10 条 / head 5 条时 flips 可以是空的，
 * 而真相是"后 5 条根本没进过场"。这个字段就是用来把"没有翻转"与
 * "翻转不可比"分开的（与 D14 / W2 同一条纪律）。
 */
export interface CaseCoverage {
  base_total: number;
  head_total: number;
  comparable_total: number;
  /** head 没跑过的用例号（已归一为 int） */
  only_in_base: number[];
  /** base 没跑过的用例号 */
  only_in_head: number[];
  flips_total: number;
}

/**
 * compare 响应 = {指标名: MetricDiff} + 两个结构性键。
 * 结构性键的名单在后端登记为 COMPARE_STRUCTURAL_KEYS（schemas/evaluation.py），
 * 渲染端按它决定"哪些键进指标表、哪些按身份特判"—— 除这两项以外的顶层键
 * 一律进表（W3.1：过滤器只准排序，不准吃掉行）。
 */
export interface CompareResult {
  case_flips: CaseFlip[];
  case_coverage: CaseCoverage;
  [metric: string]: MetricDiff | CaseFlip[] | CaseCoverage;
}

/** 新增用例的入参（CaseIn 的白名单子集：extra=forbid，多一个字段都不发） */
export interface CaseCreateInput {
  input: string;
  type: CaseCategory;
  expected?: { key_points?: string[] } & Record<string, unknown>;
}

/** 数据集列表（created_at 倒序） */
export function listDatasets(limit = 50, offset = 0): Promise<Dataset[]> {
  return request<Dataset[]>('/evaluations/datasets', { params: { limit, offset } });
}

/** 数据集详情：摘要 + 用例明细 */
export function getDataset(datasetId: string): Promise<DatasetDetail> {
  return request<DatasetDetail>(`/evaluations/datasets/${datasetId}`);
}

/** 创建数据集（cases 可选，给了就在同一请求里写入） */
export function createDataset(input: {
  name: string;
  description?: string;
  category_scope?: CaseCategory[];
  cases?: CaseCreateInput[];
}): Promise<Dataset> {
  return request<Dataset>('/evaluations/datasets', { method: 'POST', body: input });
}

/** 新增一条用例（嵌套路径，裁定 D13；同 code = upsert 覆盖内容列） */
export function addCase(datasetId: string, input: CaseCreateInput): Promise<EvalCase> {
  return request<EvalCase>(`/evaluations/datasets/${datasetId}/cases`, {
    method: 'POST',
    body: input,
  });
}

/** 从生产任务一键回放沉淀用例（D9 / docs/08 §3.3）：input/category 由服务端反推，
 *  expected 落"待补"态；同一条 run 重复加入幂等（返回已存在的那条） */
export function promoteCaseFromTaskRun(datasetId: string, taskRunId: string): Promise<EvalCase> {
  return request<EvalCase>(`/evaluations/datasets/${datasetId}/cases/from-task-run`, {
    method: 'POST',
    body: { task_run_id: taskRunId },
  });
}

/** 发起评测：建 run（pending）后立即返回，执行挂在后台（本仓唯一真花钱的入口，见钱闸） */
export function startRun(input: {
  dataset_id: string;
  target: RunTarget;
  note?: string;
}): Promise<RunSubmit> {
  return request<RunSubmit>('/evaluations/runs', { method: 'POST', body: input });
}

/** Run 状态/进度（轮询端点；pending/running 只在它上面可查） */
export function getRun(runId: string): Promise<Run> {
  return request<Run>(`/evaluations/runs/${runId}`);
}

/** Run 列表（趋势图数据源）。只出终态 run —— 进行中那条要用 getRun 找 */
export function listRuns(datasetId?: string): Promise<RunSummary[]> {
  return request<RunSummary[]>('/evaluations/runs', { params: { dataset_id: datasetId } });
}

/**
 * Run 指标。**未完成时后端回 409（EVAL_409001）而不是 null** ——
 * 调用方要把 409 当"还没有指标"，不是错误红条（见 EvaluationPage 的分支）。
 */
export function getMetrics(runId: string): Promise<RunMetrics> {
  return request<RunMetrics>(`/evaluations/runs/${runId}/metrics`);
}

/** 逐用例结果（case_no 正序） */
export function getResult(runId: string): Promise<RunResult> {
  return request<RunResult>(`/evaluations/runs/${runId}/result`);
}

/** dir 七值的运行时集合（边界归一的判据；集合漏一个值只会多渲"无法比较"，方向安全）。
 *  W2：与后端 `DIFF_DIRS` / `COMPARE_DIRS` 逐字同域 —— `test_evaluations_api.py`
 *  里有 `COMPARE_DIRS == DIFF_DIRS` 的断言锁后端两份登记，这里漏一个值的话
 *  新 dir 会被归一成 'unknown'（渲"无法比较"，安全方向的降级，不会误报持平）。
 *  数组写成显式 7 项字面量而不是从类型派生，是为了让漏项在 review 时一眼可见。 */
const VALID_COMPARE_DIRS: readonly CompareDir[] = [
  'up',
  'down',
  'same',
  'missing_base',
  'missing_head',
  'unmeasured',
  'changed',
];

/** compare 顶层**不是指标 diff** 的结构性键（= 后端 schemas.COMPARE_STRUCTURAL_KEYS）。
 *  两处用到：① 运行时归一要跳过它们（它们没有 dir，归一会把 case_coverage 写脏）；
 *  ② 对比表的行过滤器只跳这些键 —— 别的键一律出行（W3.1：过滤器只准排序不准吃行，
 *  否则后端新加一个指标键，前端这张表就静默少一行，而这张表是回归判据本身）。 */
export const COMPARE_STRUCTURAL_KEYS: readonly string[] = ['case_flips', 'case_coverage'];

/**
 * compare 响应的一次运行时归一（I3 修复轮②）：凡 dir 不在七值集合里
 * （后端新增取值、字段整个缺席、形状漂移成非对象值之外的 dir）统一改写为
 * 'unknown'，让"无法比较"在运行时确定可渲，而不是落到某个默认分支渲成"持平"。
 * 就地改写后返回 —— 这是刚 JSON.parse 出来的响应壳，没有别人持有同一引用。
 */
function normalizeCompareDirs(raw: CompareResult): CompareResult {
  for (const [key, value] of Object.entries(raw)) {
    if (COMPARE_STRUCTURAL_KEYS.includes(key)) continue;
    if (value === null || typeof value !== 'object') continue;
    const diff = value as MetricDiff;
    if (!VALID_COMPARE_DIRS.some((dir) => dir === diff.dir)) {
      diff.dir = 'unknown';
    }
  }
  return raw;
}

/**
 * 两次 Run 的回归对比（两侧都必须 completed，否则后端 409）。
 * dir 的运行时归一只在这里做一次（见 normalizeCompareDirs），
 * 出了这个边界，渲染层见到的 dir 只会是 NormalizedCompareDir。
 */
export async function compareRuns(base: string, head: string): Promise<CompareResult> {
  const raw = await request<CompareResult>('/evaluations/compare', { params: { base, head } });
  return normalizeCompareDirs(raw);
}
