/**
 * 轮询的计数核（Phase 9a，spec §4.4）：节奏与止损口径的唯一出处。
 *
 * 为什么单独一个文件而不是写进 hook：这里零 React import，
 * 于是能用 node + 真定时器直接断言"连续 3 次失败升级、成功复位"（scratch/p9a_poll_harness.mjs）。
 * 塞进 hook 就得请出 DOM/测试框架，而本仓前端没有测试框架（package.json 无 vitest）。
 *
 * 口径来自 `pages/Knowledge/KnowledgePage.tsx` 里那两个常量（2500ms / 三次止损）的既有先例，
 * Dashboard/AgentTasks/TaskDetail 三页共用这一份，不各抄一遍。
 */

export const POLL_INTERVAL_MS = 2500;
export const MAX_POLL_FAILURES = 3;

export interface PollOptions<T> {
  fetcher: () => Promise<T>;
  /** 落终态就停表（例如「列表里没有未完成任务」） */
  isSettled: (data: T) => boolean;
  onData: (data: T) => void;
  /** 连续 maxFailures 次失败后升级一次，然后停表。
   *  R78：参数是**原始 error 对象**，不是文案。核只做节奏与止损，不碰展示——
   *  原先核里写 `error instanceof Error ? error.message : String(error)`，等于在计数核里
   *  造了一套文案口径，绕开 `toReadableError`：`ApiError` 之外的抛出物会显成原文
   *  （非 Error 时是 `[object Object]`），而 `message` 为空串的 Error 会让页面
   *  `setError('')` —— 空串是 falsy，错误卡不渲、`active`（含 `error === null`）却已经
   *  因为表停而不成立，于是**静默死掉且不再刷新**。文案口径归各页，与本页其他错误同函数。 */
  onFatal: (error: unknown) => void;
  intervalMs?: number;
  maxFailures?: number;
}

/** 开一张表，返回停表函数（幂等：重复调用不会清掉别人的表）。
 *  **首次数据在 +intervalMs 之后**：setInterval 不立刻 tick，这与既有先例一致
 *  （`pages/Knowledge/KnowledgePage.tsx` 的挂载 effect 里自己 fetch 一次，`setInterval` 只负责后续刷新）。
 *  所以消费页必须自己在挂载时拉第一帧——指望这个核给首屏数据会白等 2.5s。 */
export function startPolling<T>(options: PollOptions<T>): () => void {
  const intervalMs = options.intervalMs ?? POLL_INTERVAL_MS;
  const maxFailures = options.maxFailures ?? MAX_POLL_FAILURES;
  let failures = 0;
  let timer: ReturnType<typeof setInterval> | null = null;
  /** 上一拍是否还挂着（R49）。setInterval 不等 promise：响应慢于 intervalMs 时它会照点开下一拍，
   *  于是并发两拍——旧响应可能覆盖新响应（onData 乱序）、失败计数被两拍各加一次而提前打满、
   *  stop() 之后挂着的那拍还会再回调一次 onData/onFatal。三个消费页共用这个核，
   *  守卫放这里比在各页各写一份「上一次还没回来先跳过」可靠。 */
  let inFlight = false;
  /** 表是否已经停过（R52）。`stop()` 只清得掉「还没开的拍」，清不掉「正在 await 的那拍」：
   *  那一拍回来时若照常回调，换 Tab / 换 restartKey 之后**旧任务的响应会写进新页面的 state**，
   *  止损横幅也会拿着上一个周期的错误弹出来。三个消费页都用 `restartKey` 换表，所以这条
   *  在本核里断掉，不给各页各写一份「先判还该不该写」的机会。 */
  let stopped = false;

  const stop = () => {
    stopped = true;
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };

  const tick = async () => {
    if (inFlight || stopped) return;
    inFlight = true;
    try {
      const data = await options.fetcher();
      if (stopped) return; // 期间停表 → 这一拍的数据作废，不写回
      failures = 0; // 成功即复位：轮询是长活，偶发一次抖动不该攒成"三次失败"
      options.onData(data);
      if (options.isSettled(data)) stop();
    } catch (error) {
      if (stopped) return; // 同理：迟到的失败不许再往计数器上加，也不许弹已过期的止损
      failures += 1;
      if (failures >= maxFailures) {
        options.onFatal(error); // R78：核只负责"要不要升级"，文案归各页的 toReadableError
        stop();
      }
    } finally {
      inFlight = false;
    }
  };

  timer = setInterval(() => void tick(), intervalMs);
  return stop;
}
