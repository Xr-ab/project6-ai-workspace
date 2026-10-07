/**
 * pollCycle 的 React 包装（Phase 9a，spec §4.4）：
 * active 为真才开表；restartKey 变了就换表；卸载即停。
 *
 * 为什么回调存 ref：定时器只建一次（依赖里不能塞 fetcher/onData，
 * 否则每次重渲染都换一张表，2.5s 的节奏会变成"跟着渲染跑"）。
 */
import { useEffect, useRef } from 'react';

import { startPolling } from './pollCycle';

export interface UsePollArgs<T> {
  /** 有没有需要盯的东西（false 时完全不开表） */
  active: boolean;
  fetcher: () => Promise<T>;
  isSettled: (data: T) => boolean;
  onData: (data: T) => void;
  onFatal: (error: unknown) => void; // R78：原始 error，文案归消费页（与本页其他错误同走 toReadableError）
  /** 换了观察对象（例如切了 range 或进了另一条任务）就重开表 */
  restartKey?: string;
}

export function usePollUntilSettled<T>(args: UsePollArgs<T>): void {
  const handlers = useRef(args);
  handlers.current = args;

  useEffect(() => {
    if (!handlers.current.active) return;
    const stop = startPolling<T>({
      fetcher: () => handlers.current.fetcher(),
      isSettled: (data) => handlers.current.isSettled(data),
      onData: (data) => handlers.current.onData(data),
      onFatal: (error) => handlers.current.onFatal(error),
    });
    return stop;
  }, [args.active, args.restartKey]);
}
