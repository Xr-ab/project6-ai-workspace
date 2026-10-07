/**
 * 时间窗档位表（今日 / 近 7 天 / 近 30 天 / 全部）—— 全仓唯一一份。
 *
 * 为什么现在抽出来：这一字不差的数组在 `DashboardPage` 与 `SettingsPage` 各躺了一份
 * （本次合并前 `git show HEAD:` 实测：两处都在各自文件的 `:23`），
 * 报告的「时间范围过滤」是第三个消费者。
 * 抄第三份的代价不是文案不一致那么轻 —— 真正会出事的是**某一档的 key 打错**：
 * key 是发给后端的机器值，打错的那一档会被 FastAPI 判 422（合法值由 `RangeKey` 锁死），
 * 页面表现为"点这一档就报错"；更糟的是后端与前端各有一份词表时，加一档只改一边
 * 会让另一边静默回到默认窗，出的数不是点的那一档。
 * 所以键集与后端 `RangeKey` 的一致性由
 * `backend/tests/unit/test_report_layer.py::test_frontend_range_tabs_are_exactly_the_backend_range_keys`
 * 直接读本文件的 `RANGE_TABS` 块钉住（与 `taskReport.ts` 那套前后端对账同一办法）。
 *
 * 窗口算法**不在这里**：滚动 7/30 天、today=本地日 00:00 的口径住在后端
 * `stats_repo.range_start`，本表只是那四个词的中文皮。
 */
import type { StatsRange } from '../api/stats';

export interface RangeTab {
  key: StatsRange;
  label: string;
}

/** 数组顺序 = 页面上 tab 的左起顺序。默认档由各页自定（stats 是 today，报告台账是 all）。 */
export const RANGE_TABS: RangeTab[] = [
  { key: 'today', label: '今日' },
  { key: 'week', label: '近 7 天' },
  { key: 'month', label: '近 30 天' },
  { key: 'all', label: '全部' },
];
