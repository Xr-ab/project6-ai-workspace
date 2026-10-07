"""异常处理器：把业务异常翻译成统一 JSON 响应。"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.exceptions import AppException


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException):
        # 响应结构与 docs/06-api-design.md §1.3 约定一致：
        # code 为字符串业务码（如 CONV_404001），details 放补充信息（可为 null）
        # Phase 8b T9 通用机制：凡带 retry_after 属性的业务异常都站出 Retry-After 头
        # （现唯一产生者 = 限流 429，06 §5 契约「附 Retry-After」）。用 getattr 而不是
        # isinstance 分支：handler 不认具体子类，今后哪个码要带头，加属性即可。
        retry_after = getattr(exc, "retry_after", None)
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "details": exc.detail,
            },
            headers=headers,
        )
