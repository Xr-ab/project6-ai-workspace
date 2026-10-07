/** lib/format.ts 的两处展示格式化（spec §5.2）。
 *
 *  为什么值得测：这两个函数被五个页面共用，而它们的边界都是**已经裁定过、不许后人
 *  「顺手优化」掉**的口径（R51 的 60.0 s、null 不等于 0）。测试在这里的角色是把裁定
 *  钉住，不是发现 bug。
 *
 *  期望值全部由 `node -e` 对当前实现手算过（2026-10-01）：59950 → '60.0 s'、
 *  90500 → '1 分 31 秒'（分钟档的秒位是 Math.round 不是 floor），照抄这两个数就够。
 */
import { describe, expect, it } from 'vitest';

import { TRACE_COST_CURRENCY, formatCost, formatDuration } from '../format';

describe('formatDuration', () => {
  it('null 显示破折号而不是 0：不可得的数不许用 0 顶', () => {
    expect(formatDuration(null)).toBe('—');
  });

  it('<1s 留整数 ms', () => {
    expect(formatDuration(0)).toBe('0 ms');
    expect(formatDuration(999)).toBe('999 ms');
  });

  it('恰好 1000ms 落到秒档，且是一位小数', () => {
    expect(formatDuration(1000)).toBe('1.0 s');
    expect(formatDuration(1500)).toBe('1.5 s');
  });

  it('R51 边界：59_950~59_999 渲成「60.0 s」而不是分钟档（裁定**不改**，这条针就是它的全部价值）', () => {
    // 走分钟档会得到「0 分 60 秒」，比 60.0 s 更难读 —— 见 lib/format.ts:9-12 的原注释。
    expect(formatDuration(59949)).toBe('59.9 s');
    expect(formatDuration(59950)).toBe('60.0 s');
    expect(formatDuration(59999)).toBe('60.0 s');
  });

  it('≥60s 走分/秒，且秒位是四舍五入', () => {
    expect(formatDuration(60000)).toBe('1 分 0 秒');
    expect(formatDuration(150000)).toBe('2 分 30 秒');
    expect(formatDuration(90500)).toBe('1 分 31 秒');   // 30.5 秒 → 31，不是 30
    expect(formatDuration(120000)).toBe('2 分 0 秒');
  });
});

describe('formatCost', () => {
  it('null / undefined 都是「未配置单价」而不是 ¥0.0000', () => {
    expect(formatCost(null, 'CNY')).toBe('未配置单价');
    expect(formatCost(undefined, 'CNY')).toBe('未配置单价');
  });

  it('0 是诚实的数字：配好单价、这条 run 没花 token ⇒ ¥0.0000', () => {
    expect(formatCost(0, 'CNY')).toBe('¥0.0000');
  });

  it('币种符号与四位小数（toFixed(4) 的四舍五入也钉住）', () => {
    expect(formatCost(1.23456, 'USD')).toBe('$1.2346');
    expect(formatCost(0.5, 'USD')).toBe('$0.5000');
  });

  it('未知币种退到「代码 + 空格」而不是猜一个符号', () => {
    expect(formatCost(1, 'EUR')).toBe('EUR 1.0000');
  });

  it('trace 详情用的具名常量仍是 CNY（换展示币种时要一起改的就是它）', () => {
    expect(TRACE_COST_CURRENCY).toBe('CNY');
  });
});
