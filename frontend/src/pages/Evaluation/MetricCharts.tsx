/**
 * 自绘 SVG 图表（D6：不引图表库，纯 SVG + CSS Modules 变量取色）。
 * 内联 SVG 的先例见 components/NavIcon.tsx。
 *
 * 为什么手写而不装 ECharts/Recharts：这里只有"折线 + 环"两种形状，
 * 引一个图表库换来的抽象远多于它省下的代码，还带进主题打通、按需加载
 * 一串新问题。两个组件都控制在几十行内，出问题直接读 SVG 规范就能懂。
 */
import styles from './EvaluationPage.module.css';

/** 趋势图的一个点：一次 Run 的某个指标值 */
export interface TrendPoint {
  /** run id，当 key 用 */
  id: string;
  /** x 轴标签（Run 时间） */
  label: string;
  /** 指标值。null = 这个 Run 没有该指标的有效值（未覆盖/未配置），不画点 */
  value: number | null;
  /** 版本锚（target_version），悬浮 <title> 里显示 */
  version: string;
}

/** 画布尺寸（viewBox 逻辑坐标，实际随容器缩放） */
const WIDTH = 340;
const HEIGHT = 150;
const PAD_LEFT = 46;
const PAD_RIGHT = 14;
const PAD_TOP = 14;
const PAD_BOTTOM = 26;

function defaultFormat(value: number): string {
  return String(value);
}

/**
 * 折线趋势图：x = Run 时间序，y = 指标值（0-1 或 ms）。
 *
 * 三条都影响"读图会不会被骗"的要求：
 *   1. 空数据出"还没有可比的 Run"，而不是画一根 0 线 —— 0 线会被读成"成绩是零"，
 *      真相是"还没测过"，两者差一个数量级的误导性。
 *   2. y 轴上下各留 8% 边距：不留边距时 0.6→0.62 的进步贴着顶线画，
 *      看起来是平的，用户会以为没有变化。
 *   3. 点上有 <title> 悬浮，显示"时间 · 版本锚 · 值"，趋势背后是谁做的可查。
 */
export function TrendLine({
  points,
  metric,
  formatValue = defaultFormat,
}: {
  points: TrendPoint[];
  metric: string;
  formatValue?: (value: number) => string;
}) {
  const valid = points.filter((p) => p.value !== null && Number.isFinite(p.value));
  if (valid.length === 0) {
    return (
      <div className={styles.chartEmpty} data-chart={metric}>
        还没有可比的 Run —— 趋势需要至少一个已完成、且该指标有值的 Run
      </div>
    );
  }

  const values = valid.map((p) => p.value as number);
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) {
    // 所有点同值时上下界重合，按值的幅度撑开，单点也能画在中间
    const pad = Math.abs(max) * 0.1 || 1;
    min -= pad;
    max += pad;
  } else {
    // 8% 边距：见组件头注释第 2 条
    const pad = (max - min) * 0.08;
    min -= pad;
    max += pad;
  }

  const plotW = WIDTH - PAD_LEFT - PAD_RIGHT;
  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM;
  // x 按"全部点（含 null 点）"的序号均分，null 点只是不画，不让后面的点前移
  // 错位 —— 时间轴间距保持均匀，读图时横向位置可比。
  const step = points.length > 1 ? plotW / (points.length - 1) : 0;
  const xAt = (index: number) =>
    points.length > 1 ? PAD_LEFT + index * step : PAD_LEFT + plotW / 2;
  const yAt = (value: number) => PAD_TOP + (1 - (value - min) / (max - min)) * plotH;

  const linePoints = points
    .map((p, i) => ({ p, i }))
    .filter(({ p }) => p.value !== null && Number.isFinite(p.value))
    .map(({ p, i }) => `${xAt(i).toFixed(1)},${yAt(p.value as number).toFixed(1)}`)
    .join(' ');

  // 三条横线（max / 中点 / min）当刻度，其余按业务值域没有意义就不硬造
  const gridValues = [max, (min + max) / 2, min];

  return (
    <svg
      className={styles.chartSvg}
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      role="img"
      aria-label={`${metric} 趋势图，共 ${valid.length} 个数据点`}
    >
      {gridValues.map((gv, gi) => (
        <g key={gi}>
          <line
            x1={PAD_LEFT}
            x2={WIDTH - PAD_RIGHT}
            y1={yAt(gv)}
            y2={yAt(gv)}
            className={styles.chartGrid}
          />
          <text x={PAD_LEFT - 6} y={yAt(gv) + 3} className={styles.chartTick} textAnchor="end">
            {formatValue(gv)}
          </text>
        </g>
      ))}
      {linePoints && <polyline points={linePoints} className={styles.chartLine} />}
      {points.map((p, i) =>
        p.value === null || !Number.isFinite(p.value) ? null : (
          <circle
            key={p.id}
            cx={xAt(i)}
            cy={yAt(p.value)}
            r={3.5}
            className={styles.chartDot}
          >
            <title>{`${p.label} · ${p.version} · ${formatValue(p.value)}`}</title>
          </circle>
        ),
      )}
      {/* x 轴只标首尾两个时间，中间点靠悬浮 */}
      <text x={xAt(0)} y={HEIGHT - 8} className={styles.chartTick} textAnchor="start">
        {points[0]?.label ?? ''}
      </text>
      <text
        x={xAt(points.length - 1)}
        y={HEIGHT - 8}
        className={styles.chartTick}
        textAnchor="end"
      >
        {points[points.length - 1]?.label ?? ''}
      </text>
    </svg>
  );
}

/**
 * 环形占比图：failure_category_dist（失败类别 → 条数）。
 *
 * 弧线用 stroke-dasharray 画：一段圆弧 = 圆周长的 frac 比例的虚线段，
 * 比手算 path 的 A 命令弧参数省事且不易错（不用推起点/终点/大弧标志）。
 * 空 dist 显示"本批无链路失败分类"—— 不画一个灰圈假装"100% 某类"。
 * W1 前端同步：这句文案改前的字面是"本批无失败用例"，而这个键量的是
 * "未通过用例的类别分布"（后端 evaluation_metrics.compute_metrics），
 * judge 判不过的用例在那里叫 judge_failed 桶 —— 文案要说它真正量的东西，
 * 并指一句"判分不过的用例见下方明细"，别让人从空图推出"这批全过了"。
 * loaded 是 metrics 到没到的门（I1 修复轮）：metrics 为 null（首帧、
 * Run 正在跑的那 15 分钟、failed run 永不会有指标）时这句是谎 ——
 * 真相是"还没测"，不是"测出来无失败"。与把 cost:null 渲成 ¥0 同族。
 */
const SEGMENT_COLORS = [
  'var(--color-danger)',
  'var(--color-warning)',
  'var(--color-primary)',
  'var(--color-success)',
  'var(--color-text-muted)',
  'var(--color-border-strong)',
];

export function FailureDonut({
  dist,
  loaded,
}: {
  dist: Record<string, number>;
  /** metrics 是否已到（metrics !== null）。false 时渲"指标尚未就绪"，绝不渲"本批无失败用例" */
  loaded: boolean;
}) {
  if (!loaded) {
    return (
      <div className={styles.chartEmpty} data-chart="failure_category_dist">
        指标尚未就绪 —— 失败分布要等选中 Run 完成，这不是"无失败"
      </div>
    );
  }
  const entries = Object.entries(dist ?? {})
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((sum, [, count]) => sum + count, 0);

  if (entries.length === 0) {
    return (
      <div className={styles.chartEmpty}>
        本批无链路失败分类 —— 判分不过的用例见下方明细
      </div>
    );
  }

  const size = 150;
  const radius = 54;
  const circumference = 2 * Math.PI * radius;
  let used = 0;

  return (
    <div className={styles.donutWrap}>
      <svg
        viewBox={`0 0 ${size} ${size}`}
        width={size}
        height={size}
        role="img"
        aria-label={`失败类别占比图，共 ${total} 条`}
      >
        {/* 整组逆旋 90°：让第一段从 12 点方向开始，符合"从顶部顺时针"的直觉 */}
        <g transform={`rotate(-90 ${size / 2} ${size / 2})`}>
          {entries.map(([category, count], i) => {
            const frac = count / total;
            const dash = frac * circumference;
            const offset = -used * circumference;
            used += frac;
            return (
              <circle
                key={category}
                cx={size / 2}
                cy={size / 2}
                r={radius}
                fill="none"
                stroke={SEGMENT_COLORS[i % SEGMENT_COLORS.length]}
                strokeWidth={18}
                strokeDasharray={`${dash.toFixed(2)} ${(circumference - dash).toFixed(2)}`}
                strokeDashoffset={offset.toFixed(2)}
              >
                <title>{`${category}：${count} 条（${(frac * 100).toFixed(1)}%）`}</title>
              </circle>
            );
          })}
        </g>
        <text x={size / 2} y={size / 2 + 5} textAnchor="middle" className={styles.donutTotal}>
          {total}
        </text>
      </svg>
      <ul className={styles.legend}>
        {entries.map(([category, count], i) => (
          <li key={category} className={styles.legendItem}>
            <span
              className={styles.legendChip}
              style={{ background: SEGMENT_COLORS[i % SEGMENT_COLORS.length] }}
            />
            <span>{category}</span>
            <span className={styles.legendCount}>
              {count} 条 · {((count / total) * 100).toFixed(1)}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
