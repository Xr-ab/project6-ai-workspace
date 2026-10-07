"""业务异常：与 HTTP 层解耦，由 exception_handlers 统一翻译为响应。"""
from typing import Optional


class AppException(Exception):
    """业务异常基类。子类定义 status_code，通过 message 传入可展示的错误信息。"""

    status_code: int = 500
    # 业务错误码：模块_HTTP状态码_序号（见 docs/06-api-design.md §5）
    code: str = "APP_500000"

    def __init__(self, message: str = "服务异常", detail: Optional[str] = None):
        self.message = message
        self.detail = detail
        super().__init__(message)


class LLMServiceError(AppException):
    """LLM 调用失败（网络 / 鉴权 / 服务端错误）。"""

    status_code = 502
    code = "LLM_502001"


class ConversationNotFoundError(AppException):
    """会话不存在，或不属于当前用户。

    两种情况故意返回同一个结果（404）而不是"无权限"（403）——
    否则攻击者可以靠状态码差异探测某个 id 是否存在。
    """

    status_code = 404
    code = "CONV_404001"

    def __init__(self, message: str = "会话不存在"):
        super().__init__(message)


class UnsupportedFileTypeError(AppException):
    """上传了不支持的文档格式（Phase 2）。"""

    status_code = 415
    code = "DOC_415001"

    def __init__(self, message: str = "不支持的文件类型"):
        super().__init__(message)


class DocumentParseError(AppException):
    """文档解析失败（文件损坏、加密、扫描件无可提取文本等）。

    为什么和 UnsupportedFileTypeError 分开：
        前者是"这个格式我不支持"（415，用户改格式就行），
        后者是"格式支持但这份文件解析不了"（422，文件本身有问题）。
        排查时状态码直接区分两类原因。
    """

    status_code = 422
    code = "DOC_422001"

    def __init__(self, message: str = "文档解析失败"):
        super().__init__(message)


class FileTooLargeError(AppException):
    """上传文件超过大小上限（Phase 2）。"""

    status_code = 413
    code = "DOC_413001"

    def __init__(self, message: str = "文件超过大小上限"):
        super().__init__(message)


class DocumentNotFoundError(AppException):
    """文档不存在，或不属于当前组织。

    和 ConversationNotFoundError 同理：两种情况返回同一个 404，
    避免靠状态码差异探测某个 id 是否存在。
    """

    status_code = 404
    code = "DOC_404001"

    def __init__(self, message: str = "文档不存在"):
        super().__init__(message)


class DocumentDuplicateError(AppException):
    """同一组织内已存在内容完全相同的文档（Phase 2 去重）。

    用 409 Conflict 而不是 400：请求本身没问题，是"和当前资源状态冲突"。
    也不能返回 200 + 已有文档 —— 那样调用方无法区分
    "这次真的新建了"和"其实什么都没发生"，前端会重复提示上传成功。
    """

    status_code = 409
    code = "DOC_409001"

    def __init__(self, message: str = "该文件已存在，无需重复上传"):
        super().__init__(message)


# ---- Phase 5：任务 / 执行（Agents API）----


class TaskNotFoundError(AppException):
    """任务不存在或不属于当前组织/用户。404 不区分"无权限"，避免探测 id 是否存在。"""

    status_code = 404
    code = "TASK_404001"

    def __init__(self, message: str = "任务不存在"):
        super().__init__(message)


class TaskRunConflictError(AppException):
    """同一任务已有 running 的执行，不能再触发新的（docs/06 §5 重复提交）。"""

    status_code = 409
    code = "TASK_409001"  # 编码规则=模块_HTTP状态码_序号（docs/06 §5）；409 冲突不是 400

    def __init__(self, message: str = "任务正在执行中，请勿重复提交"):
        super().__init__(message)


class ApprovalConflictError(AppException):
    """审批决策落在非 pending 的审批 / 非 waiting_approval 的任务上（Phase 7 Task 6）。

    注册方式与 TaskRunConflictError 同形：handler 不分表映射，统一按 AppException 的
    status_code/code/message 翻译（core/exception_handlers.py），所以这里只需定义类。
    竞态语义：并发双决策时条件 UPDATE...WHERE status='pending' RETURNING 0 行 = 输者，
    本异常就是那把 409 锁的对外形状（不许读后写裸改）。
    号段：WF_ 前缀是 Phase 7 workflow 模块新开段，docs/06 §5 的登记随 Task 7 端点组落地。
    """

    status_code = 409
    code = "WF_409001"

    def __init__(self, message: str = "审批状态已变更（已决策过或任务不在等待审批）"):
        super().__init__(message)


class WorkflowNotFoundError(AppException):
    """Workflow 编目行不存在，或不属于当前组织（Phase 7 Task 7）。

    与 TaskNotFoundError 分码：编目不是任务，前端按 code 分支时「找不到 workflow」
    与「找不到 task」是两个提示；404 不区分跨租户，探测防护同 CONV/DOC 口径。
    docs/06 §5 登记 `WF_404001`（与 WF_409001 同一批，随 Task 7 端点组落地）。
    """

    status_code = 404
    code = "WF_404001"

    def __init__(self, message: str = "Workflow 不存在"):
        super().__init__(message)


# ---- Phase 6 Evaluation（docs/06 §5 的 EVAL_* 号段）----


class EvaluationParamError(AppException):
    """用例集为空 / 参数非法（docs/06 §5 已登记此码，必须逐字用 EVAL_400001）。"""

    status_code = 400
    code = "EVAL_400001"

    def __init__(self, message: str = "用例集为空或参数不合法"):
        super().__init__(message)


class EvaluationNotFoundError(AppException):
    """评测数据集 / Run / 用例不存在（或不属于当前租户）。

    404 与 403 故意不分：与 ConversationNotFoundError 同理 —— 分开的状态码
    等于允许外部靠差异探测某个 id 是否存在。
    """

    status_code = 404
    code = "EVAL_404001"

    def __init__(self, message: str = "评测对象不存在"):
        super().__init__(message)


class EvaluationConflictError(AppException):
    """评测 Run 状态不允许该操作（未完成就想比、已完成还想重跑）。"""

    status_code = 409
    code = "EVAL_409001"

    def __init__(self, message: str = "评测状态不允许该操作"):
        super().__init__(message)


class ReportNotFoundError(AppException):
    """报告不存在，或不属于当前组织（Phase 9b）。

    与全仓同形 404 一套口径（ConversationNotFoundError 的先例）：跨 org 探测
    与"真没这个 id"逐字段同形，不靠 403 区分错因 —— 能区分就等于泄漏存在性。
    """

    status_code = 404
    code = "REPORT_404001"

    def __init__(self, message: str = "报告不存在"):
        super().__init__(message)


# ---- Phase 8a：身份与访问（docs/06 §5 的 AUTH_* 号段；401/403/409 五枚在档，503 为本计划补登）----


class InvalidTokenError(AppException):
    """access token 缺失/无效/过期；登录凭证错误也走此码（06 未单列，裁定 Global-3）。"""

    status_code = 401
    code = "AUTH_401001"

    def __init__(self, message: str = "登录已过期或凭证无效"):
        super().__init__(message)


class RefreshTokenRevokedError(AppException):
    """refresh token 不在白名单（已轮转/已登出/从未签发）——三者在外部不可分，同码同形。"""

    status_code = 401
    code = "AUTH_401002"

    def __init__(self, message: str = "refresh token 已失效，请重新登录"):
        super().__init__(message)


class RolePermissionError(AppException):
    """角色不满足 require_role（如 member 调 admin-only 的审计查询）。"""

    status_code = 403
    code = "AUTH_403001"

    def __init__(self, message: str = "当前角色无权执行该操作"):
        super().__init__(message)


class ToolPermissionError(AppException):
    """工具权限不足（06 §6.3 矩阵）。执行闸口在 registry.execute，对外形状见其拒绝文案。"""

    status_code = 403
    code = "AUTH_403002"

    def __init__(self, message: str = "当前角色无权使用该工具"):
        super().__init__(message)


class OldPasswordIncorrectError(AppException):
    """改口令时旧口令不匹配。

    为什么是 403 而不是 401（Phase 9a 裁定 R4）：前端 client 对 401 的动作是
    「静默 refresh + 重放，refresh 失败就清 token 跳登录」—— 把"填错旧口令"发成 401
    会让用户在 Settings 里打错一个字就被踢出会话。401 留给"身份不可信"，
    403 留给"身份可信但这次操作不被接受"，这里正是后者。
    """

    status_code = 403
    code = "AUTH_403003"

    def __init__(self, message: str = "旧口令不正确"):
        super().__init__(message)


class EmailAlreadyRegisteredError(AppException):
    """邮箱已注册（06 §5 AUTH_409001）。不透露该邮箱属于哪个组织。"""

    status_code = 409
    code = "AUTH_409001"

    def __init__(self, message: str = "该邮箱已注册"):
        super().__init__(message)


class RefreshUnavailableError(AppException):
    """Redis 不可用导致 refresh 无法验签/轮转/吊销。

    宁可 503 也不做 DB 兜底白名单：兜底 = 静默丢掉"可吊销"契约的假安全
    （spec §2.2，与「未测不渲零」同族的诚实原则）。
    """

    status_code = 503
    code = "AUTH_503001"

    def __init__(self, message: str = "会话服务暂时不可用，请稍后重试"):
        super().__init__(message)


# ---- Phase 8b：限流（T9；两枚都是 docs/06 §5 预登记契约码，不新造）----


class RateLimitedError(AppException):
    """限流超限（06 §5 COMMON_429001，契约面本就写着「附 Retry-After」）。

    retry_after（秒，ceil 到整数）由 exception_handlers 以
    `getattr(exc, "retry_after", None)` 的**通用机制**读出站成响应头——
    handler 不分表识别异常子类（那才是加 if-else 分支的反面教材）；
    没有这个属性的异常，自然没有这个头。
    """

    status_code = 429
    code = "COMMON_429001"

    def __init__(self, message: str = "操作过于频繁，请稍后重试",
                 retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimitUnavailableError(AppException):
    """限流依赖的 Redis 暂不可用（06 §5 COMMON_503001，R-8b-4 裁定文档先行登记）。

    口径与 T8 缓存面**故意相反**（spec §6 逐字）：缓存是加速面，写失败只 warning、
    静默回落 PG；限流闸是契约面——「挂了就放行」等于把宕机窗口变成爆破窗口，
    是反向假安全。所以受闸面一律 503 诚实停机，与 8a AUTH_503001 同族裁定。
    """

    status_code = 503
    code = "COMMON_503001"

    def __init__(self, message: str = "服务暂时不可用，请稍后重试"):
        super().__init__(message)
