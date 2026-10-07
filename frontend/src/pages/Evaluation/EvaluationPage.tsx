import { Fragment, useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { ApiError, toReadableError } from '../../api/client';
import {
  addCase,
  COMPARE_STRUCTURAL_KEYS,
  compareRuns,
  getDataset,
  getMetrics,
  getResult,
  getRun,
  listDatasets,
  listRuns,
  startRun,
} from '../../api/evaluation';
import type {
  CaseCategory,
  CompareResult,
  Dataset,
  DatasetDetail,
  MetricDiff,
  Run,
  RunMetrics,
  RunResult,
  RunSummary,
  RunTarget,
} from '../../api/evaluation';
import { FailureDonut, TrendLine } from './MetricCharts';
import type { TrendPoint } from './MetricCharts';
import TraceTree from './TraceTree';
import styles from './EvaluationPage.module.css';

/** 轮询节奏与止损线（brief Step 2）：2s 一次；连续 3 次失败就停手，交给人工重试 */
const POLL_INTERVAL_MS = 2000;
const MAX_POLL_FAILURES = 3;

/** 成功提示的停留时长（同 KnowledgePage 的做法） */
const HINT_DURATION = 3000;

/** docs/08 §2 的五类，新增用例下拉用 */
const CASE_CATEGORIES: CaseCategory[] = [
  'chat',
  'rag',
  'tool_calling',
  'multi_agent',
  'workflow',
];

const RUN_TARGETS: Array<{ value: RunTarget; label: string }> = [
  { value: 'agent', label: 'agent（Multi-Agent 全链路）' },
  { value: 'prompt', label: 'prompt' },
  { value: 'workflow', label: 'workflow' },
];

type ValueKind = 'percent' | 'ms' | 'int' | 'cost' | 'score';

/** RunMetrics 的全部键（M1 修复轮：键表用它的 keyof 而不是裸 string）——
 *  键名打错在编译期报错，而不是查不到值后静默渲成"未覆盖/未配置"（I1 那一族）。 */
type MetricKey = keyof RunMetrics;

/** 指标键 → 展示口径。渲染与格式化都以这张表为准，不散落 if */
const METRIC_KIND: Partial<Record<MetricKey, ValueKind>> = {
  task_success_rate: 'percent',
  tool_success_rate: 'percent',
  agent_completion_rate: 'percent',
  reviewer_error_detection_rate: 'percent',
  reviewer_miss_rate: 'percent',
  retrieval_quality: 'percent',
  latency_avg_ms: 'ms',
  latency_p50_ms: 'ms',
  latency_p95_ms: 'ms',
  total_tokens: 'int',
  total_prompt_tokens: 'int',
  total_completion_tokens: 'int',
  total_cost: 'cost',
  final_answer_quality_avg: 'score',
};

/** 指标卡：docs/06 §2.7 六键 + docs/08 §4 的四个补充口径（Step 4 点名列举） */
const METRIC_CARDS: Array<{ key: MetricKey; label: string; kind: ValueKind | 'dist' }> = [
  { key: 'task_success_rate', label: '任务成功率', kind: 'percent' },
  { key: 'tool_success_rate', label: '工具成功率', kind: 'percent' },
  { key: 'latency_avg_ms', label: '平均延迟', kind: 'ms' },
  { key: 'total_tokens', label: '总 Token', kind: 'int' },
  { key: 'total_cost', label: '总成本', kind: 'cost' },
  { key: 'failure_category_dist', label: '失败类别分布', kind: 'dist' },
  { key: 'agent_completion_rate', label: 'Agent 完成率', kind: 'percent' },
  { key: 'reviewer_error_detection_rate', label: 'Reviewer 拦截率', kind: 'percent' },
  { key: 'reviewer_miss_rate', label: 'Reviewer 漏检率', kind: 'percent' },
  { key: 'final_answer_quality_avg', label: '最终回答质量均值', kind: 'score' },
  // retrieval_quality 不在 brief Step 4 的十键里，但 Step 7 第 3 项要求它显式渲出
  // "未覆盖"：给它一张卡，null + not_applicable 的渲法在指标面板里直接可见
  { key: 'retrieval_quality', label: 'Retrieval Quality', kind: 'percent' },
];

/** 趋势图可切的指标（都有数值语义；dist 类不进趋势） */
const TREND_METRICS: Array<{ key: MetricKey; label: string }> = [
  { key: 'task_success_rate', label: '任务成功率' },
  { key: 'tool_success_rate', label: '工具成功率' },
  { key: 'agent_completion_rate', label: 'Agent 完成率' },
  { key: 'reviewer_error_detection_rate', label: 'Reviewer 拦截率' },
  { key: 'reviewer_miss_rate', label: 'Reviewer 漏检率' },
  { key: 'final_answer_quality_avg', label: '回答质量均值' },
  { key: 'retrieval_quality', label: 'Retrieval Quality' },
  { key: 'latency_avg_ms', label: '平均延迟 (ms)' },
  { key: 'total_tokens', label: '总 Token' },
  { key: 'total_cost', label: '总成本' },
];

/** 回归对比表默认展示的行（docs/06 六键 + docs/08 数值口径）。
 *  缺席键（missing_base/missing_head）即使不在这张表里也会追加进来 ——
 *  "某指标整个消失"是比数值抖动更大的回归信号，不能被过滤器吃掉。 */
const COMPARE_PRIMARY_KEYS: readonly MetricKey[] = [
  'task_success_rate',
  'tool_success_rate',
  'agent_completion_rate',
  'reviewer_error_detection_rate',
  'reviewer_miss_rate',
  'final_answer_quality_avg',
  'retrieval_quality',
  'latency_avg_ms',
  'latency_p50_ms',
  'latency_p95_ms',
  'total_tokens',
  'total_cost',
];

/* ---- 本地格式化函数（R126 终评 F7 更正注释：原文「仓内无共享 utils/」已被否证——
   src/lib/format.ts 就是共享格式化模块，同页 TraceTree.tsx 正在 import 它的 formatDuration。
   本页这几份与 lib/ 口径不同（formatMs/formatCost 无分钟档、币种硬编码），统一留 9b：
   会改已验收页的渲染文案，不在收口修射程。 ---- */

function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function formatMs(value: number): string {
  return `${Math.round(value).toLocaleString('en-US')} ms`;
}

function formatInt(value: number): string {
  return Math.round(value).toLocaleString('en-US');
}

function formatCost(value: number): string {
  return `¥${value.toFixed(4)}`;
}

function formatByKind(key: string, value: number): string {
  // key 允许是 compare 响应里的任意指标名（后端可新增键，见报告里 extra_metric_v9 那例），
  // 而 METRIC_KIND 只收录已知键：在这个边界做一次判定，页内键表已由 MetricKey 锁住（M1）。
  const kind = key in METRIC_KIND ? METRIC_KIND[key as MetricKey] : undefined;
  switch (kind) {
    case 'percent':
      return formatPercent(value);
    case 'ms':
      return formatMs(value);
    case 'cost':
      return formatCost(value);
    case 'score':
      return value.toFixed(2);
    default:
      return formatInt(value);
  }
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function statusView(status: Run['status']): { label: string; className: string } {
  switch (status) {
    case 'completed':
      return { label: '已完成', className: styles.badgeReady };
    case 'failed':
      return { label: '失败', className: styles.badgeFailed };
    case 'running':
      return { label: '执行中', className: styles.badgeProcessing };
    default:
      return { label: '待执行', className: styles.badgeNeutral };
  }
}

/* ---------------- 轮询 hook（本仓第一次，brief Step 2 一条不许省） ---------------- */

/**
 * 轮询 getRun：setInterval 2000ms；status 进入 completed|failed 即 clearInterval；
 * 卸载时必清（effect return）；runId 变化时整套状态重置；
 * 拉取失败连续 3 次 → 停止轮询并交回人工重试（不许无限重试打后端）。
 *
 * 为什么要进度条：后端是 BackgroundTasks 串行跑，一条用例约 60s，10 条约 15 分钟 ——
 * 没有 case_done/case_total 用户会以为页面死了。
 */
function useRunPolling(runId: string | null) {
  const [run, setRun] = useState<Run | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [halted, setHalted] = useState(false);
  // 手动重试的计数器：+1 让 effect 整体重跑（等价于"重新开始一轮轮询"）
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    // runId 变化 → 上一轮的状态全部作废（重置）
    setRun(null);
    setFailure(null);
    setHalted(false);
    if (!runId) return;

    // I2 修复轮：与"选中数据集拉详情""选中 Run 拉指标"两个 effect 同款的
    // cancelled 守卫（评审指出的全页唯一缺口）。runId 翻转后 A 那条在飞的
    // getRun 不许再落 setState —— 否则 B 的头部可能显示 A，
    // 且 A 的失败会把 halted=true 粘到已经正常的 B 上、把重试横幅困住。
    let cancelled = false;
    let consecutiveFailures = 0;
    let timer = 0;
    const stop = () => window.clearInterval(timer);

    const tick = async () => {
      try {
        const fresh = await getRun(runId);
        if (cancelled) return;
        consecutiveFailures = 0;
        setRun(fresh);
        setFailure(null);
        if (fresh.status === 'completed' || fresh.status === 'failed') {
          stop(); // 进终态即停：listRuns 只出终态，之后它就是普通的历史 Run
        }
      } catch (error) {
        if (cancelled) return;
        consecutiveFailures += 1;
        setFailure(toReadableError(error));
        if (consecutiveFailures >= MAX_POLL_FAILURES) {
          stop();
          setHalted(true); // 止损：显示"进度获取失败，点击重试"，不再自动打后端
        }
      }
    };

    void tick(); // 立即拉一次，不等第一个 2s 间隔
    timer = window.setInterval(() => void tick(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true; // 卸载必清（守卫 + 清 timer 两件事在同一处生效）
      stop();
    };
  }, [runId, attempt]);

  return { run, failure, halted, retry: () => setAttempt((a) => a + 1) };
}

/* ---------------- 指标卡与对比行的纯渲染件（同文件内的小组件，不外抛） ---------------- */

/** 一张指标卡。三条特殊渲法都是"不可得必须说出来"的口径：
 *  cost_available=false → "单价未配置"（绝不显示 0 元，D5）；
 *  null → "未覆盖"灰态 + not_applicable 里的原因；
 *  正常值 → 按 kind 格式化。 */
function MetricCard({
  spec,
  metrics,
}: {
  spec: (typeof METRIC_CARDS)[number];
  metrics: RunMetrics;
}) {
  // spec.key 已由 MetricKey（keyof RunMetrics）锁住：直接下标取值，
  // 不再需要 `as unknown as Record`（M1 修复轮，那两层擦除正是"键打错静默渲未覆盖"的入口）
  const raw = metrics[spec.key];

  if (spec.kind === 'dist') {
    const dist = (raw ?? {}) as RunMetrics['failure_category_dist'];
    const entries = Object.entries(dist).filter(([, n]) => n > 0);
    const total = entries.reduce((sum, [, n]) => sum + n, 0);
    return (
      <div className={styles.metricCard}>
        <div className={styles.metricLabel}>{spec.label}</div>
        {/* W1 前端同步：这个键量的是"未通过用例的类别分布"（judge 判不过的在
            judge_failed 桶里），不是"链路分类出现过的条数" —— 文案改说它真正量的
            东西，并指一句判分不过的用例去哪看，别用"本批无失败"冒充全过了 */}
        <div className={total ? styles.metricValue : styles.metricValueNull}>
          {total ? `${entries.length} 类 / ${total} 条` : '本批无链路失败分类'}
        </div>
        <div className={styles.metricNote}>
          {total
            ? '右下方占比图可看每类明细 · 判分不过的用例见下方明细'
            : '判分不过的用例见下方明细'}
        </div>
      </div>
    );
  }

  // 成本单独一支：null 的原因不是"没样本"而是"没单价"，文案必须不同（D5）
  if (spec.key === 'total_cost' && (raw === null || metrics.cost_available === false)) {
    return (
      <div className={styles.metricCard}>
        <div className={styles.metricLabel}>{spec.label}</div>
        <div className={styles.metricValueNull}>单价未配置</div>
        <div className={styles.metricNote}>cost_available=false —— 按 0 元入账就是假账</div>
      </div>
    );
  }

  if (typeof raw !== 'number') {
    const inNotApplicable = metrics.not_applicable?.includes(spec.key);
    return (
      <div className={styles.metricCard}>
        <div className={styles.metricLabel}>{spec.label}</div>
        <div className={styles.metricValueNull}>未覆盖</div>
        <div className={styles.metricNote}>
          {inNotApplicable
            ? `登记于 not_applicable：本批 category_scope=${
                metrics.category_scope?.join('/') || '空'
              } 未含该指标所需类别`
            : '本批无可用样本（分母为 0），不充 0'}
        </div>
      </div>
    );
  }

  return (
    <div className={styles.metricCard}>
      <div className={styles.metricLabel}>{spec.label}</div>
      <div className={styles.metricValue}>{formatByKind(spec.key, raw)}</div>
      {spec.key === 'final_answer_quality_avg' && (
        <div className={styles.metricNote}>判分 0-5，及格线 quality_pass_score={metrics.quality_pass_score}</div>
      )}
      {spec.key === 'task_success_rate' && (
        <div className={styles.metricNote}>
          {metrics.cases_passed}/{metrics.cases_total} 条通过
          {metrics.cases_error > 0 ? ` · ${metrics.cases_error} 条链路错误` : ''}
        </div>
      )}
      {(spec.key === 'reviewer_error_detection_rate' || spec.key === 'reviewer_miss_rate') && (
        <div className={styles.metricNote}>分母 reviewed_cases={metrics.reviewed_cases}</div>
      )}
    </div>
  );
}

/** diff 方向的展示：后端七值 + 边界归一的 unknown，八值各有独立渲法。
 *  着色必须用 lower_is_better：latency/token/cost 变小是"变好"（绿），别默认"涨是好事"。
 *  missing_* 是"单边缺席"—— 不许并入持平（D14）；unknown 是"后端给了没见过的
 *  dir 取值"（compareRuns 边界归一的产物）—— 同样不许并入持平（I3 修复轮：
 *  原先以无条件 `return 持平` 收尾，等于把 criterion 1 的失败模式埋进代码）。
 *
 *  W2 补两值（后端 `else: same` 把四种语义全吞成"持平"的前端一侧）：
 *    unmeasured 两侧键都在、但有一侧是 null → "?/未量出"（中性，持平不成立）
 *    changed    两侧都是非数值且值不相等（旗标翻了/集合换构成）→ "◆/口径变化"
 *               （警示，不是持平也不是好坏）。◆ 与 missing_base 同符是判据字面
 *               指定的取值，靠文案区分；改符号要连判据一起改，这里不擅改。
 *  两支都**不许绿/红**（W2.3）：绿红只在 up/down 且方向登记过时才有资格出现。
 *
 *  末尾 default 的 never 断言是编译期锁：CompareDir 新增取值而这里没加 case，
 *  `npm run build` 立刻红，而不是静默渲成"持平"。 */
function dirView(diff: MetricDiff): { symbol: string; text: string; className: string } {
  switch (diff.dir) {
    case 'missing_base':
      return { symbol: '◆', text: '新增', className: styles.dirMissing };
    case 'missing_head':
      return { symbol: '◇', text: '消失', className: styles.dirMissing };
    case 'up':
    case 'down': {
      // W2.3：`direction_registered !== true` 时 lower_is_better 的 false 含义是
      // "这个键的方向后端没登记"，不是"越大越好"—— 计数类（cases_total /
      // reviewed_cases）与元信息类（not_applicable）都在这支里，把它们的涨跌
      // 渲成绿/红就是给"用例变多了"发奖状。用 `!== true` 判：透传壳整个漏掉
      // 这个布尔时也落中性，安全方向唯一。
      if (diff.direction_registered !== true) {
        return {
          symbol: diff.dir === 'up' ? '▲' : '▼',
          text: '方向未登记',
          className: styles.dirFlat,
        };
      }
      const improved = diff.dir === (diff.lower_is_better ? 'down' : 'up');
      return {
        symbol: diff.dir === 'up' ? '▲' : '▼',
        text: improved ? '变好' : '变差',
        className: improved ? styles.dirGood : styles.dirBad,
      };
    }
    case 'same':
      return { symbol: '―', text: '持平', className: styles.dirFlat };
    case 'unmeasured':
      return { symbol: '?', text: '未量出', className: styles.dirFlat };
    case 'changed':
      return { symbol: '◆', text: '口径变化', className: styles.dirMissing };
    case 'unknown':
      return { symbol: '?', text: '无法比较', className: styles.dirMissing };
    default: {
      // 上面 case 覆盖全部 NormalizedCompareDir 后，这里的 diff.dir 是 never。
      // 编译期到不了、运行期也被 compareRuns 归一挡在前面，仅作兜底。
      const _exhaustive: never = diff.dir;
      void _exhaustive;
      throw new Error(`dirView: 意外的 dir 取值 ${String(diff.dir)}`);
    }
  }
}

/** 对比格里一格的值。未覆盖必须读两侧的原始值而不是从 dir 推：
 *  两侧都在、值都是 null 的指标 dir 也是 same，但它俩都是"未覆盖"，不是"持平于 0" */
function compareCellText(raw: unknown, key: string): string {
  if (raw === undefined) return '缺席';
  if (raw === null) return key === 'total_cost' ? '未配置' : '未覆盖';
  if (typeof raw === 'number') return formatByKind(key, raw);
  if (Array.isArray(raw)) return raw.length ? raw.map(v => String(v)).join('、') : '（空）';
  // 对象必须是这副样子，不是 [object Object]：failure_category_dist 是 W1 之后
  // 唯一能回答"失败都长什么形状"的一格，渲成 [object Object] 等于把修好的东西又藏起来。
  if (typeof raw === 'object') {
    const entries = Object.entries(raw as Record<string, unknown>);
    return entries.length ? entries.map(([k, v]) => `${k}×${String(v)}`).join('、') : '（空）';
  }
  return String(raw);
}

/* ---------------- 页面 ---------------- */

/**
 * Evaluation 页（docs/08 §7 全清单）：数据集/用例浏览 + 发起 Run + 轮询进度 +
 * 指标卡 + 自绘趋势/占比（D6）+ 逐用例失败明细与 Trace 下钻 + 回归对比。
 *
 * 状态全部放页面本地（同 KnowledgePage：没有跨页共享数据，不进 Zustand store；
 * 全仓也只有 chat 因高频流式才用 store）。
 */
export default function EvaluationPage() {
  // ---- 数据集 ----
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [datasetsLoading, setDatasetsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedDatasetId, setSelectedDatasetId] = useState<string | null>(null);
  const [detail, setDetail] = useState<DatasetDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // ---- 新增用例表单（能看能加为止，不做编辑/删除：后端没有那两个端点） ----
  const [caseInput, setCaseInput] = useState('');
  const [caseCategory, setCaseCategory] = useState<CaseCategory>('multi_agent');
  const [caseKeyPoints, setCaseKeyPoints] = useState('');
  const [addingCase, setAddingCase] = useState(false);

  // ---- 发起 Run ----
  const [runDatasetId, setRunDatasetId] = useState('');
  const [runTarget, setRunTarget] = useState<RunTarget>('agent');
  const [runNote, setRunNote] = useState('');
  const [starting, setStarting] = useState(false);

  // ---- Run 列表与选中 ----
  const [runs, setRuns] = useState<RunSummary[]>([]);
  // ?run=<id> 深链：读作选中态的初值，并在每次选中时写回（I4 修复轮，见 selectRun）。
  // 进行中的 run 不在 listRuns（只出终态）里，刷新页面后全靠这个参数把它指回来。
  const [searchParams, setSearchParams] = useSearchParams();
  const [activeRunId, setActiveRunId] = useState<string | null>(searchParams.get('run'));

  // ---- 选中 Run 的指标 / 结果 / 下钻 ----
  const [metrics, setMetrics] = useState<RunMetrics | null>(null);
  const [metricsNote, setMetricsNote] = useState<string | null>(null);
  const [result, setResult] = useState<RunResult | null>(null);
  const [expandedCaseNo, setExpandedCaseNo] = useState<number | null>(null);
  const [traceCase, setTraceCase] = useState<{ taskRunId: string; caseNo: number } | null>(null);

  // ---- 趋势 ----
  const [trendMetric, setTrendMetric] = useState<MetricKey>('task_success_rate');

  // ---- 回归对比 ----
  const [compareBase, setCompareBase] = useState('');
  const [compareHead, setCompareHead] = useState('');
  const [compareResult, setCompareResult] = useState<CompareResult | null>(null);
  const [compareError, setCompareError] = useState<string | null>(null);
  const [comparing, setComparing] = useState(false);

  const [actionError, setActionError] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);

  // ---- 轮询（本仓第一次，设计理由见 hook 头注释） ----
  const { run: polledRun, failure: pollFailure, halted, retry } = useRunPolling(activeRunId);
  // pending/running 那条不在 listRuns 里（repo 只出终态），进行中的展示只能靠 polledRun
  const activeRun: Run | null =
    polledRun ?? runs.find((r) => r.id === activeRunId) ?? null;

  /* --------- 数据加载 --------- */

  const loadDatasets = useCallback(async () => {
    setDatasetsLoading(true);
    setLoadError(null);
    try {
      const list = await listDatasets();
      setDatasets(list);
      // 默认展开最近的一个数据集，省一次点击；已选的还指向原 id 时不动
      setSelectedDatasetId((current) => {
        if (current && list.some((d) => d.id === current)) return current;
        return list[0]?.id ?? null;
      });
    } catch (error) {
      setLoadError(toReadableError(error));
    } finally {
      setDatasetsLoading(false);
    }
  }, []);

  const loadRuns = useCallback(async () => {
    try {
      setRuns(await listRuns());
    } catch (error) {
      // Run 列表失败不吞整页：数据集与用例仍然可看，报错走顶部横幅
      setActionError(toReadableError(error));
    }
  }, []);

  useEffect(() => {
    void loadDatasets();
    void loadRuns();
  }, [loadDatasets, loadRuns]);

  useEffect(() => {
    if (!hint) return;
    const timer = window.setTimeout(() => setHint(null), HINT_DURATION);
    return () => window.clearTimeout(timer);
  }, [hint]);

  // 选中数据集 → 拉详情（含 cases 明细）
  useEffect(() => {
    if (!selectedDatasetId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    void (async () => {
      try {
        const d = await getDataset(selectedDatasetId);
        if (!cancelled) setDetail(d);
      } catch (error) {
        if (!cancelled) {
          setDetail(null);
          setActionError(toReadableError(error));
        }
      } finally {
        if (!cancelled) setDetailLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedDatasetId]);

  // 选中 Run 进终态 → 拉指标与逐用例结果。
  // metrics 端点对未完成回 409（EVAL_409001）：那是"还没有指标"，不是错误红条。
  const activeRunStatus = activeRun?.status;
  useEffect(() => {
    setMetrics(null);
    setMetricsNote(null);
    setResult(null);
    setExpandedCaseNo(null);
    setTraceCase(null);
    if (!activeRunId || activeRunStatus !== 'completed') return;
    let cancelled = false;
    void (async () => {
      try {
        const m = await getMetrics(activeRunId);
        if (!cancelled) setMetrics(m);
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 409) {
          setMetricsNote('评测尚未完成，暂无指标（409）');
        } else {
          setActionError(toReadableError(error));
        }
      }
      try {
        const r = await getResult(activeRunId);
        if (!cancelled) setResult(r);
      } catch (error) {
        if (!cancelled) setActionError(toReadableError(error));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeRunId, activeRunStatus]);

  // 轮询到终态后刷新 Run 列表：新完成的 run 这才进得了列表/趋势/对比候选
  useEffect(() => {
    if (activeRunStatus === 'completed' || activeRunStatus === 'failed') void loadRuns();
  }, [activeRunStatus, loadRuns]);

  /* --------- 事件处理 --------- */

  /** 选中/发起 Run 的统一入口：置 state 的同时把 ?run=<id> 写回 URL（I4 修复轮）。
   *  此前参数只读不写，"深链可恢复"只对手动改地址成立；发起后/点选后一刷新，
   *  正在跑的 pending run（listRuns 只出终态，别处找不回它）就在 UI 里丢了。
   *  replace=true：选中态是视图细节，不往浏览器历史里堆记录。 */
  const selectRun = (id: string) => {
    setActiveRunId(id);
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set('run', id);
        return next;
      },
      { replace: true },
    );
  };

  const handleAddCase = async () => {
    if (!selectedDatasetId) return;
    const input = caseInput.trim();
    if (!input) {
      setActionError('用例输入（input）不能为空');
      return;
    }
    const keyPoints = caseKeyPoints
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);
    setAddingCase(true);
    setActionError(null);
    try {
      const created = await addCase(selectedDatasetId, {
        input,
        type: caseCategory,
        expected: keyPoints.length ? { key_points: keyPoints } : undefined,
      });
      setCaseInput('');
      setCaseKeyPoints('');
      setHint(`已新增用例 ${created.code}`);
      // 详情与列表（case_count）都要跟着刷新，否则"加成功了但数没变"
      const [d] = await Promise.all([getDataset(selectedDatasetId), loadDatasets()]);
      setDetail(d);
    } catch (error) {
      setActionError(toReadableError(error));
    } finally {
      setAddingCase(false);
    }
  };

  const handleStartRun = async () => {
    const datasetId = runDatasetId || selectedDatasetId;
    if (!datasetId) {
      setActionError('请先选择一个数据集');
      return;
    }
    setStarting(true);
    setActionError(null);
    try {
      const submitted = await startRun({
        dataset_id: datasetId,
        target: runTarget,
        note: runNote.trim() || undefined,
      });
      setRunNote('');
      selectRun(submitted.evaluation_id);
      setHint('评测已发起，后台串行执行中，页面每 2 秒轮询一次进度');
    } catch (error) {
      // W11：同一数据集已有未完成的批次时后端回 409（EVAL_409001）——
      // 走页面级 actionError 给固定可读文案，不白屏、不谎报发起成功。
      if (error instanceof ApiError && error.status === 409) {
        setActionError(
          typeof error.detail === 'string' && error.detail
            ? error.detail
            : '同一数据集已有在跑的评测，请等待完成',
        );
      } else {
        setActionError(toReadableError(error));
      }
    } finally {
      setStarting(false);
    }
  };

  const handleCompare = async () => {
    if (!compareBase || !compareHead) {
      setCompareError('先选择基线 Run 与对比 Run');
      return;
    }
    // W3.2：base === head 是恒等式，不是"没有回归"。发出去的请求必然全行 same，
    // 一张全"持平"的表恰恰是自我对比页最容易读错的东西 —— 不发，文案直说。
    // （isSelfCompare 的展示文案在表单下方，见回归对比 section。）
    if (isSelfCompare) {
      setCompareResult(null);
      setCompareError(null);
      return;
    }
    setComparing(true);
    setCompareError(null);
    try {
      setCompareResult(await compareRuns(compareBase, compareHead));
    } catch (error) {
      // 两侧任一未 completed 后端回 409，这里同样是可读状态而不是崩溃
      setCompareResult(null);
      setCompareError(toReadableError(error));
    } finally {
      setComparing(false);
    }
  };

  /* --------- 派生数据 --------- */

  // 趋势点：listRuns 已按 created_at 倒序，画图转正序（时间从左到右）
  const trendPoints: TrendPoint[] = useMemo(
    () =>
      [...runs]
        .sort(
          (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
        )
        .map((r) => {
          // trendMetric 是 MetricKey：RunMetrics 上直接下标，不经 `as unknown as Record`（M1）
          const raw = r.metrics ? r.metrics[trendMetric] : undefined;
          return {
            id: r.id,
            label: formatTime(r.created_at),
            value: typeof raw === 'number' ? raw : null,
            version: r.target_version,
          };
        }),
    [runs, trendMetric],
  );

  const completedRuns = useMemo(
    () => runs.filter((r) => r.status === 'completed'),
    [runs],
  );

  // 对比结果 → 表行（W3.1）：**响应里凡不是结构性键的键一律出行**。
  // 修前这里是一道白名单过滤器（主键表 ∪ 单边缺席），于是
  // failure_category_dist / cost_available / not_applicable / cases_* 整行蒸发 ——
  // 一张"看两次跑批差在哪"的表被过滤器吃掉了四行，而它恰恰是回归判据本身。
  // COMPARE_PRIMARY_KEYS 从此只决定**顺序**（不在表内的排后面、按键名字典序），
  // 不再决定**可见性**；未知键用 String(raw) 兜底渲（值不是 number 时 compareCellText
  // 本来就走 String 支），后端加新键不需要前端配合改表。
  // 结构性键（case_flips / case_coverage）不是一格指标，各自有专门的渲染区（见下方）。
  const compareRows = useMemo(() => {
    if (!compareResult) return [];
    const primaries: readonly string[] = COMPARE_PRIMARY_KEYS;
    const rows: Array<{ key: string; diff: MetricDiff }> = [];
    for (const [key, value] of Object.entries(compareResult)) {
      if (COMPARE_STRUCTURAL_KEYS.includes(key)) continue;
      rows.push({ key, diff: value as MetricDiff });
    }
    rows.sort((a, b) => {
      const ia = primaries.indexOf(a.key);
      const ib = primaries.indexOf(b.key);
      return (ia === -1 ? primaries.length : ia) - (ib === -1 ? primaries.length : ib)
        || a.key.localeCompare(b.key);
    });
    return rows;
  }, [compareResult]);

  // 对比表头上的身份（W3.2）：一张 Δ 表如果读的人不知道是谁比谁，就等于没有结论。
  const compareSides = useMemo(() => {
    const find = (id: string) => runs.find((r) => r.id === id);
    const nameOf = (id: string | null | undefined) =>
      datasets.find((d) => d.id === id)?.name ?? '未知数据集';
    const side = (id: string) => {
      const r = find(id);
      if (!r) return null;
      return {
        version: r.target_version,
        time: formatTime(r.created_at),
        dataset: nameOf(r.dataset_id),
        cases: `${r.case_done}/${r.case_total} 条`,
      };
    };
    return { base: side(compareBase), head: side(compareHead) };
  }, [runs, datasets, compareBase, compareHead]);

  // 自我对比（base === head）是恒等式，不是"没有回归"（W3.2）：不发请求，直说。
  const isSelfCompare =
    compareBase !== '' && compareBase === compareHead;

  const failedCases = useMemo(
    () => (result?.cases ?? []).filter((c) => !c.passed),
    [result],
  );

  /* --------- 渲染 --------- */

  const runOptions = completedRuns.map((r, i) => ({
    value: r.id,
    label: `#${i + 1} ${formatTime(r.created_at)} · ${r.target_version} · ${r.case_done}/${r.case_total} 条`,
  }));

  return (
    <div className={styles.page}>
      <div className={styles.container}>
        <header className={styles.header}>
          <h1 className={styles.title}>Evaluation 评测</h1>
          <p className={styles.subtitle}>
            数据集与用例、Run 执行与进度、指标与失败下钻、两次 Run 的回归对比（docs/08）。
          </p>
        </header>

        {hint && <p className={styles.successBanner}>{hint}</p>}
        {actionError && <p className={styles.errorBanner}>{actionError}</p>}

        {/* ---- 轮询进度（页面顶部）：running/pending/止损时出现。
              halted 独立判断：首跳就失败时 activeRun 还是 null，重试入口也必须可见 ---- */}
        {activeRunId &&
          (halted ||
            (activeRun && (activeRun.status === 'pending' || activeRun.status === 'running'))) && (
          <section className={styles.progressPanel}>
            <div className={styles.progressHead}>
              <span>
                {activeRun
                  ? `${statusView(activeRun.status).label} · ${activeRun.case_done}/${activeRun.case_total} 条用例`
                  : '进度获取中'}
              </span>
              <span className={styles.progressPercent}>{activeRun ? `${activeRun.progress}%` : '—'}</span>
            </div>
            <div className={styles.progressBar}>
              <div
                className={styles.progressFill}
                style={{ width: `${activeRun?.progress ?? 0}%` }}
              />
            </div>
            <p className={styles.progressNote}>
              后台串行执行，一条用例约 60s，10 条约 15 分钟；页面每 2 秒查询一次进度。
            </p>
            {halted ? (
              <p className={styles.errorBanner}>
                进度获取失败，点击重试{pollFailure ? `（${pollFailure}）` : ''}
                <button type="button" className={styles.actionButton} onClick={retry}>
                  点击重试
                </button>
              </p>
            ) : (
              pollFailure && <p className={styles.progressWarn}>{pollFailure}（继续重试中）</p>
            )}
          </section>
        )}

        {/* ---- 失败收尾：run 级 error_message ---- */}
        {activeRun?.status === 'failed' && (
          <p className={styles.errorBanner}>
            本次评测执行失败{activeRun.error_message ? `：${activeRun.error_message}` : ''}
          </p>
        )}

        {/* ---- 数据集列表 + 详情 ---- */}
        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>数据集与用例</h2>

          {datasetsLoading && (
            <p className={styles.hint}>正在加载数据集…</p>
          )}
          {!datasetsLoading && loadError && (
            <div className={styles.errorCard}>
              <p className={styles.errorCardText}>{loadError}</p>
              <button type="button" className={styles.actionButton} onClick={() => void loadDatasets()}>
                重试
              </button>
            </div>
          )}
          {!datasetsLoading && !loadError && datasets.length === 0 && (
            <p className={styles.hint}>还没有数据集 —— 先跑 backend 的 seed 脚本或调 POST /datasets 建一个。</p>
          )}

          {!datasetsLoading && !loadError && datasets.length > 0 && (
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>名称</th>
                  <th>用例数</th>
                  <th>类别范围</th>
                  <th>创建时间</th>
                </tr>
              </thead>
              <tbody>
                {datasets.map((d) => (
                  <tr
                    key={d.id}
                    className={`${styles.clickableRow} ${
                      selectedDatasetId === d.id ? styles.selectedRow : ''
                    }`}
                    onClick={() => setSelectedDatasetId(d.id)}
                  >
                    <td>{d.name}</td>
                    <td>{d.case_count}</td>
                    <td className={styles.cellMono}>{d.category_scope.join(' / ') || '—'}</td>
                    <td className={styles.cellMuted}>{formatTime(d.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {detail && (
            <div className={styles.detailBlock}>
              <h3 className={styles.detailTitle}>
                {detail.name} · {detail.case_count} 条用例
              </h3>
              {detail.description && <p className={styles.hint}>{detail.description}</p>}
              {detailLoading ? (
                <p className={styles.hint}>正在加载用例明细…</p>
              ) : (
                <table className={styles.table}>
                  <thead>
                    <tr>
                      <th className={styles.colCode}>编号</th>
                      <th className={styles.colCategory}>类别</th>
                      <th>输入</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.cases.map((c) => (
                      <tr key={c.id}>
                        <td className={styles.cellMono}>{c.code}</td>
                        <td className={styles.cellMono}>{c.category}</td>
                        <td className={styles.cellClamp} title={c.input}>
                          {c.input}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              {/* 新增用例：接口只补了新增（D13），编辑/删除不做 */}
              <div className={styles.formBlock}>
                <h4 className={styles.formTitle}>新增用例</h4>
                <div className={styles.formRow}>
                  <label className={styles.formLabel}>
                    类别
                    <select
                      className={styles.select}
                      value={caseCategory}
                      onChange={(e) => setCaseCategory(e.target.value as CaseCategory)}
                    >
                      {CASE_CATEGORIES.map((c) => (
                        <option key={c} value={c}>
                          {c}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                <textarea
                  className={styles.textarea}
                  placeholder="用例输入（必填）：要问被测对象的那句话"
                  value={caseInput}
                  onChange={(e) => setCaseInput(e.target.value)}
                  rows={2}
                />
                <textarea
                  className={styles.textarea}
                  placeholder="期望要点 expected.key_points（一行一条，可留空）"
                  value={caseKeyPoints}
                  onChange={(e) => setCaseKeyPoints(e.target.value)}
                  rows={3}
                />
                <button
                  type="button"
                  className={styles.primaryButton}
                  disabled={addingCase || !selectedDatasetId}
                  onClick={() => void handleAddCase()}
                >
                  {addingCase ? '正在新增…' : '新增用例'}
                </button>
              </div>
            </div>
          )}
        </section>

        {/* ---- 发起 Run ---- */}
        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>发起评测 Run</h2>
          <div className={styles.formRow}>
            <label className={styles.formLabel}>
              数据集
              <select
                className={styles.select}
                value={runDatasetId || selectedDatasetId || ''}
                onChange={(e) => setRunDatasetId(e.target.value)}
              >
                <option value="" disabled>
                  请选择数据集
                </option>
                {datasets.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}（{d.case_count} 条）
                  </option>
                ))}
              </select>
            </label>
            <label className={styles.formLabel}>
              目标
              <select
                className={styles.select}
                value={runTarget}
                onChange={(e) => setRunTarget(e.target.value as RunTarget)}
              >
                {RUN_TARGETS.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </select>
            </label>
            <label className={styles.formLabel}>
              备注（可选）
              <input
                className={styles.input}
                value={runNote}
                maxLength={2000}
                placeholder="这次跑的是什么改动，回归对比时好看"
                onChange={(e) => setRunNote(e.target.value)}
              />
            </label>
            <button
              type="button"
              className={styles.primaryButton}
              disabled={starting}
              onClick={() => void handleStartRun()}
            >
              {starting ? '正在发起…' : '发起 Run'}
            </button>
          </div>
          <p className={styles.hint}>
            target_version 由服务端从 git 现取，前端不传也不见（防自报版本锚）。
          </p>
        </section>

        {/* ---- Run 列表 ---- */}
        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>Run 历史（只列终态；进行中看上方进度条）</h2>
          {runs.length === 0 ? (
            <p className={styles.hint}>还没有已完成的 Run。</p>
          ) : (
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>目标@版本</th>
                  <th>状态</th>
                  <th>用例</th>
                  <th>备注</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr
                    key={r.id}
                    className={`${styles.clickableRow} ${
                      activeRunId === r.id ? styles.selectedRow : ''
                    }`}
                    onClick={() => selectRun(r.id)}
                  >
                    <td className={styles.cellMuted}>{formatTime(r.created_at)}</td>
                    <td className={styles.cellMono}>
                      {r.target}@{r.target_version}
                    </td>
                    <td>
                      <span className={`${styles.badge} ${statusView(r.status).className}`}>
                        {statusView(r.status).label}
                      </span>
                    </td>
                    <td>
                      {r.case_done}/{r.case_total}
                    </td>
                    <td className={styles.cellClamp} title={r.note ?? undefined}>
                      {r.note || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className={styles.hint}>点一行查看该 Run 的指标卡、失败明细与 Trace 下钻。</p>
        </section>

        {/* ---- 趋势 + 占比 ---- */}
        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>指标趋势</h2>
          <div className={styles.formRow}>
            <label className={styles.formLabel}>
              指标
              <select
                className={styles.select}
                value={trendMetric}
                onChange={(e) => setTrendMetric(e.target.value as MetricKey)}
              >
                {TREND_METRICS.map((m) => (
                  <option key={m.key} value={m.key}>
                    {m.label}（{m.key}）
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className={styles.chartRow}>
            <div className={styles.chartCard}>
              <TrendLine
                points={trendPoints}
                metric={trendMetric}
                formatValue={(v) => formatByKind(trendMetric, v)}
              />
            </div>
            <div className={styles.chartCard}>
              <div className={styles.chartTitle}>失败类别占比（选中 Run）</div>
              {/* I1 修复轮：loaded 门 —— metrics 为 null（首帧/run 在跑/failed run）时
                  donut 渲"指标尚未就绪"，不许用空 dist 冒充"本批无失败用例" */}
              <FailureDonut dist={metrics?.failure_category_dist ?? {}} loaded={metrics !== null} />
            </div>
          </div>
        </section>

        {/* ---- 选中 Run 的指标卡 ---- */}
        {activeRun && (
          <section className={styles.section}>
            <h2 className={styles.sectionTitle}>
              选中 Run · {formatTime(activeRun.started_at ?? activeRun.finished_at ?? '')}
              <span className={`${styles.badge} ${statusView(activeRun.status).className}`}>
                {statusView(activeRun.status).label}
              </span>
            </h2>
            {activeRun.status !== 'completed' && (
              <p className={styles.hint}>
                {activeRun.status === 'failed'
                  ? '本次执行失败，没有指标可看。'
                  : '执行中，指标要等 run 完成（metrics 端点在完成前会回 409，不是出错）。'}
              </p>
            )}
            {metricsNote && <p className={styles.hint}>{metricsNote}</p>}
            {metrics && (
              <>
                <div className={styles.metricGrid}>
                  {METRIC_CARDS.map((spec) => (
                    <MetricCard key={spec.key} spec={spec} metrics={metrics} />
                  ))}
                </div>
                <p className={styles.hint}>
                  口径：cases_total={metrics.cases_total} · cases_passed={metrics.cases_passed} ·
                  cases_error={metrics.cases_error} · reviewed_cases={metrics.reviewed_cases} ·
                  category_scope={metrics.category_scope.join('/') || '空'}
                </p>

                {/* ---- 失败明细 ---- */}
                <h3 className={styles.detailTitle}>失败用例明细（passed=false）</h3>
                {failedCases.length === 0 ? (
                  <p className={styles.hint}>本批没有失败用例。</p>
                ) : (
                  <table className={styles.table}>
                    <thead>
                      <tr>
                        <th className={styles.colCode}>#</th>
                        <th>状态</th>
                        <th>得分</th>
                        <th>延迟</th>
                        <th>成本</th>
                        <th>操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {failedCases.map((c) => (
                        <Fragment key={c.case_no}>
                          <tr>
                            <td className={styles.cellMono}>{c.case_no}</td>
                            <td className={styles.cellMono}>{c.status}</td>
                            <td>{c.score ?? '—'}</td>
                            <td>{c.latency_ms !== null ? formatMs(c.latency_ms) : '—'}</td>
                            {/* cost null 渲"未配置"而不是 0（D5），逐用例成本与指标层同口径。
                                M6 修复轮：旧判据 `!== null` 放 undefined（字段整个缺席）过去，
                                formatCost(undefined) 直接 TypeError；而 findings 点名的 ¥NaN
                                只有 Number.isFinite 挡得住（typeof NaN === 'number' 为真）。
                                typeof 前置是为了让 TS 收窄到 number 再进 isFinite。 */}
                            <td>
                              {typeof c.cost === 'number' && Number.isFinite(c.cost)
                                ? formatCost(c.cost)
                                : '未配置'}
                            </td>
                            <td>
                              <button
                                type="button"
                                className={styles.actionButton}
                                onClick={() =>
                                  setExpandedCaseNo(expandedCaseNo === c.case_no ? null : c.case_no)
                                }
                              >
                                {expandedCaseNo === c.case_no ? '收起理由' : '展开理由'}
                              </button>
                              {c.task_run_id && (
                                <button
                                  type="button"
                                  className={styles.actionButton}
                                  onClick={() =>
                                    setTraceCase({ taskRunId: c.task_run_id as string, caseNo: c.case_no })
                                  }
                                >
                                  查看 Trace
                                </button>
                              )}
                            </td>
                          </tr>
                          {expandedCaseNo === c.case_no && (
                            <tr>
                              <td colSpan={6}>
                                <div className={styles.reasonsBox}>
                                  {c.note && <p>{c.note}</p>}
                                  {(c.reasons ?? []).length === 0 && !c.note ? (
                                    <p>该用例没有记录判分理由。</p>
                                  ) : (
                                    <ul>
                                      {(c.reasons ?? []).map((reason, i) => (
                                        <li key={i}>{reason}</li>
                                      ))}
                                    </ul>
                                  )}
                                </div>
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      ))}
                    </tbody>
                  </table>
                )}

                {/* ---- Trace 下钻 ---- */}
                <h3 className={styles.detailTitle}>Trace 下钻</h3>
                <TraceTree taskRunId={traceCase?.taskRunId ?? null} />
              </>
            )}
          </section>
        )}

        {/* ---- 回归对比（docs/08 §6） ---- */}
        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>回归对比</h2>
          <div className={styles.formRow}>
            <label className={styles.formLabel}>
              基线 Run（base）
              <select
                className={styles.select}
                value={compareBase}
                onChange={(e) => {
                  setCompareBase(e.target.value);
                  // W3.2：改选即作废。旧表是上一对 Run 的结论，挂在新一对的选择
                  // 下面就是一个关于别人那次对比的陈述 —— 直接清空，不许留。
                  setCompareResult(null);
                }}
              >
                <option value="">请选择</option>
                {runOptions.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </label>
            <label className={styles.formLabel}>
              当前 Run（head）
              <select
                className={styles.select}
                value={compareHead}
                onChange={(e) => {
                  setCompareHead(e.target.value);
                  setCompareResult(null);  // 同上：改选即作废（W3.2）
                }}
              >
                <option value="">请选择</option>
                {runOptions.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className={styles.primaryButton}
              disabled={comparing || isSelfCompare}
              onClick={() => void handleCompare()}
            >
              {comparing ? '对比中…' : '对比'}
            </button>
          </div>
          {completedRuns.length < 2 && (
            <p className={styles.hint}>回归对比需要至少两个已完成的 Run，当前还不够。</p>
          )}
          {isSelfCompare && (
            <p className={styles.hint}>
              base 与 head 选中的是同一个 Run：自我对比恒为持平，无意义，不发送对比请求。
            </p>
          )}
          {compareError && <p className={styles.errorBanner}>{compareError}</p>}

          {compareResult && (
            <>
              {/* W3.2：表上方写明身份。一张 Δ 表如果读的人不知道是谁比谁，
                  表里的"变差/变好"就落不到任何一次真实的跑批上。 */}
              <p className={styles.hint}>
                {(() => {
                  const sideText = (role: string, s: typeof compareSides.base) =>
                    s
                      ? `${role} ${s.version}@${s.time}（数据集 ${s.dataset} · ${s.cases}）`
                      : `${role} 所选 Run 不在列表（数据可能已清理）`;
                  return `${sideText('base', compareSides.base)} → ${sideText('head', compareSides.head)}`;
                })()}
              </p>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th>指标</th>
                    <th>base → head（Δ）</th>
                    <th>方向</th>
                  </tr>
                </thead>
                <tbody>
                  {compareRows.map(({ key, diff }) => {
                    const dir = dirView(diff);
                    return (
                      <tr key={key}>
                        <td className={styles.cellMono}>{key}</td>
                        <td>
                          {compareCellText(diff.base, key)} → {compareCellText(diff.head, key)}
                          {diff.delta !== null && (
                            <span className={styles.delta}>
                              {' '}
                              (Δ {diff.delta > 0 ? '+' : ''}
                              {formatByKind(key, diff.delta)})
                            </span>
                          )}
                        </td>
                        <td
                          className={dir.className}
                          title={
                            diff.dir === 'missing_base'
                              ? '基线侧没有该指标（head 新增）—— 不可比，不是没回归'
                              : diff.dir === 'missing_head'
                                ? '当前侧没有该指标（相对基线消失）—— 不可比，不是没回归'
                                : diff.lower_is_better
                                  ? '该指标越小越好'
                                  : '该指标越大越好'
                          }
                        >
                          {dir.symbol} {dir.text}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>

              {/* 逐用例翻转：指标均值会掩盖"某条从过变不过"，单独列。
                  W3.3：flips 只统计两侧都跑过的用例，覆盖不等时"没有翻转"四个字
                  必须有可比域限定语，否则就是对覆盖收缩的谎（判据同 D14/W2）。 */}
              <h3 className={styles.detailTitle}>用例翻转</h3>
              {(() => {
                const cov = compareResult.case_coverage;
                const oneSided =
                  cov ? cov.only_in_base.length + cov.only_in_head.length : 0;
                const flips = compareResult.case_flips ?? [];
                return (
                  <>
                    {flips.length === 0 ? (
                      <p className={styles.hint}>
                        {oneSided > 0 && cov
                          ? `两侧共有的 ${cov.comparable_total} 条用例里没有 pass/fail 翻转` +
                            `（可比域只有 ${cov.comparable_total} / base ${cov.base_total} · head ${cov.head_total}）。`
                          : '没有用例在两次 Run 间发生 pass/fail 翻转。'}
                      </p>
                    ) : (
                      <ul className={styles.flipList}>
                        {flips.map((flip) => (
                          <li
                            key={flip.case_no}
                            className={flip.to === 'fail' ? styles.flipBad : styles.flipGood}
                          >
                            用例 #{flip.case_no}：{flip.from} → {flip.to}
                            {flip.to === 'fail' ? '（回归：从过变不过）' : '（修复：从不过变过）'}
                          </li>
                        ))}
                      </ul>
                    )}
                    {cov && oneSided > 0 && (
                      <p className={styles.errorBanner}>
                        ⚠ {oneSided} 条用例只在一侧出现，翻转不可比：
                        {cov.only_in_base.length > 0 &&
                          ` 仅 base 跑过 #${cov.only_in_base.join('、#')}`}
                        {cov.only_in_head.length > 0 &&
                          `${cov.only_in_base.length > 0 ? '；' : ''} 仅 head 跑过 #${cov.only_in_head.join('、#')}`}
                        。上面所有翻转与指标结论只建立在 {cov.comparable_total} 条两侧共有的用例上。
                      </p>
                    )}
                  </>
                );
              })()}
            </>
          )}
        </section>
      </div>
    </div>
  );
}
