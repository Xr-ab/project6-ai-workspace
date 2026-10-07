/**
 * 任务报告归一（Phase 9b 前置件，收 R56）。
 *
 * 为什么要有这个文件：在它之前，**没有任何页面渲染得到报告正文**。
 *   - `/agents/:taskId` 只认字符串（旧 `TaskDetailPage` 那句 `typeof run?.meta?.report === 'string'`），
 *     而产品自 Phase 5 起写进 `task_run.meta.report` 的是七个字段的结构化字典
 *     （`app/ai/graph/nodes/report.py:25-32` 的 `Report` → `:55` 的 `model_dump()`
 *     → `app/application/task_runner.py:217`）⇒ 判据恒 false，整个「分析报告」区从不出现。
 *     降级支路（`report.py:59`）写的是 `{executive_summary, content}`，同样被拒。
 *   - Workflow 详情页只从 `meta.result.report` 里挑 `executive_summary` 一段（旧 `ResultBlock`），
 *     七个字段里剩下六个只在「执行 meta」的 `<details>` 裸 JSON 里看得见。
 * 两处都是同一个根因：**消费侧按自己臆想的形状取**，而不是按产品真写的形状取。
 *
 * 为什么是纯函数而不是写在组件里（与 `lib/traceTree.ts` 同一条纪律）：归一里全是分支
 * （字符串 / 字典 / 降级 / 认不出的形状 / 空壳），住在组件里就只能靠浏览器手点复现。
 * 抽到 `lib/` 之后 `vitest`（environment: 'node'）能零 DOM 直接断言，收据可重跑。
 *
 * 两条不骗人的规矩：
 *   1. **认不出就返回 null，不猜**。调用方拿 null 时照旧走「没记录到」的实话，
 *      而不是渲染一个空标题骗人（`format.ts` 的同一族规矩：不可得的数不许用 0 顶）。
 *   2. 不认识的字段**不丢**，按原键名排在已知七字段之后展示 —— 产品以后加字段时，
 *      旧前端是"少标一个中文标签"，不是"少显示一段正文"。
 */
import type { TaskRunInfo } from '../api/agent';

/** 报告里的一段：prose = 一段散文（走 MarkdownView），items = 逐条列表。 */
export interface TaskReportSection {
  /** 字段键名（如 `key_findings`），认不出的键原样保留 */
  key: string;
  /** 中文标签；键不在已知表里时回落成键名本身 */
  label: string;
  /** 散文正文（与 items 二选一，可能都缺 —— 见下面的空壳口径） */
  prose: string | null;
  /** 逐条内容（已 `.trim()` 并剔掉空白项） */
  items: string[];
}

export interface TaskReport {
  /** 摘要一段，单独置顶（它的标签在正文里不再重复一次） */
  summary: string;
  sections: TaskReportSection[];
  /** 复核未通过却被放行时挂的警告（`report.py:68` 的 `review_warning`） */
  warnings: string[];
  /** 归一之前的原值：渲染层用它做「报告原文」折叠区（排障要看的是库里的那一份，不是我们重排过的） */
  raw: unknown;
}

/**
 * 字段键 → 中文标签（**唯一一份**，别处不许再抄）。
 *
 * 这张表就是 docs/03 §2.6 那七个字段的呈现面。它与产品模型的对应关系由后端针
 * `backend/tests/unit/test_report_render_contract.py` **同进程**钉住（读
 * `app.ai.graph.nodes.report.Report.model_fields` 比对本表的键集）——与 Demo 2 那份
 * `test_demo2_report_fields_match_the_product_schema` 同一套办法，理由也一样：
 * 产品加/改字段时，红的是针而不是下一次演示。
 *
 * 导出它是因为那条后端针要读它；前端内部只经 `parseTaskReport` 使用。
 */
export const REPORT_FIELD_LABELS: Record<string, string> = {
  executive_summary: '结论摘要',
  key_findings: '核心发现',
  data_evidence: '数据依据',
  root_causes: '原因分析',
  risks: '风险提示',
  recommendations: '可执行建议',
  sources: '来源',
};

/** 展示顺序：照 `Report` 模型的字段声明序（报告是给人顺着读的，不是 JSON 键序）。
 *  实况落库后 JSONB 会按「键短者在前」重排（`risks` 恒排第一，Demo 2 读出来过），
 *  所以指望对象自身的键序本身就是个坑 —— 顺序必须由这份常量定。 */
export const REPORT_FIELD_ORDER: readonly string[] = [
  'executive_summary',
  'key_findings',
  'data_evidence',
  'root_causes',
  'risks',
  'recommendations',
  'sources',
];

/** 降级支路（`report.py:59`）写的第二个散文键：`{executive_summary, content}`。
 *  并进摘要一段、不再另起一节 —— 两者在产品里就是 `analysis` 的同一份原文，
 *  分两处渲染等于把同一段字印两遍。**只在两值真相等时并**：万一模型把它们写岔了，
 *  分开展示才是实话（摘要是结论、content 是原文），所以这条不是无条件键黑名单。 */
const DEGRADED_CONTENT_KEY = 'content';

/** `review_warning: string[]` 之外的形状兜底用：单条字符串也收 */
function collectWarnings(value: unknown): string[] {
  if (typeof value === 'string') return value.trim() ? [value.trim()] : [];
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item).trim()).filter((item) => item.length > 0);
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === 'string' && value.trim().length > 0 ? value : null;
}

/** 一个字段值 → 一段：字符串是散文，数组是逐条，其余（数字/嵌套对象）JSON 串化兜底。 */
function toSection(key: string, value: unknown): TaskReportSection | null {
  const label = REPORT_FIELD_LABELS[key] ?? key;
  if (typeof value === 'string') {
    const prose = nonEmptyString(value);
    return prose ? { key, label, prose, items: [] } : null;
  }
  if (Array.isArray(value)) {
    const items = value
      .map((item) => (typeof item === 'string' ? item.trim() : JSON.stringify(item)))
      .filter((item): item is string => typeof item === 'string' && item.length > 0);
    return items.length > 0 ? { key, label, prose: null, items } : null;
  }
  // 认不出的值是**有内容**的（数字、嵌套对象），不许当空丢掉 —— 串化后照常展示。
  if (value === null || value === undefined) return null;
  return { key, label, prose: JSON.stringify(value), items: [] };
}

/**
 * 把一段散文切成「首段 = 摘要，其余按裸标题行分节」。
 *
 * 只认**行首**的 `★ 标题：` / `**标题**` / `# 标题`，且标题不超过 20 字：
 * 普通句子里的冒号（「结论：本月下滑」这种出现在行中间、或标题过长）不会被误判成节，
 * 假节会让正文被切碎、标签乱飞，比不切更糟。
 */
function splitProse(text: string): Pick<TaskReport, 'summary' | 'sections'> {
  const lines = text.split(/\r?\n/);
  const sections: TaskReportSection[] = [];
  let summary: string | null = null;
  let buffer: string[] = [];
  let current: { key: string; label: string } | null = null;

  const flush = () => {
    const body = buffer.join('\n').trim();
    buffer = [];
    if (!body) return;
    if (current === null) {
      // 标题之前的正文：第一段当摘要，再来的续段并入摘要（不丢字）
      summary = summary === null ? body : `${summary}\n\n${body}`;
      return;
    }
    sections.push({ key: current.key, label: current.label, prose: body, items: [] });
  };

  for (const line of lines) {
    const heading = matchHeading(line);
    if (heading) {
      flush();
      current = heading;
      continue;
    }
    buffer.push(line);
  }
  flush();
  return { summary: summary ?? '', sections };
}

/** 裸标题行 → {key,label}；不是标题返回 null。key 用原文，保证节内不重名不乱序。 */
function matchHeading(line: string): { key: string; label: string } | null {
  const raw = line.trim();
  if (!raw || raw.length > 32) return null;
  const stripped = raw
    .replace(/^#{1,6}\s*/, '')
    .replace(/^\*\*(.+?)\*\*[：:]?$/, '$1')
    .replace(/[：:]$/, '')
    .trim();
  if (!stripped || stripped.length > 20) return null;
  // 已知字段的中文标签：**只在它独占短暂行时**才算标题（`结论摘要` / `结论摘要：` / `## 结论摘要`）。
  // 为什么加这道窄化：不窄的话，一句以「结论摘要」四字开头的散文（「结论摘要如下，本季……」）
  // 会被当成标题切开，正文被切碎、标签乱飞——假节比不切更糟。≤12 字是"四个字 + 冒号 + 一点空格"的量级。
  const known = Object.entries(REPORT_FIELD_LABELS).find(([, label]) => label === stripped);
  if (known && raw.length <= 12) return { key: known[0], label: known[1] };
  // 未知标题：只认**明确带标题记号**的（`#` / `**` / 行尾冒号 + 短）避免切碎散文
  const looksLikeHeading =
    /^#{1,6}\s/.test(raw) || /^\*\*.+\*\*[：:]?$/.test(raw) || raw.endsWith('：') || raw.endsWith(':');
  return looksLikeHeading ? { key: stripped, label: stripped } : null;
}

/**
 * 报告归一：产品真写的两种形状（结构化七字段 / 降级双字段）都收，散文串也收。
 *
 * @returns `null` = 这条 run 没有报告，或形状认不出 —— 调用方照实说"没记录到"。
 */
export function parseTaskReport(raw: unknown): TaskReport | null {
  if (typeof raw === 'string') {
    const text = raw.trim();
    if (!text) return null;
    const { summary, sections } = splitProse(text);
    return { summary, sections, warnings: [], raw };
  }
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) return null;

  const record = raw as Record<string, unknown>;
  const warnings = [
    ...collectWarnings(record.review_warning),
    ...collectWarnings(record.review_warnings),
  ];

  const summary = nonEmptyString(record.executive_summary) ?? nonEmptyString(record.summary) ?? '';

  const keys = [
    ...REPORT_FIELD_ORDER.filter((key) => key in record),
    ...Object.keys(record).filter((key) => !REPORT_FIELD_ORDER.includes(key)),
  ];
  const sections: TaskReportSection[] = [];
  for (const key of keys) {
    if (key === 'executive_summary' || key === 'summary') continue;
    if (key === 'review_warning' || key === 'review_warnings') continue;
    // 降级支路的重复原文：只在与摘要逐字相同时并掉（见 DEGRADED_CONTENT_KEY 注释）
    if (key === DEGRADED_CONTENT_KEY && nonEmptyString(record[key]) === summary && summary) continue;
    const section = toSection(key, record[key]);
    if (section) sections.push(section);
  }

  // 摘要与各段全空 = 空壳：不给它一句"有报告"，返回 null 让调用方说实话。
  if (!summary && sections.length === 0) {
    return warnings.length > 0 ? { summary: '', sections: [], warnings, raw } : null;
  }
  return { summary, sections, warnings, raw };
}

/** 便捷入口：从一次执行（`TaskRunInfo`）的 meta 里取报告。
 *  这里只做"取位 + 归一"，不关心页面 —— 两处详情页共用同一条取数口径。 */
export function reportFromRun(run: TaskRunInfo | null | undefined): TaskReport | null {
  if (!run || run.meta === null || typeof run.meta !== 'object') return null;
  return parseTaskReport((run.meta as Record<string, unknown>).report);
}

/** Workflow 面：报告在 `meta.result.report`（`sales_analysis.py:150` 的 final_report 装配）。 */
export function reportFromWorkflowRun(run: TaskRunInfo | null | undefined): TaskReport | null {
  if (!run || run.meta === null || typeof run.meta !== 'object') return null;
  const result = (run.meta as Record<string, unknown>).result;
  if (result === null || typeof result !== 'object' || Array.isArray(result)) return null;
  return parseTaskReport((result as Record<string, unknown>).report);
}

/** 有内容才算一段：空壳节在渲染层不占位（`parseTaskReport` 已经滤过一道，这里是渲染入口的第二道）。 */
export function sectionHasContent(section: TaskReportSection): boolean {
  return section.prose !== null || section.items.length > 0;
}

/** 归一结果拆成「正文段」与「引用来源段」—— 来源独立折叠区的唯一口径（Phase 9b 留白收口）。 */
export interface SplitSources {
  /** 除来源以外的节，顺序不变（渲染层照它画正文） */
  bodySections: TaskReportSection[];
  /** `key === 'sources'` 那一节；没有就 null（不是"来源为空"，是"这条报告没有来源字段"） */
  sources: TaskReportSection | null;
}

/**
 * 把 `sources` 一节从正文里挑出去。
 *
 * 为什么是纯函数而不是在 `ReportView` 里 `filter` 一下（同本文件头注那条纪律）：
 * 这条判据只有一个键名，看着不值得抽，但**它是"折叠起来的那一段还是不是报告的一部分"
 * 的唯一答案**。写在组件里就没有针 —— 而它的失败形状是静默的：键名改错（比如按中文
 * 标签 '来源' 去匹）会让折叠区从不出现、来源又留在正文里，页面看起来完全正常；
 * 反过来判据太宽会把别的节也折起来，用户要点开才看得到正文。两种都要针拦。
 *
 * 认 `key`（字段名）不认 `label`（中文）：标签是文案，会改；键名是产品模型的字段名，
 * 与后端 `Report` 同源于 `REPORT_FIELD_LABELS` 那张表（另有后端针双向对账）。
 */
export function splitSources(report: TaskReport): SplitSources {
  const sourcesKey = 'sources';
  const found = report.sections.find((section) => section.key === sourcesKey);
  if (!found || !sectionHasContent(found)) {
    return { bodySections: report.sections.filter(sectionHasContent), sources: null };
  }
  return {
    bodySections: report.sections.filter((section) => section !== found && sectionHasContent(section)),
    sources: found,
  };
}

/** 折叠区标题里的条数：列表按条算，散文按一段算（拿不到 0，空段在上面就返回 null 了）。 */
export function sourceCount(sources: TaskReportSection): number {
  return sources.items.length > 0 ? sources.items.length : 1;
}
