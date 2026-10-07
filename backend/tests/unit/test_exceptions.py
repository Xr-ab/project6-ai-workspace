"""AppException 族契约（app/core/exceptions.py，实测 24 个子类）。

这族对外只承诺三件事：HTTP 状态码、字符串业务码、可展示 message，三件全写在类属性上，
运行期没有任何代码替你检查。于是「码里编的状态」和「类上的状态」会不会漂、
会不会撞号、会不会有子类忘了改掉基类默认——只有针能回答。

数量更正：spec §5.1 锚点行写「exceptions.py 21 个 AppException 子类」，随后实测 **23**
（差的是 8b T9 的限流两枚 COMMON_429001 / COMMON_503001），**Phase 9b 又加一枚
`ReportNotFoundError`（404 `REPORT_404001`）⇒ 现值 24**。本文件按 24 立针，
且计数本身是断言（test_family_census…），此后不再引 spec 的数字。
这两次都是「数据事实被实测否证」的例外，不是补实现。
"""
import inspect
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.exception_handlers import register_exception_handlers
from app.core.exceptions import (
    AppException,
    ConversationNotFoundError,
    DocumentParseError,
    OldPasswordIncorrectError,
    RateLimitedError,
)


def _family() -> dict[str, type[AppException]]:
    """exceptions 模块里除基类以外的全部 AppException 子类（按模块名字典序）。"""
    import app.core.exceptions as exceptions_module

    return {name: obj for name, obj in vars(exceptions_module).items()
            if isinstance(obj, type) and issubclass(obj, AppException)
            and obj is not AppException}


def _client_raising(exc: AppException) -> TestClient:
    """裸 FastAPI + 只挂异常处理器。

    不 import app.main：unit 层的定义就是「不建 engine、不读 DATABASE_URL」，
    而 app.main 的模块级副作用会把整套依赖拖进来。
    TestClient 只吃 httpx（生产依赖里已经有），不起 uvicorn、不连库、不跑 lifespan。
    """
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/probe")
    async def _probe():
        raise exc

    return TestClient(app)


def test_family_census_is_24_and_matches_the_source_text():
    """两个来源各数一遍：模块命名空间 + 源码里的 class 行。
    只数命名空间的话，「子类定义在别的模块却在 exceptions.py 里 re-export」会算进来；
    只数源码的话，动态造出来的类会漏。两个都数，漂了就红。"""
    family = _family()
    assert len(family) == 24
    src = Path(inspect.getsourcefile(AppException)).read_text(encoding="utf-8")
    declared = sum(1 for line in src.splitlines()
                   if line.startswith("class ") and "(AppException)" in line)
    assert declared == len(family)


def test_business_codes_are_unique_across_the_family():
    codes = [cls.code for cls in _family().values()]
    assert len(codes) == len(set(codes))


def test_every_code_encodes_its_own_http_status():
    """号段规则 = 模块_状态码_序号（docs/06 §5）。钉的是最容易漂的前三位。"""
    for name, cls in _family().items():
        prefix, digits = cls.code.split("_")
        assert prefix.isalpha() and prefix.isupper(), name
        assert len(digits) == 6 and digits.isdigit(), cls.code
        assert int(digits[:3]) == cls.status_code, name
        assert int(digits[3:]) >= 1, name


def test_no_subclass_keeps_the_base_defaults():
    """留着的后果：对外是 500 + APP_500000，前端只看到「服务异常」，
    日志里也没族名以外的线索——最难查的一类响应恰好是这种。"""
    for name, cls in _family().items():
        assert cls.code != AppException.code, name
        assert cls.status_code != AppException.status_code, name


def test_name_suffix_rules_hold():
    """后缀不是装饰，是三族安全口径：
    NotFound 一律 404（分 403 等于把「这个 id 存在」泄露给探测者）、
    Conflict/Duplicate 一律 409、Unavailable 一律 503（依赖挂了诚实停机而不是放行）。"""
    rules = {"NotFoundError": 404, "ConflictError": 409, "DuplicateError": 409,
             "UnavailableError": 503}
    checked = 0
    for name, cls in _family().items():
        for suffix, status in rules.items():
            if name.endswith(suffix):
                assert cls.status_code == status, name
                checked += 1
    assert checked == 12          # 6×NotFound + 3×Conflict + 1×Duplicate + 2×Unavailable
                                  # （6×NotFound：Phase 9b 的 ReportNotFoundError 是第六枚）


def test_every_class_constructs_with_no_args_and_fills_display_fields():
    """全部子类都得能被「不带参数就 raise」使用——调用点就是这么写的。"""
    for name, cls in _family().items():
        exc = cls()
        assert isinstance(exc.message, str) and exc.message, name
        assert str(exc) == exc.message, name      # 基类 super().__init__(message) 的结果
        assert exc.detail is None, name


def test_base_defaults_are_the_documented_unknown():
    exc = AppException()
    assert (exc.status_code, exc.code, exc.message, exc.detail) == (
        500, "APP_500000", "服务异常", None)


def test_old_password_wrong_is_403_not_401():
    """Phase 9a 裁定 R4：401 会触发前端 client 的「静默 refresh + 重放，
    refresh 失败就清 token 跳登录」——填错旧口令不该被踢出会话。
    这个语义只能靠状态码表达，所以单独钉一枚。"""
    assert (OldPasswordIncorrectError.status_code, OldPasswordIncorrectError.code) == (
        403, "AUTH_403003")


def test_handler_json_uses_the_details_key_not_the_attribute_name():
    """属性叫 `detail`，响应键叫 `details`（06 §1.3）。
    名字差一个 s，只有真发一次请求的针能钉住它。"""
    resp = _client_raising(ConversationNotFoundError()).get("/probe")
    assert resp.status_code == 404
    assert resp.json() == {"code": "CONV_404001", "message": "会话不存在",
                           "details": None}


def test_retry_after_header_appears_only_when_the_attribute_is_set():
    """handler 用 `getattr(exc, "retry_after", None)` 的通用机制，不分表认子类。
    钉两面：设了就有头（值逐字对上），没设就不许有头。"""
    resp = _client_raising(RateLimitedError(retry_after=7)).get("/probe")
    assert resp.status_code == 429
    assert resp.headers.get("retry-after") == "7"

    other = _client_raising(DocumentParseError()).get("/probe")
    assert other.status_code == 422
    assert other.headers.get("retry-after") is None


def test_only_rate_limited_declares_retry_after():
    """今天 `retry_after` 的唯一持有者必须是限流那枚。
    多一枚 = 有子类悄悄开始给客户端发 Retry-After；少一枚 = 429 的契约头没了。"""
    holders = {name for name, cls in _family().items() if hasattr(cls(), "retry_after")}
    assert holders == {"RateLimitedError"}
