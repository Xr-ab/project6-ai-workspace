/**
 * 结构化报告的呈现区（R56 收口件）。
 *
 * 两处详情页共用：`/agents/:taskId`（报告在 `run.meta.report`）与 Workflow 执行详情
 * （报告在 `run.meta.result.report`）。归一全在 `lib/taskReport.ts` 里做完，本组件
 * 只负责画——它拿到的已经是「摘要一段 + 有序的若干节 + 警告若干条」，不含任何
 * 形状判断。这样将来产品加字段时，要改的只有 `REPORT_FIELD_LABELS` 一处。
 *
 * 为什么正文的每一块都走 `MarkdownView` 而不是纯 `<p>`：**模型产出的就是 Markdown**
 * （`REPORT_PROMPT` 里要求分点、带表格），纯 `<p>` 会把表格渲成一堆竖线、
 * 把 `**加粗**` 原样打出来。列表字段（key_findings 等）逐条 `- ` 拼成列表再交给同一个渲染器，
 * 与散文段保持同一套排版（docs/07 §6.2 的统一渲染口径）。
 *
 * 认不出形状时**不渲染本组件**（调用方拿 null 自己走"没记录到"的实话）——所以这里
 * 没有"加载失败"之类的分支。
 *
 * 引用来源（`sources`）折成独立一块、不与其他六段同排（Phase 9b 收口那处留白）：
 * 判据住在 `lib/taskReport.ts::splitSources` 而不是这里，因为"折出去的那一段还算不算
 * 报告正文"是一条会静默出错的判据（键名写错 ⇒ 折叠区从不出现而来源留在正文里，页面
 * 看起来毫无异常），只能靠针拦。折在这里意味着**三处消费者一起变**（报告详情、
 * Agent 任务详情、Workflow 执行详情）——同一份报告在三处长得一样，是本组件存在的全部理由。
 */
import type { TaskReport } from '../lib/taskReport';
import { sourceCount, splitSources } from '../lib/taskReport';
import MarkdownView from './MarkdownView';
import styles from './ReportView.module.css';

interface Props {
  report: TaskReport;
  /** 段落标题；Workflow 详情页的产物区已经在标题里写了"产物"，可传空串省掉这一层 */
  title?: string;
  /** 原始 JSON 折叠区的标题（两页的定位一致：正文明读，JSON 供排障） */
  rawLabel?: string;
  /** 原始值：给了才有折叠区。详情页传 `meta.report` / `result.report` 原文 */
  raw?: unknown;
}

/** 逐条内容 → Markdown 无序列表（`- ` 前缀，与散文段共用渲染器与排版）。 */
function toBulletMarkdown(items: string[]): string {
  return items.map((item) => `- ${item}`).join('\n');
}

/** 一段内容（散文或逐条）交给同一个 Markdown 渲染器 —— 正文与来源区排版一致。 */
function SectionBody({ prose, items }: { prose: string | null; items: string[] }) {
  return (
    <>
      {prose !== null && <MarkdownView content={prose} />}
      {items.length > 0 && <MarkdownView content={toBulletMarkdown(items)} />}
    </>
  );
}

export default function ReportView({ report, title = '分析报告', rawLabel, raw }: Props) {
  const { bodySections, sources } = splitSources(report);
  const hasSummary = report.summary.trim().length > 0;
  // 三块全空就不占版面（调用方本该给 null，这里是第二道闸）
  if (!hasSummary && bodySections.length === 0 && sources === null && report.warnings.length === 0) {
    return null;
  }

  return (
    <div className={styles.report}>
      {title && <h2 className={styles.reportTitle}>{title}</h2>}

      {report.warnings.length > 0 && (
        <div className={styles.warning} role="note">
          <span className={styles.warningLabel}>复核警告</span>
          <ul className={styles.warningList}>
            {report.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      {hasSummary && (
        <div className={styles.summary}>
          <MarkdownView content={report.summary} />
        </div>
      )}

      {bodySections.map((section) => (
        <section className={styles.section} key={`${section.key}:${section.label}`}>
          <h3 className={styles.sectionLabel}>{section.label}</h3>
          <SectionBody prose={section.prose} items={section.items} />
        </section>
      ))}

      {/* 引用来源折成一块：它是"这份报告凭什么"的凭证，不是结论的一部分。
          摊在正文末尾时它和「可执行建议」抢版面，读者要么全读要么全跳过；
          折起来之后标题上的条数是常驻信息——"这份报告引了 0 条还是 6 条"
          不点开就知道，才谈得上信不信它。默认收起。 */}
      {sources !== null && (
        <details className={styles.sources}>
          <summary className={styles.sourcesSummary}>
            {`${sources.label}（${sourceCount(sources)} 条）`}
          </summary>
          <div className={styles.sourcesBody}>
            <SectionBody prose={sources.prose} items={sources.items} />
          </div>
        </details>
      )}

      {raw !== undefined && rawLabel && (
        <details className={styles.rawDetails}>
          <summary className={styles.rawSummary}>{rawLabel}</summary>
          <pre className={styles.rawPre}>{JSON.stringify(raw, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}
