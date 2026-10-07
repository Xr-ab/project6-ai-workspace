/** 测试配置与构建配置**分成两份文件**，理由不是整洁而是约束不同：
 *  vite.config.ts 里锁着 strictPort 5173（后端 CORS 只放行这个端口，端口一漂全部请求被拦，
 *  见 vite.config.ts:7-11 的原注释），而测试根本不起 dev server。合并成一份
 *  等于让"测试跑不起来"和"CORS 放行面"两件事共用一个旋钮。
 *
 *  environment: 'node'（spec §5.2 的边界）：只测 lib/ 下的纯函数，不引 jsdom、
 *  不做组件渲染。这条边界由 docs/02 §11 成文，配置文件是它的物证。
 */
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    // 刻意不开 globals：用例里显式 import { describe, it, expect } from 'vitest'。
    // 开 globals 要往 tsconfig 塞 "types": ["vitest/globals"]，那是把全局命名空间放宽，
    // tsc 就会认下本来不该存在的裸名字——为了少写一行 import 不值。
  },
});
