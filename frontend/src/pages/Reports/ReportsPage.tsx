/**
 * 报告页（Phase 9b，docs/07 §5.7）。
 *
 * 在这一波之前 `/reports` 只是 `<PlaceholderPage>`（路由里挂着、侧栏 `enabled:false`
 * 标着 `9b`）—— 因为报告当时**不是一等资源**：正文挂在 `task_runs.meta.report` 上，
 * 给不出"列表 / 按 id 取 / 删除"这三种形状。本页消费的是 Phase 9b 新建的
 * `reports` 表与 `GET /api/v1/reports` 两条读口 + 一条删除口。
 *
 * 三个设计约束：
 * 1. **不轮询**。任务列表要轮询是因为执行在后台跑；报告是终态投影，写完就不再变
 *    （没有任何写入方会更新它，重跑会新增一行而不是改旧的）。给一份静止数据挂 2s 轮询
 *    只是白打后端。要刷新有显式按钮。
 * 2. **正文的渲染不在本页实现**：结构化七字段 → `lib/taskReport.ts` 归一 → `components/ReportView`
 *    渲染。本页只负责"取哪一份、旁边显示什么元信息"。归一器的三种形状（结构化 / 降级 /
 *    散文串）与 Agent 任务详情、Workflow 执行详情**共用同一份**，所以同一份报告在三处
 *    长得一样。
 * 3. **`markdown` 为 null 不是"没有报告"**：后端认不出形状时渲不出全文，此时直接按
 *    `content` 渲（上面那条路本来就在）。只有 `content` 也认不出，才说"读不出正文"。
 * 4. **筛出来的空列表不许说"还没有报告"**（本轮加时间档之后新增的口径）：
 *    默认档是 `all`，所以空列表在带筛选时几乎总意味着"这一档里没有"，
 *    而不是"一份都没有" —— 文案要跟着 `hasFilter` 换，并给出清空筛选的出口。
 *    翻页器同一处道理：只有上一页/下一页时，第 40 页回不去（首页/末页/页码直达收在这轮）。
 */
import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { deleteReport, getReport, listReports } from '../../api/reports';
import type { ReportDetail, ReportSummary } from '../../api/reports';
import type { StatsRange } from '../../api/stats';
import { toReadableError } from '../../api/client';
import { formatCost, TRACE_COST_CURRENCY } from '../../lib/format';
import { clampPage, offsetForPage, pageCount, parseJumpPage } from '../../lib/pagination';
import { RANGE_TABS } from '../../lib/ranges';
import { parseTaskReport, sectionHasContent } from '../../lib/taskReport';
import ReportView from '../../components/ReportView';
import MarkdownView from '../../components/MarkdownView';
import styles from './ReportsPage.module.css';

/** 一页多少份。后端 `limit` 上限 200、下限 1（少了 `ge=1` 那个 bug 的教训见 agents.py）。 */
const PAGE_SIZE = 20;

/** 时间档默认值：与后端 `GET /reports?range` 的默认值同一个词。
 *  两处各写一遍就允许"前端默认 today、后端默认 all"这种分叉（页面显示的和实际筛的不是同一窗）。
 *  这条差异是刻意的：stats 默认 today（当日运营），报告台账默认 all（交付物历史）。 */
const DEFAULT_RANGE: StatsRange = 'all';

/** 类型筛选档：'all' + 后端三种 report_type。
 *  中文标签在**前端**这一份（后端只存 analysis/workflow/summary 三个机器值）——
 *  与 TaskStatusBadge 的 TASK_STATUS_LABELS 同一套做法：库里存稳定值，展示层给中文。 */
const TYPE_TABS: { key: string; label: string }[] = [
  { key: '', label: '全部' },
  { key: 'analysis', label: '分析报告' },
  { key: 'workflow', label: 'Workflow 产物' },
  { key: 'summary', label: '文档总结' },
];

const TYPE_LABELS: Record<string, string> = {
  analysis: '分析报告',
  workflow: 'Workflow 产物',
  summary: '文档总结',
};

function typeLabel(value: string): string {
  return TYPE_LABELS[value] ?? value;
}

/** 列表页的日期格：年月日 + 时分即可，秒没有信息量 */
function formatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export default function ReportsPage() {
  // 详情用**路由参数**而不是页内 state：报告有 id、可分享、可刷新回到同一屏，
  // 页内 state 一刷新就丢（docs/07 §4 的路由表为本页留的就是 /reports 与 /reports/:id）。
  const { reportId } = useParams<{ reportId?: string }>();
  return reportId ? <ReportDetailView reportId={reportId} /> : <ReportListView />;
}

/* ---------------- 列表 ---------------- */

function ReportListView() {
  const navigate = useNavigate();
  const [type, setType] = useState('');
  const [range, setRange] = useState<StatsRange>(DEFAULT_RANGE);
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<ReportSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  /** 手动刷新计数：重试按钮与"刷一次"共用同一条取数路径 */
  const [nonce, setNonce] = useState(0);
  /** 页码直达输入框的原始文本（受控，跳成功才清） */
  const [jumpText, setJumpText] = useState('');

  const load = useCallback(async () => {
    return listReports({
      ...(type ? { report_type: type } : {}),
      range,
      limit: PAGE_SIZE,
      offset,
    });
  }, [type, range, offset]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const result = await load();
        if (cancelled) return;
        setItems(result.items);
        setTotal(result.total);
      } catch (err) {
        if (!cancelled) setError(toReadableError(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [load, nonce]);

  /** 换任一档都**同时**把 offset 归零：否则从"全部"的第 3 页切到只有 2 条的"文档总结"
   *  会拿到空列表 —— 看起来像"这个类型没有报告"，而实际是页码越界。
   *  时间档同理（近 7 天往往比全部少得多），所以两件事写在两处而不是只写一处。 */
  const switchType = (next: string) => {
    setType(next);
    setOffset(0);
    setJumpText('');
  };

  const switchRange = (next: StatsRange) => {
    setRange(next);
    setOffset(0);
    setJumpText('');
  };

  const pages = pageCount(total, PAGE_SIZE);
  const page = Math.floor(offset / PAGE_SIZE) + 1;

  /** 跳页：页码先夹进 [1, pages] 再换算 offset（首页/上下页/末页四条按钮共用一条算式）。 */
  const goTo = (next: number) => {
    setOffset(offsetForPage(clampPage(next, pages), PAGE_SIZE));
    setJumpText('');
  };

  /** 页码直达：输入不是 1..pages 的整数就什么都不做（`前往` 按钮此时也是禁用态）。 */
  const jumpTarget = parseJumpPage(jumpText, pages);
  const jumpToPage = () => {
    if (jumpTarget === null) return;
    setOffset(offsetForPage(jumpTarget, PAGE_SIZE));
    setJumpText('');
  };

  /** 是否带着筛选条件（默认档 all + 全部类型算"没筛"）。空列表的文案要看它脸色。 */
  const hasFilter = type !== '' || range !== DEFAULT_RANGE;
  const clearFilters = () => {
    setType('');
    setRange(DEFAULT_RANGE);
    setOffset(0);
    setJumpText('');
  };
  const filterText = `${type === '' ? '全部类型' : typeLabel(type)} · ${
    RANGE_TABS.find((tab) => tab.key === range)?.label ?? range
  }`;

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>报告</h1>
        <p className={styles.subtitle}>
          每一次跑出报告的执行都会在这里留一份；点标题看正文，右侧进来源执行的 Trace。
        </p>
        <div className={styles.toolbar}>
          <div className={styles.filters}>
            <div className={styles.filterGroup}>
              <span className={styles.filterLabel}>类型</span>
              <nav className={styles.tabs} aria-label="报告类型">
                {TYPE_TABS.map((tab) => (
                  <button
                    key={tab.key || 'all'}
                    type="button"
                    className={tab.key === type ? styles.tabActive : styles.tab}
                    onClick={() => switchType(tab.key)}
                  >
                    {tab.label}
                  </button>
                ))}
              </nav>
            </div>
            <div className={styles.filterGroup}>
              <span className={styles.filterLabel}>时间</span>
              <nav className={styles.tabs} aria-label="时间范围">
                {RANGE_TABS.map((tab) => (
                  <button
                    key={tab.key}
                    type="button"
                    className={tab.key === range ? styles.tabActive : styles.tab}
                    onClick={() => switchRange(tab.key)}
                  >
                    {tab.label}
                  </button>
                ))}
              </nav>
            </div>
          </div>
          <div className={styles.toolbarRight}>
            <span className={styles.hint}>共 {total} 份</span>
            <button type="button" className={styles.linkButton} onClick={() => setNonce((n) => n + 1)}>
              刷新
            </button>
          </div>
        </div>
      </header>

      {error && (
        <div className={styles.errorCard}>
          <p className={styles.errorText}>{error}</p>
          <button type="button" className={styles.linkButton} onClick={() => setNonce((n) => n + 1)}>重试</button>
        </div>
      )}

      {loading && items.length === 0 ? (
        <p className={styles.hint}>正在加载报告…</p>
      ) : items.length === 0 ? (
        <div className={styles.emptyCard}>
          {hasFilter ? (
            // 带着筛选条件时空列表**不许**说"还没有报告"：那等于把"这批筛不出来"讲成"没有过报告"。
            // 尤其时间档：默认 all 与 today 差出的是一份份真实存在的交付物。
            <>
              <p>当前筛选条件下没有报告。</p>
              <p className={styles.hint}>
                这份台账在「{filterText}」这一档里是空的。
                <button type="button" className={styles.linkButton} onClick={clearFilters}>清空筛选</button>
              </p>
            </>
          ) : (
            <>
              <p>还没有报告。</p>
              <p className={styles.hint}>
                去 <Link className={styles.link} to="/agents">Agent 任务</Link> 提交一次分析，
                或到 <Link className={styles.link} to="/workflows">Workflow</Link> 跑一条图 ——
                执行成功且产出报告后，这里就会出现一行。
              </p>
            </>
          )}
        </div>
      ) : (
        <table className={styles.table}>
          <thead>
            <tr>
              <th>标题</th>
              <th className={styles.colType}>类型</th>
              <th className={styles.colNum}>Token</th>
              <th className={styles.colNum}>成本</th>
              <th className={styles.colTime}>生成时间</th>
              <th className={styles.colAction}>来源</th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr key={row.id}>
                <td>
                  <button
                    type="button"
                    className={styles.titleButton}
                    onClick={() => navigate(`/reports/${row.id}`)}
                  >
                    {row.title}
                  </button>
                  {row.reviewer_verdict && row.reviewer_verdict !== 'pass' && (
                    <span className={styles.warnBadge}>{row.reviewer_verdict}</span>
                  )}
                </td>
                <td className={styles.colType}>{typeLabel(row.report_type)}</td>
                <td className={styles.colNum}>{row.total_tokens}</td>
                {/* 成本口径与 Trace 页同一处常量（docs/07 §5.6 的已知局限：载荷里没有 currency） */}
                <td className={styles.colNum}>{formatCost(row.cost, TRACE_COST_CURRENCY)}</td>
                <td className={styles.colTime}>{formatTime(row.created_at)}</td>
                <td className={styles.colAction}>
                  {/* 来源任务可空：task 被删后外键 SET NULL，报告本身仍在（这是刻意口径，不是 bug） */}
                  {row.task_id ? (
                    <Link className={styles.link} to={`/agents/${row.task_id}`}>看 Trace</Link>
                  ) : (
                    <span className={styles.hint}>来源已删</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {total > PAGE_SIZE && (
        // 五件套而不是只有上一页/下一页：这份台账会长到几十页，
        // 只有单步按钮时"回到第一页"要点 N 次，而首页/末页是零成本的两个动作。
        // 页码直达框认不出的输入不响应（按钮同时禁用），不夹到末页 —— 见 lib/pagination.ts。
        <div className={styles.pager}>
          <button
            type="button"
            className={styles.linkButton}
            disabled={page === 1}
            onClick={() => goTo(1)}
          >
            首页
          </button>
          <button
            type="button"
            className={styles.linkButton}
            disabled={page === 1}
            onClick={() => goTo(page - 1)}
          >
            上一页
          </button>
          <span className={styles.hint}>第 {page} / {pages} 页</span>
          <input
            className={styles.pageInput}
            type="text"
            inputMode="numeric"
            aria-label="跳转到页码"
            placeholder="页码"
            value={jumpText}
            onChange={(event) => setJumpText(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') jumpToPage(); }}
          />
          <button
            type="button"
            className={styles.linkButton}
            disabled={jumpTarget === null}
            onClick={jumpToPage}
          >
            前往
          </button>
          <button
            type="button"
            className={styles.linkButton}
            disabled={page >= pages}
            onClick={() => goTo(page + 1)}
          >
            下一页
          </button>
          <button
            type="button"
            className={styles.linkButton}
            disabled={page >= pages}
            onClick={() => goTo(pages)}
          >
            末页
          </button>
        </div>
      )}
    </div>
  );
}

/* ---------------- 详情 ---------------- */

function ReportDetailView({ reportId }: { reportId: string }) {
  const [report, setReport] = useState<ReportDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  const load = useCallback(async () => {
    return getReport(reportId);
  }, [reportId]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const detail = await load();
        if (!cancelled) setReport(detail);
      } catch (err) {
        if (!cancelled) setError(toReadableError(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [load]);

  const remove = async () => {
    // 删除是不可逆的（报告行删了就没了；正文虽能从 task_run 重投影，但那是运维动作）。
    // 不用 window.confirm 之外的花样：本仓没有模态框基建，为一次确认引入一套不划算。
    if (!window.confirm('删除这份报告？来源任务与 Trace 不受影响。')) return;
    setBusy(true);
    setError(null);
    try {
      await deleteReport(reportId);
      navigate('/reports', { replace: true });
    } catch (err) {
      setError(toReadableError(err));
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <p className={styles.hint}>正在加载报告…</p>;
  if (!report) {
    return (
      <div className={styles.page}>
        <div className={styles.errorCard}>
          <p className={styles.errorText}>{error ?? '报告不存在或不属于你'}</p>
          <Link className={styles.link} to="/reports">回报告列表</Link>
        </div>
      </div>
    );
  }

  // 归一交给 lib（三种形状都收、认不出返回 null）；本页只决定"渲哪一份"。
  const parsed = parseTaskReport(report.content);
  const hasParsedContent = parsed !== null &&
    (parsed.summary.trim().length > 0 ||
      parsed.sections.some(sectionHasContent) ||
      parsed.warnings.length > 0);

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.backRow}>
          <Link className={styles.link} to="/reports">← 报告列表</Link>
        </div>
        <h1 className={styles.title}>{report.title}</h1>
        <dl className={styles.stats}>
          <div><dt>类型</dt><dd>{typeLabel(report.report_type)}</dd></div>
          <div><dt>状态</dt><dd>{report.status === 'final' ? '定稿' : report.status}</dd></div>
          <div><dt>复核结论</dt><dd>{report.reviewer_verdict ?? '—'}</dd></div>
          <div><dt>Token</dt><dd>{report.total_tokens}</dd></div>
          <div><dt>成本</dt><dd>{formatCost(report.cost, TRACE_COST_CURRENCY)}</dd></div>
          <div><dt>生成时间</dt><dd>{formatTime(report.created_at)}</dd></div>
        </dl>
        <div className={styles.actions}>
          {report.task_id ? (
            <Link className={styles.linkButton} to={`/agents/${report.task_id}`}>来源执行与 Trace</Link>
          ) : (
            <span className={styles.hint}>来源任务已删除（报告本身保留）</span>
          )}
          <button type="button" className={styles.dangerButton} disabled={busy} onClick={() => void remove()}>
            删除报告
          </button>
        </div>
      </header>

      {error && (
        <div className={styles.errorCard}>
          <p className={styles.errorText}>{error}</p>
        </div>
      )}

      {hasParsedContent ? (
        <ReportView
          report={parsed}
          rawLabel="报告原文（库里的 markdown）"
          // 折叠区优先给后端的 markdown 全文（它是"报告该长什么样"的权威渲染）；
          // markdown 为 null（认不出形状）时退回结构化 content 的原文 JSON。
          raw={report.markdown ?? report.content}
        />
      ) : report.markdown ? (
        // 结构化认不出但后端渲得出全文：直接给全文，不让用户对着一份"认不出"的空屏。
        <section className={styles.fallback}>
          <h2 className={styles.sectionTitle}>报告正文</h2>
          <MarkdownView content={report.markdown} />
        </section>
      ) : (
        <div className={styles.emptyCard}>
          <p>这份报告读不出正文。</p>
          <p className={styles.hint}>
            它的结构化字段与渲染全文都为空 —— 这通常意味着它是早期执行留下的行。
            来源执行的 Trace 仍可查（上方入口）。
          </p>
        </div>
      )}
    </div>
  );
}
