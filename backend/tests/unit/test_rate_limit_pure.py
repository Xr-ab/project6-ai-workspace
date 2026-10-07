"""限流里不碰 redis 的那半（app/core/rate_limit.py:46 / 92 / 111 / 124）。

enforce 的滑窗判定属于 integration 层（Task 3），这里只收 identity 归一化与
上限接缝——这两处错了，redis 面上的断言全都在测别的东西。
"""
import pytest
from starlette.requests import Request

from app.core.rate_limit import DEFAULT_WINDOW_S, _client_ip, _email_from_body, _resolve_limit


def _request(body: bytes | None = None, client=None) -> Request:
    async def receive():
        return {"type": "http.request", "body": body or b"", "more_body": False}
    return Request({"type": "http", "headers": [], "client": client}, receive)


def test_limit_accepts_int_or_zero_arg_callable():
    """callable 分支是测试抬上限的唯一接缝，生产默认值一毫米不放宽。"""
    assert _resolve_limit(10) == 10
    assert _resolve_limit(lambda: 20) == 20
    assert DEFAULT_WINDOW_S == 60


def test_client_ip_falls_back_to_unknown_when_scope_has_no_peer():
    assert _client_ip(_request(client=("10.1.2.3", 5555))) == "10.1.2.3"
    assert _client_ip(_request(client=None)) == "unknown"


@pytest.mark.asyncio
async def test_email_identity_is_normalised_strip_and_lower():
    assert await _email_from_body(_request(b'{"email":"  A@B.com  "}')) == "a@b.com"


@pytest.mark.asyncio
async def test_malformed_bodies_do_not_invent_an_identity():
    assert await _email_from_body(_request(b"not json")) is None
    assert await _email_from_body(_request(b"")) is None
    assert await _email_from_body(_request(b'[]')) is None
    assert await _email_from_body(_request(b'{"email":null}')) is None
    assert await _email_from_body(_request(b'{"email":"   "}')) is None
    assert await _email_from_body(_request(b'{}')) is None
