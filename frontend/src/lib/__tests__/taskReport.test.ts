/** lib/taskReport.ts 的针（R56 收口：报告正文的呈现面）。
 *
 *  为什么值得测：收 R56 之前，`/agents/:taskId` 与 Workflow 详情页**都没有**报告正文的
 *  呈现区 —— 一个按 `typeof report === 'string'` 取（产品写的是字典）、一个只挑
 *  `executive_summary` 一段。这条缺陷活了两段（Phase 9a 起）没被任何针拦下，因为
 *  「归一」住在组件里，而本仓的 vitest 边界是 `environment: 'node'`、零 DOM（vitest.config.ts:6）。
 *  归一抽成纯函数之后，这条面才第一次有针。字段键与产品模型的对应关系另有后端同进程针
 *  （`backend/tests/unit/test_report_render_contract.py`）——本文件只管归一的形状。
 *
 *  期望值取值口径：字典类用例的形状逐字照 `report.py` 的两种产出（结构化七字段 /
 *  降级双字段），并刻意用 Demo 2 真跑读出来的**反常规键序**（`risks` 在最前，jsonb
 *  按「键短者在前」重排）——那正是「指望对象键序」这个坑的物证。
 *
 *  末尾 `splitSources` 一组是 Phase 9b 三条 UI 留白之二（引用来源独立折叠区）的针：
 *  判据本身只有一行，但它的失败是静默的（折叠区从不出现 + 来源仍在正文 = 页面看着正常），
 *  所以连「空壳不折叠」「认 key 不认 label」两条边界也钉住。
 */
import { describe, expect, it } from 'vitest';

import type { TaskRunInfo } from '../../api/agent';
import {
  REPORT_FIELD_LABELS,
  REPORT_FIELD_ORDER,
  parseTaskReport,
  reportFromRun,
  reportFromWorkflowRun,
  sectionHasContent,
  sourceCount,
  splitSources,
} from '../taskReport';

/** 一条真跑形状的结构化报告：七个字段齐，键序按 PostgreSQL jsonb 的重排结果（`risks` 在前）。 */
const STRUCTURED = {
  risks: ['渠道结构变化的风险未量化', '上月窗口数据为空'],
  sources: ['query_sales 2026Q3'],
  key_findings: ['8 月环比下降 12%', '华东区贡献了降幅的 70%'],
  data_evidence: ['sales_2026q3.csv 逐月汇总'],
  root_causes: ['促销活动集中在 7 月'],
  recommendations: ['9 月补一次区域定向促销'],
  executive_summary: '本月销售下滑主要由渠道结构与促销节奏错位造成。',
};

function runWithMeta(meta: Record<string, unknown> | null): TaskRunInfo {
  return {
    id: 'run-1',
    run_no: 1,
    status: 'completed',
    progress: 100,
    failure_category: null,
    error_message: null,
    started_at: '2026-10-02T09:00:00Z',
    finished_at: '2026-10-02T09:01:00Z',
    meta,
  };
}

describe('parseTaskReport · 结构化七字段（产品主路径）', () => {
  it('R56 根因：字典报告不再被判成"没有报告"', () => {
    const report = parseTaskReport(STRUCTURED);
    expect(report).not.toBeNull();
    expect(report?.summary).toBe('本月销售下滑主要由渠道结构与促销节奏错位造成。');
  });

  it('节顺序由 REPORT_FIELD_ORDER 定，不跟落库后的 jsonb 键序走', () => {
    const report = parseTaskReport(STRUCTURED);
    expect(report?.sections.map((section) => section.key)).toEqual([
      'key_findings',
      'data_evidence',
      'root_causes',
      'risks',
      'recommendations',
      'sources',
    ]);
  });

  it('七个字段各有中文标签，且标签表与顺序表键集一致（漂移就地红）', () => {
    expect(Object.keys(REPORT_FIELD_LABELS).sort()).toEqual([...REPORT_FIELD_ORDER].sort());
    const report = parseTaskReport(STRUCTURED);
    for (const section of report?.sections ?? []) {
      expect(section.label).toBe(REPORT_FIELD_LABELS[section.key]);
      // 中文标签不等于键名 = 真翻译过；照抄键名会让这一节白做
      expect(section.label).not.toBe(section.key);
    }
  });

  it('列表字段逐条落 items，字符串字段落 prose（两种形状都取到）', () => {
    const report = parseTaskReport(STRUCTURED);
    const findings = report?.sections.find((section) => section.key === 'key_findings');
    expect(findings?.items).toEqual(['8 月环比下降 12%', '华东区贡献了降幅的 70%']);
    expect(findings?.prose).toBeNull();
    const single = parseTaskReport({ executive_summary: '摘要', content: '正文一段' });
    const content = single?.sections.find((section) => section.key === 'content');
    expect(content?.prose).toBe('正文一段');
    expect(content?.items).toEqual([]);
  });

  it('认不出的字段不丢：排在已知七字段之后，标签回落成键名', () => {
    const report = parseTaskReport({ ...STRUCTURED, confidence: 0.82 });
    // 用下标取末项而不是 `.at(-1)`：tsconfig 的 target 是 ES2021，`at` 要 ES2022 才认，
    // 而这条针的职责不是逼项目抬 target（本仓 tsc strict 下 build 必须是零错误）。
    const last = report?.sections[report.sections.length - 1];
    expect(last).toEqual({
      key: 'confidence',
      label: 'confidence',
      prose: '0.82',
      items: [],
    });
  });

  it('空数组 / 空串 / null 字段各自不占位（空壳节会把版面撑出一排空标题）', () => {
    const report = parseTaskReport({
      executive_summary: '摘要',
      key_findings: [],
      data_evidence: '   ',
      risks: null,
      recommendations: ['a', '  ', 'b'],
    });
    expect(report?.sections.map((section) => section.key)).toEqual(['recommendations']);
    expect(report?.sections[0].items).toEqual(['a', 'b']);
  });
});

describe('parseTaskReport · 降级支路与认不出的形状', () => {
  it('降级形状 {executive_summary, content} 两值相同时并成摘要一段，不印两遍', () => {
    const report = parseTaskReport({ executive_summary: '原文一段', content: '原文一段' });
    expect(report?.summary).toBe('原文一段');
    expect(report?.sections).toEqual([]);
  });

  it('两值写岔了就分开展示（不搞无条件键黑名单，少显示比多显示更危险）', () => {
    const report = parseTaskReport({ executive_summary: '结论', content: '完整原文' });
    expect(report?.summary).toBe('结论');
    expect(report?.sections.map((section) => section.key)).toEqual(['content']);
  });

  it('review_warning 收进 warnings，不当普通字段渲成一段', () => {
    const report = parseTaskReport({
      executive_summary: '摘要',
      key_findings: ['发现'],
      review_warning: ['数字口径未标明', '材料中不存在的数字不得出现'],
    });
    expect(report?.warnings).toEqual(['数字口径未标明', '材料中不存在的数字不得出现']);
    expect(report?.sections.map((section) => section.key)).toEqual(['key_findings']);
  });

  it('认不出就 null，不猜：数字、数组、空对象、空串都别冒充"有报告"', () => {
    expect(parseTaskReport(42)).toBeNull();
    expect(parseTaskReport(['a'])).toBeNull();
    expect(parseTaskReport({})).toBeNull();
    expect(parseTaskReport('   ')).toBeNull();
    expect(parseTaskReport(null)).toBeNull();
    expect(parseTaskReport(undefined)).toBeNull();
    // 全空壳（键都在、值都空）同样不算报告；只有警告时警告本身是内容
    expect(parseTaskReport({ executive_summary: '', key_findings: [] })).toBeNull();
    const warningOnly = parseTaskReport({ executive_summary: '', review_warning: ['超限放行'] });
    expect(warningOnly?.summary).toBe('');
    expect(warningOnly?.sections).toEqual([]);
    expect(warningOnly?.warnings).toEqual(['超限放行']);
  });

  it('散文串按裸标题行分节，且行中间的冒号不会被误判成节', () => {
    const report = parseTaskReport(
      [
        '本月销售下滑 12%。',
        '',
        '结论：渠道结构变化是主因，促销节奏错位是次因。',
        '',
        '**核心发现**',
        '- 8 月环比下降 12%',
        '- 华东区贡献七成',
        '',
        '风险提示：',
        '上月窗口数据为空，口径待补。',
      ].join('\n'),
    );
    expect(report?.summary).toBe('本月销售下滑 12%。\n\n结论：渠道结构变化是主因，促销节奏错位是次因。');
    expect(report?.sections.map((section) => section.label)).toEqual(['核心发现', '风险提示']);
    expect(report?.sections[0].prose).toBe('- 8 月环比下降 12%\n- 华东区贡献七成');
    expect(report?.sections[1].prose).toBe('上月窗口数据为空，口径待补。');
  });

  it('以已知标签开头的散文行不算标题（假节会把正文切碎，比不切更糟）', () => {
    const report = parseTaskReport(
      '结论摘要如下：本季渠道结构发生变化，详见下文。\n\n核心发现：这是行中间的冒号，不该切。',
    );
    expect(report?.sections).toEqual([]);
    expect(report?.summary).toBe(
      '结论摘要如下：本季渠道结构发生变化，详见下文。\n\n核心发现：这是行中间的冒号，不该切。',
    );
  });
});

describe('取数入口与渲染门槛', () => {
  it('reportFromRun 取 meta.report；shape 不对 / 没有 run 都给 null', () => {
    expect(reportFromRun(runWithMeta({ report: STRUCTURED }))?.summary).toBe(
      '本月销售下滑主要由渠道结构与促销节奏错位造成。',
    );
    expect(reportFromRun(runWithMeta({ report: { executive_summary: '摘要' } }))?.summary).toBe('摘要');
    expect(reportFromRun(runWithMeta({}))).toBeNull();
    expect(reportFromRun(runWithMeta(null))).toBeNull();
    expect(reportFromRun(null)).toBeNull();
    expect(reportFromRun(undefined)).toBeNull();
  });

  it('reportFromWorkflowRun 取 meta.result.report（sales_analysis 的 final_report 装配位）', () => {
    expect(reportFromWorkflowRun(runWithMeta({ result: { report: STRUCTURED } }))?.sections).toHaveLength(6);
    expect(reportFromWorkflowRun(runWithMeta({ result: null }))).toBeNull();
    expect(reportFromWorkflowRun(runWithMeta({ result: { summary: 'doc_summary 形' } }))).toBeNull();
    expect(reportFromWorkflowRun(runWithMeta({ report: STRUCTURED }))).toBeNull();
  });

  it('sectionHasContent 把空壳节挡在渲染层之外', () => {
    expect(sectionHasContent({ key: 'k', label: 'l', prose: 'x', items: [] })).toBe(true);
    expect(sectionHasContent({ key: 'k', label: 'l', prose: null, items: ['x'] })).toBe(true);
    expect(sectionHasContent({ key: 'k', label: 'l', prose: null, items: [] })).toBe(false);
  });

  it('raw 原样留底：折叠区要显示库里的那一份，不是归一后的重排结果', () => {
    expect(parseTaskReport(STRUCTURED)?.raw).toBe(STRUCTURED);
    expect(parseTaskReport('一段散文')?.raw).toBe('一段散文');
    expect(reportFromRun(runWithMeta({ report: STRUCTURED }))?.raw).toBe(STRUCTURED);
  });
});

/** `splitSources` / `sourceCount`：引用来源独立折叠区的唯一判据（Phase 9b 三条留白之二）。
 *  这两条针要拦的是**静默**失败：键名写错时折叠区从不出现、来源又留在正文里，页面看起来一切正常。 */
describe('splitSources · 引用来源独立折叠区', () => {
  it('结构化七字段：sources 被挑出正文，其余五节顺序不变', () => {
    const report = parseTaskReport(STRUCTURED);
    const { bodySections, sources } = splitSources(report!);
    expect(sources?.items).toEqual(['query_sales 2026Q3']);
    expect(bodySections.map((section) => section.key)).toEqual([
      'key_findings',
      'data_evidence',
      'root_causes',
      'risks',
      'recommendations',
    ]);
  });

  it('没有 sources 字段：sources 为 null，正文原样（不是"来源为空"，是"这条报告没有来源"）', () => {
    const { sources: _omit, ...withoutSources } = STRUCTURED;
    const { bodySections, sources } = splitSources(parseTaskReport(withoutSources)!);
    expect(sources).toBeNull();
    expect(bodySections.map((section) => section.key)).toEqual([
      'key_findings',
      'data_evidence',
      'root_causes',
      'risks',
      'recommendations',
    ]);
  });

  it('散文形状里以「来源」独占一行为标题的段落同样折叠（归一已知标签 → key=sources）', () => {
    const report = parseTaskReport(['正文一段。', '', '来源：', 'query_sales 2026Q3'].join('\n'));
    const { bodySections, sources } = splitSources(report!);
    expect(sources?.key).toBe('sources');
    expect(sources?.prose).toBe('query_sales 2026Q3');
    expect(bodySections).toEqual([]);
  });

  it('认 key 不认 label：标签叫「来源」但键不是 sources 的节留在正文里', () => {
    // 这条针专门拦"按中文文案匹配"的实现：文案会改，键名与产品模型同源。
    const report = parseTaskReport({ executive_summary: '摘要', references: ['a.csv'] })!;
    const relabelled = {
      ...report,
      sections: report.sections.map((section) => ({ ...section, label: REPORT_FIELD_LABELS.sources })),
    };
    expect(relabelled.sections[0].key).toBe('references');
    expect(relabelled.sections[0].label).toBe('来源');
    const { bodySections, sources } = splitSources(relabelled);
    expect(sources).toBeNull();
    expect(bodySections.map((section) => section.key)).toEqual(['references']);
  });

  it('空壳 sources（键在、值空）不折叠：否则页面上多一个点开是空的折叠区', () => {
    const report = parseTaskReport(STRUCTURED)!;
    const emptied = {
      ...report,
      sections: report.sections.map((section) =>
        section.key === 'sources' ? { ...section, prose: null, items: [] } : section,
      ),
    };
    const { bodySections, sources } = splitSources(emptied);
    expect(sources).toBeNull();
    expect(bodySections.map((section) => section.key)).toContain('recommendations');
    expect(bodySections.map((section) => section.key)).not.toContain('sources');
  });

  it('sourceCount：列表按条算，散文按一段算（拿不出 0，标题写「0 条」是谎）', () => {
    const report = parseTaskReport(STRUCTURED)!;
    const { sources } = splitSources(report);
    expect(sourceCount(sources!)).toBe(1);
    expect(
      sourceCount({ key: 'sources', label: '来源', prose: '一段', items: [] }),
    ).toBe(1);
    expect(
      sourceCount({ key: 'sources', label: '来源', prose: null, items: ['a', 'b', 'c'] }),
    ).toBe(3);
  });
});
