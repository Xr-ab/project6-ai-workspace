/**
 * 认证上下文（Phase 8a T7）：全站唯一的"当前用户"真相源。
 *
 * 为什么用 Context 而不是 zustand（chat store 那种）：登录态要跨整个组件树
 * 且和生命周期绑定（挂载探测会话、卸载注销 handler），Provider 树是更自然的宿主；
 * 页面局部数据留在 zustand 不动。
 *
 * 导航策略：本层**不**直接跳路由。会话失效 / 登出只清 user，
 * 由 RequireAuth（路由守卫，在 Router 树内）看到 user 空后统一 <Navigate to="/login">。
 * 好处是 AuthProvider 可以挂在 RouterProvider 之外（见 main.tsx），
 * 避免"Context 反向 import router 对象"的循环依赖。
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import { getStoredTokens, setStoredTokens, clearStoredTokens, setSessionExpiredHandler } from '../api/client';
import * as authApi from '../api/auth';
import type { AuthUser } from '../api/auth';

export type { AuthUser };

export interface RegisterInput {
  email: string;
  password: string;
  full_name: string;
  organization_name: string;
}

interface AuthContextValue {
  /** null = 未登录（且已确认：initializing 结束后仍无 user） */
  user: AuthUser | null;
  /** 挂载期用存储的 token 换 /auth/me 的过程中，守卫据此渲占位而不是弹登录页 */
  initializing: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (input: RegisterInput) => Promise<void>;
  logout: () => Promise<void>;
  /** 服务端权威响应写回内存 state（9a Settings 的 profile 保存用）。
   *  不另拉 /auth/me：PATCH /auth/me 的返回体就是 UserOut 的权威形状，多一跳只是慢。
   *  不碰 token：这只改"我是谁"的展示与判定，不改"我有没有登录"。 */
  applyUser: (user: AuthUser) => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [initializing, setInitializing] = useState(true);

  // 挂载探测：本地有 access token 就试拉 /auth/me。
  // 401 交给 client 层静默刷新——刷新成功 me() 正常返回，刷新失败会触发
  // 下面的 sessionExpired handler（清 user），两者都会让 initializing 落定。
  useEffect(() => {
    let cancelled = false;
    const { access } = getStoredTokens();
    if (!access) {
      setInitializing(false);
      return;
    }
    void (async () => {
      try {
        const me = await authApi.me();
        if (!cancelled) setUser(me);
      } catch {
        // 网络错 / 刷新失败：不当作已登录。刷新失败时 client 层已清 token
        // 并回调 onSessionExpired，这里只需收束 loading
        if (!cancelled) setUser(null);
      } finally {
        if (!cancelled) setInitializing(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // 注册全局会话失效回调：静默刷新换不来新 token 时（refresh 也过期/被吊销），
  // client 层清完 token 后调这里，把内存里的 user 也清空 → RequireAuth 跳登录页。
  useEffect(() => {
    setSessionExpiredHandler(() => setUser(null));
    return () => setSessionExpiredHandler(() => {});
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const result = await authApi.login(email, password);
    setStoredTokens(result.access_token, result.refresh_token);
    setUser(result.user);
  }, []);

  const register = useCallback(async (input: RegisterInput) => {
    const result = await authApi.register(input);
    setStoredTokens(result.access_token, result.refresh_token);
    setUser(result.user);
  }, []);

  const logout = useCallback(async () => {
    // 先拿 refresh 再清：登出要告诉后端吊销白名单里的这一条（06 §1.4）
    const { refresh } = getStoredTokens();
    try {
      if (refresh) await authApi.logout(refresh);
    } catch {
      // 吊销失败不拦本地登出：token 反正要清，服务端 refresh 有 TTL 兜底
    } finally {
      clearStoredTokens();
      setUser(null); // user 归零 → RequireAuth 跳 /login
    }
  }, []);

  const applyUser = useCallback((next: AuthUser) => setUser(next), []);

  const value = useMemo<AuthContextValue>(
    () => ({ user, initializing, login, register, logout, applyUser }),
    [user, initializing, login, register, logout, applyUser],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** 消费口。放在 Provider 外调用是装配错误，直接抛而不是静默给 null */
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth 必须在 <AuthProvider> 内调用');
  return ctx;
}
