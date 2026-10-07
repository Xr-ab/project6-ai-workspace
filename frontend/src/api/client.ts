/**
 * 统一 API 客户端。
 *
 * 为什么要有这一层：如果页面里直接 fetch，会有三件事散落在各处 ——
 * base URL 拼接、错误响应体的结构解析、非 2xx 时抛错。集中在这里之后，
 * 页面只关心业务语义（"列会话""发消息"），错误文案也只在这里维护一份。
 */

/** 后端 API 前缀。优先读 .env，没配就回落到本地后端（新人 clone 下来直接能跑） */
const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8002/api/v1';

/* ---------------- Token 存储（Phase 8a T7） ----------------
 * 会话令牌只存 localStorage：本仓无 HttpOnly cookie 后端配合（06 §1.4 走
 * Bearer header），XSS 面靠"业务数据本就要求登录"这一现实边界接受。
 * 键名固定 p6_access_token / p6_refresh_token，全站唯一读写口在这层。 */
const ACCESS_KEY = 'p6_access_token';
const REFRESH_KEY = 'p6_refresh_token';

export function getStoredTokens(): { access: string | null; refresh: string | null } {
  return { access: localStorage.getItem(ACCESS_KEY), refresh: localStorage.getItem(REFRESH_KEY) };
}
export function setStoredTokens(access: string, refresh: string): void {
  localStorage.setItem(ACCESS_KEY, access);
  localStorage.setItem(REFRESH_KEY, refresh);
}
export function clearStoredTokens(): void {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
}

/** 合入 Authorization 的 header 构造器。base 缺省时给空对象而非 undefined：
 *  FormData 场景依旧不写 Content-Type，让浏览器带 boundary（原规则保持）。 */
function authHeaders(base?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { ...(base ?? {}) };
  const t = localStorage.getItem(ACCESS_KEY);
  if (t) h.Authorization = `Bearer ${t}`;
  return h;
}

/** 会话彻底失效（refresh 也换不来新 token）时的全局回调。
 *  AuthContext 挂载时注册（清 user + 由路由守卫跳 /login），client 层不认识路由。 */
let onSessionExpired: () => void = () => {};
export function setSessionExpiredHandler(fn: () => void): void {
  onSessionExpired = fn;
}

/** 并发 401 只放一次真 refresh 出去（单飞）：轮转语义下旧 refresh 用一次即废，
 *  第二个并发请求若再拿同一个旧 refresh 去换会被判 401，误清整个会话。 */
let inFlightRefresh: Promise<boolean> | null = null;

/** 公开（无需登录）的 auth 端点：递归守卫只需豁免这三个 ——
 *  refresh 死循环的成因是"/auth/refresh 自身 401 又触发 refresh"，
 *  login/register 本就不带 token。/auth/me 与 /auth/logout 是受保护端点，
 *  必须参与 401 → 静默刷新 → 重放（brief Step 3 要求 me 走 client 层静默处理；
 *  logout 也要能刷新后重放，否则服务端白名单吊销静默失效）。 */
const PUBLIC_AUTH_PATHS = new Set(['/auth/login', '/auth/register', '/auth/refresh']);

/** 401 → 静默 refresh 一次 → 重放。仅公开三端点不参与（refresh 死循环守卫）。 */
async function withAuthRetry(path: string, doFetch: () => Promise<Response>): Promise<Response> {
  let response = await doFetch();
  if (response.status === 401 && !PUBLIC_AUTH_PATHS.has(path)) {
    const ok = await trySilentRefresh();
    response = ok ? await doFetch() : response;
    if (!ok) {
      clearStoredTokens();
      onSessionExpired();
    }
  }
  return response;
}

async function trySilentRefresh(): Promise<boolean> {
  if (!inFlightRefresh) {
    inFlightRefresh = doSilentRefresh().finally(() => {
      inFlightRefresh = null;
    });
  }
  return inFlightRefresh;
}

async function doSilentRefresh(): Promise<boolean> {
  const { refresh } = getStoredTokens();
  if (!refresh) return false;
  try {
    // 这里走裸 fetch 而不是 request()：/auth/refresh 返回 401 时必须原样判失败，
    // 绝不能再进 withAuthRetry 触发递归刷新
    const r = await fetch(`${API_BASE}/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refresh }),
    });
    if (!r.ok) return false;
    const data = (await r.json()) as { access_token: string; refresh_token: string };
    // 轮转语义（06 §1.4）：服务端已删旧发新，两个都要存
    setStoredTokens(data.access_token, data.refresh_token);
    return true;
  } catch {
    return false;
  }
}

/** 后端业务异常的统一信封（由 app/core/exception_handlers.py 产出） */
interface ErrorEnvelope {
  code: string | number;
  message: string;
  details: unknown;
}

/** FastAPI 请求体校验失败（422）时的默认结构 */
interface ValidationEnvelope {
  detail: Array<{ loc?: Array<string | number>; msg?: string }>;
}

/**
 * 统一的 API 异常。页面通常只需要读 message 展示中文提示；
 * status / code 留给需要分支处理的场景（例如 404 表示会话已被删除）。
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code?: string | number;
  readonly detail?: unknown;

  constructor(message: string, status: number, code?: string | number, detail?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

/* ---------------- 限流通告（Phase 8b T11） ----------------
 * COMMON_429001 是 docs/06 §5 的契约码，前端只消费不新造。"顶部提示"属 UI，
 * client 层不认识组件树 —— 注册回调的形状照 setSessionExpiredHandler 同款，
 * AppLayout 挂载时接走文案渲全局横幅。
 * 注意：429 非 401，天然不进静默刷新链 —— withAuthRetry 的闸只对 401 开，
 * 限流重放只会再吃一次 429（假重试），正确动作是等 Retry-After 到点再来。 */
const RATE_LIMIT_CODE = 'COMMON_429001';

let onRateLimited: (message: string) => void = () => {};
export function setRateLimitNoticeHandler(fn: (message: string) => void): void {
  onRateLimited = fn;
}

/** Retry-After 头（后端 RateLimitedError.retry_after 经 exception_handlers 通用机制
 *  站成响应头，秒、整数）。非数字/缺失时返回 null，文案退化成不带秒数的一句。 */
function parseRetryAfterSeconds(response: Response): number | null {
  const raw = response.headers.get('retry-after');
  if (!raw) return null;
  const seconds = Number(raw);
  return Number.isFinite(seconds) && seconds >= 0 ? Math.ceil(seconds) : null;
}

/**
 * 判断是否为"用户主动中止"。按 name 判断而不是 instanceof DOMException：
 * 不同运行时（浏览器 / Node undici）抛出的类型不一致，name 才是稳定的。
 */
export function isAbortError(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    (error as { name?: string }).name === 'AbortError'
  );
}

/** 把任意异常转成可以直接展示给用户的中文文案（页面不要自己拼错误信息） */
export function toReadableError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message || '发生未知错误';
  return '发生未知错误';
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  params?: Record<string, string | number | undefined>;
  body?: unknown;
  signal?: AbortSignal;
}

function buildUrl(path: string, params?: RequestOptions['params']): string {
  const url = `${API_BASE}${path}`;
  if (!params) return url;
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    // 值为 undefined 的参数直接丢弃，避免拼出 ?offset=undefined
    if (value !== undefined) search.append(key, String(value));
  }
  const query = search.toString();
  return query ? `${url}?${query}` : url;
}

/** 网络层失败统一成一句可操作的中文提示，而不是把 "Failed to fetch" 甩给用户 */
function networkError(): ApiError {
  return new ApiError(
    '无法连接服务器，请确认后端已启动（http://127.0.0.1:8002）',
    0,
  );
}

/** 普通 JSON 请求：解析错误信封、非 2xx 抛 ApiError */
export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = 'GET', params, body, signal } = options;

  // 文件上传走 FormData。它和 JSON body 有两处必须区别对待（见下面注释）
  const isForm = body instanceof FormData;

  let response: Response;
  try {
    response = await withAuthRetry(path, () =>
      fetch(buildUrl(path, params), {
        method,
        // FormData 不能手写 Content-Type：multipart 需要一个 boundary 分隔符，
        // 这个 boundary 是浏览器生成并写进 Content-Type 的。我们手写成
        // 'multipart/form-data' 会丢掉 boundary，后端就切不出文件字段。
        // header 每次 doFetch 现读 localStorage：静默刷新重放时要用**新** token。
        headers:
          body === undefined || isForm ? authHeaders() : authHeaders({ 'Content-Type': 'application/json' }),
        // FormData 要原样传，JSON.stringify 会把它变成 "{}"
        body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
        signal,
      }),
    );
  } catch (error) {
    // fetch 只在网络层失败时 reject。AbortError 必须原样抛出：
    // 调用方靠它区分"用户点了停止"和"真的出错了"
    if (isAbortError(error)) throw error;
    throw networkError();
  }

  if (!response.ok) throw await parseErrorResponse(response);
  // 204 无响应体（DELETE 会话就是这个），直接 .json() 会抛解析错误
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/**
 * 流式请求入口。和 request 的区别：不解析响应体，把 Response 原样交给调用方
 * 去读 ReadableStream（SSE 是持续推送的，不能 await json()）。
 * 错误处理复用同一套逻辑，所以 404/422 在这里照样会抛 ApiError。
 * withAuthRetry 同样生效 —— SSE 面（chat 流式）自动带 Authorization，
 * access 过期时先静默刷新再重连一次。
 */
export async function streamRequest(
  path: string,
  body: unknown,
  signal: AbortSignal,
): Promise<Response> {
  let response: Response;
  try {
    response = await withAuthRetry(path, () =>
      fetch(buildUrl(path), {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
        signal,
      }),
    );
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw networkError();
  }

  if (!response.ok) throw await parseErrorResponse(response);
  return response;
}

/**
 * 解析错误响应。后端有两种错误体，必须都能认出来：
 *   1. 业务异常 → {"code": "CONV_404001", "message": "会话不存在", "details": null}
 *   2. 请求体校验失败（422）→ {"detail": [{"loc": [...], "msg": "..."}]}
 * 认不出来时给一句带状态码的兜底文案，绝不返回空 message。
 */
export async function parseErrorResponse(response: Response): Promise<ApiError> {
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    // 响应体不是 JSON（网关 502、代理返回 HTML 页面等）
    return new ApiError(`请求失败（HTTP ${response.status}）`, response.status);
  }

  if (isErrorEnvelope(payload)) {
    // 限流一档走专用文案（brief 钦定句 + Retry-After 秒数），不透传后端 message：
    // 契约码才是判据，message 措辞后端可以改，前端口径钉死。
    if (payload.code === RATE_LIMIT_CODE) {
      const seconds = parseRetryAfterSeconds(response);
      const message =
        seconds === null
          ? '操作过于频繁，请稍后再试'
          : `操作过于频繁，请稍后再试（约 ${seconds} 秒后可再试）`;
      onRateLimited(message);
      return new ApiError(message, response.status, payload.code, payload.details);
    }
    return new ApiError(
      payload.message || `请求失败（HTTP ${response.status}）`,
      response.status,
      payload.code,
      payload.details,
    );
  }

  if (isValidationEnvelope(payload)) {
    // loc 的第一段是 body/query/path，对用户没意义，去掉只留字段名
    const message = payload.detail
      .map((item) => {
        if (!item.msg) return '';
        const field = (item.loc ?? []).slice(1).join('.');
        return field ? `${field}: ${item.msg}` : item.msg;
      })
      .filter(Boolean)
      .join('；');
    return new ApiError(
      message || `请求参数不合法（HTTP ${response.status}）`,
      response.status,
      undefined,
      payload.detail,
    );
  }

  return new ApiError(`请求失败（HTTP ${response.status}）`, response.status);
}

function isErrorEnvelope(payload: unknown): payload is ErrorEnvelope {
  return (
    typeof payload === 'object' &&
    payload !== null &&
    'code' in payload &&
    'message' in payload
  );
}

function isValidationEnvelope(payload: unknown): payload is ValidationEnvelope {
  return (
    typeof payload === 'object' &&
    payload !== null &&
    Array.isArray((payload as ValidationEnvelope).detail)
  );
}
