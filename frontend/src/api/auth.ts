/**
 * Auth 域 API 薄封装（Phase 8a T7，形状对齐 06 §2.1 与后端 schemas/auth.py）。
 *
 * refresh 不在这里露出：静默刷新完全封在 client.ts 的 withAuthRetry 里
 * （401 → 刷新 → 重放对业务层透明），页面没有主动调它的场景。
 * login/register 的响应带 token 对，这里原样返回给 AuthContext 落库，
 * 本模块不碰 localStorage（令牌存储是 client 层的唯一读写口）。
 */
import { request } from './client';

/** 后端 UserOut（06 §2.1 /auth/me 响应形状的唯一定义源） */
export interface AuthUser {
  id: string;
  email: string;
  full_name: string | null;
  /** "admin" | "member"（06 §6.3 工具权限闸按此判） */
  role: string;
  organization_id: string;
  created_at: string;
  /** 组织名。`GET /auth/me` 与 `PATCH /auth/me` **两口都**回填（一次 join，R27 让两口同形状），
   *  register/login 的响应里它是 null —— 收成可选，AuthContext 登录态下不假设它有值（R6）。
   *  （R36 勘误：原计划这里写「只有 GET 会回填」，是 Task 4 评审判为谎话的同一句 M-1，
   *  磁盘上的 `backend/app/schemas/auth.py` 已按 GET/PATCH 两口改正，这里跟着对齐。） */
  organization_name?: string | null;
}

/** 后端 AuthOut：token 对 + 用户信息 */
export interface AuthResult {
  access_token: string;
  refresh_token: string;
  user: AuthUser;
}

/** 注册（同时建组织，第一个用户即 admin —— auth_service 口径） */
export function register(input: {
  email: string;
  password: string;
  full_name: string;
  organization_name: string;
}): Promise<AuthResult> {
  return request<AuthResult>('/auth/register', { method: 'POST', body: input });
}

/** 登录 */
export function login(email: string, password: string): Promise<AuthResult> {
  return request<AuthResult>('/auth/login', { method: 'POST', body: { email, password } });
}

/** 登出（吊销 refresh）。需带 access token —— 路由层要认登出者身份（06 §1.4 实现注） */
export function logout(refreshToken: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>('/auth/logout', { method: 'POST', body: { refresh_token: refreshToken } });
}

/** 当前用户信息（AuthContext 挂载时的会话恢复探测；401 已由 client 层静默处理） */
export function me(): Promise<AuthUser> {
  return request<AuthUser>('/auth/me');
}

/** PATCH /auth/me 只带 full_name：后端字段白名单就这一列（spec §3.3），
 *  传 role 会被 extra=forbid 挡在 422，所以这里连参数都不给。 */
export function updateProfile(fullName: string): Promise<AuthUser> {
  return request<AuthUser>('/auth/me', { method: 'PATCH', body: { full_name: fullName } });
}

/** 改口令。204 无响应体（client 层对 204 返回 undefined）。
 *  旧口令错 → 403 + AUTH_403003：**刻意不用 401**，因为 client 的 withAuthRetry
 *  会把 401 当"身份过期"去静默刷新、失败即清 token 跳登录 —— 填错旧口令不该被踢下线（R4）。 */
export function changePassword(oldPassword: string, newPassword: string): Promise<void> {
  return request<void>('/auth/change-password', {
    method: 'POST',
    body: { old_password: oldPassword, new_password: newPassword },
  });
}

/** 一条审计记录（AuditLogItemOut）。detail 形状随 action 而变，按透传壳收 */
export interface AuditLogItem {
  id: string;
  organization_id: string | null;
  user_id: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  detail: Record<string, unknown> | null;
  request_id: string | null;
  created_at: string;
}

/** 审计分页信封（06 §7.1） */
export interface AuditLogPage {
  items: AuditLogItem[];
  total: number;
  page: number;
  page_size: number;
}

/** 组织审计自查（**admin 闸**：member 调用会 403，Settings 页按角色条件渲染，根本不发这个请求） */
export function listAuditLogs(page = 1, pageSize = 20): Promise<AuditLogPage> {
  return request<AuditLogPage>('/auth/audit-log', { params: { page, page_size: pageSize } });
}
