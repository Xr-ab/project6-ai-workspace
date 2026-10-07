/** lib/pagination.ts 的针（Phase 9b 三条留白之三：分页器只有「上一页/下一页」→ 五件套）。
 *
 *  为什么值得单独钉：本仓的 vitest 边界是 `environment: 'node'`、零 DOM
 *  （vitest.config.ts:6），组件里的按钮态与输入框行为**没法**在这里断言。
 *  所以把翻页的算式抽成纯函数，页面只负责把 state 喂进来 —— 钉得住的是
 *  「第 999 页跳哪去」「输入 abc 跳哪去」「total=0 时共几页」这三类会静默骗人的分支。
 *
 *  期望值口径：逐字照 ReportsPage 的真实用法取（PAGE_SIZE=20、`offset = (page-1)*20`）。
 */
import { describe, expect, it } from 'vitest';

import { clampPage, offsetForPage, pageCount, parseJumpPage } from '../pagination';

/** 与 ReportsPage.tsx 的 PAGE_SIZE 一致；这里自己写一份，是为了让「改页大小」时红的是本页而不是巧合。 */
const PAGE_SIZE = 20;

describe('pageCount · 共几页', () => {
  it('total=0 也是 1 页：0 页是个不存在的页码，会把按钮判成永久禁用', () => {
    expect(pageCount(0, PAGE_SIZE)).toBe(1);
  });

  it('整除不多算一页，余 1 条就多一页', () => {
    expect(pageCount(1, PAGE_SIZE)).toBe(1);
    expect(pageCount(20, PAGE_SIZE)).toBe(1);
    expect(pageCount(21, PAGE_SIZE)).toBe(2);
    expect(pageCount(40, PAGE_SIZE)).toBe(2);
    expect(pageCount(41, PAGE_SIZE)).toBe(3);
  });

  it('pageSize 非法（0 / 负数）时给 1 页而不是 Infinity / NaN', () => {
    expect(pageCount(10, 0)).toBe(1);
    expect(pageCount(10, -1)).toBe(1);
  });
});

describe('clampPage · 页码收进合法区间', () => {
  it('区间内原样，越界夹到边界（首页/末页/上一页/下一页四条按钮都经这里）', () => {
    expect(clampPage(3, 5)).toBe(3);
    expect(clampPage(1, 5)).toBe(1);
    expect(clampPage(5, 5)).toBe(5);
    expect(clampPage(0, 5)).toBe(1);
    expect(clampPage(-8, 5)).toBe(1);
    expect(clampPage(999, 5)).toBe(5);
  });

  it('小数向下取整；pages=0 也至少有第 1 页', () => {
    expect(clampPage(2.7, 5)).toBe(2);
    expect(clampPage(1, 0)).toBe(1);
    expect(clampPage(4, 0)).toBe(1);
  });
});

describe('offsetForPage · 页码 → 请求 offset', () => {
  it('第 1 页是 0，第 n 页是 (n-1)*pageSize', () => {
    expect(offsetForPage(1, PAGE_SIZE)).toBe(0);
    expect(offsetForPage(2, PAGE_SIZE)).toBe(20);
    expect(offsetForPage(3, PAGE_SIZE)).toBe(40);
  });

  it('非法页码不会算出负 offset（后端 limit/offset 的 ge=1 教训同款）', () => {
    expect(offsetForPage(0, PAGE_SIZE)).toBe(0);
    expect(offsetForPage(-3, PAGE_SIZE)).toBe(0);
  });
});

describe('parseJumpPage · 页码直达输入框', () => {
  it('1..pages 的整数才响应', () => {
    expect(parseJumpPage('3', 5)).toBe(3);
    expect(parseJumpPage(' 3 ', 5)).toBe(3);
    expect(parseJumpPage('1', 1)).toBe(1);
  });

  it('认不出就 null，不猜也不夹带：超末页 / 0 / 负数 / 小数 / 非数字 / 空串一律不响应', () => {
    // 刻意不夹到末页：夹带会让"输入的第 999 页"和"实际跳到的第 5 页"不是同一页，
    // 而有末页按钮在，跳不到不存在的那一页是事实而不是故障。
    expect(parseJumpPage('999', 5)).toBeNull();
    expect(parseJumpPage('0', 5)).toBeNull();
    expect(parseJumpPage('-3', 5)).toBeNull();
    expect(parseJumpPage('2.5', 5)).toBeNull();
    expect(parseJumpPage('abc', 5)).toBeNull();
    expect(parseJumpPage('', 5)).toBeNull();
    expect(parseJumpPage('   ', 5)).toBeNull();
    expect(parseJumpPage('3abc', 5)).toBeNull();
  });
});
