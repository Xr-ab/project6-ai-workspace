"""结构化 Logging（spec §7）：stdlib + 手写 JSON Formatter，不引 structlog。

核心卖点：日志行与 audit_logs 行按 request_id 互对账（出口针⑦验它）——
复用 8a 的 REQUEST_ID contextvar（audit.py:19），一个 logging.Filter 注入每行。
"""
import json
import logging

from app.core.audit import REQUEST_ID


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = REQUEST_ID.get() or None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        from datetime import datetime, timezone
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(getattr(h, "_p8b_json", False) for h in root.handlers):
        logging.getLogger().setLevel(level)   # 幂等：已装配只调级
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    handler._p8b_json = True
    root.handlers.clear()      # 开发机默认 stderr 文本行一并清掉：两制式混排比缺格式更难读
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []       # 让 uvicorn 日志走 root 的 JSON handler
        lg.propagate = True
