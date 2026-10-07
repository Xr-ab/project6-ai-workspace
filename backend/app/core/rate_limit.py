"""API 限流（Phase 8b T9）：Redis ZSET 滑动窗口，几十行自建小模块（spec §6）。

键形（brief Step 2 逐字）：`rl:{bucket}:{identity}`
- 鉴权面 identity = user UUID 字符串（桶 task / upload，spec §6 表第二、三行）；
- 登录面 identity **两把**：`ip:{client_host}` 与 `email:{归一化邮箱}`（桶 auth，
  表第一行「IP + email 双键」）——换 IP 绕不开 email 腿，换邮箱绕不开 ip 腿。

判定纪律：**zcard 判定后拒也不撤销**（拒绝即计数）——被拒请求的 zadd 留在窗内，
爆破重试只会自然变慢；这是刻意的不对称（撤销 = 给爆破者免单）。

Redis 宕机口径（与 T8 缓存面故意相反，spec §6 逐字）：受闸面一律 503
（COMMON_503001），**不做「挂了就放行」**——宕机窗口就是爆破窗口，静默放行是
反向假安全；never-raise 只属于加速面（task_cache），闸面没有这个特权。

登录面读 body（Step 2 预告的坑，实测依据）：依赖里 `await request.body()` 不
消费路由的流——starlette Request 首次读取后缓存在 `_body`，且 FastAPI 的 body
校验与本依赖共用**同一个 Request 实例**（scope 级缓存）。真针实测：连打 10 次
错密码 login 全 401（401 = 路由真实拿到 body 查了库；若流被吃掉只会 422/500），
第 11 次才 429（scratch/test_p8b_ratelimit.py 针⑧）。故维持依赖注入形态，
不必退回「路由体首行调 enforce」。

上限值不写死在这里：Settings 三档字段（rate_limit_{auth,task,upload}_per_min），
挂载点以 `lambda: settings.*` 传入 → 测试 monkeypatch settings 即抬上限（接缝），
生产默认值一毫米不放宽。
"""
import json
import logging
import math
import time
import uuid
from typing import Annotated, Callable, Union

from fastapi import Depends, Request
from redis.exceptions import RedisError

from app.api.deps import get_current_user
from app.core.exceptions import RateLimitedError, RateLimitUnavailableError
from app.core.security import get_redis
from app.data.models import User

logger = logging.getLogger(__name__)

# limit 允许直给整数或零参 callable（callable = 测试抬上限的接缝，见模块 docstring）
LimitLike = Union[int, Callable[[], int]]

DEFAULT_WINDOW_S = 60  # spec §6 表：三档全是 per-min 口径


async def enforce(identity: str, bucket: str, limit: int,
                  window_s: int = DEFAULT_WINDOW_S) -> None:
    """滑窗判定：超限抛 RateLimitedError(429, retry_after=ceil(窗口-已过秒))；
    Redis 不可用抛 RateLimitUnavailableError(503)——两条都是**必须响**的路径。

    一次 pipeline 五连（transaction=False：计数精确性靠 zset 自身语义，
    不靠事务——多 worker 并发下 ±1 的窗口毛刺对「限速」这个目的无感；
    如未来需要精确计数，把这条 pipeline 改成 MULTI/EXEC 或 Lua 单命令即可，
    判定分支零改动）：
      zremrangebyscore  扫掉窗外出站（滑窗的「滑」）
      zadd nx           本次进站；member 带 uuid 必属新条目，nx 只是防御裸重放
      zcard             进站后的在窗数 = 判定值
      zrange 0 0 with   最早进站者（retry_after 的原料；无新输入时零成本备着）
      expire            键 TTL 兜底：纯读面不再来时整键自清，不占内存
    """
    key = f"rl:{bucket}:{identity}"
    now = time.time()
    member = f"{now:.6f}-{uuid.uuid4().hex}"
    try:
        pipe = get_redis().pipeline(transaction=False)
        pipe.zremrangebyscore(key, 0, now - window_s)
        pipe.zadd(key, {member: now}, nx=True)
        pipe.zcard(key)
        pipe.zrange(key, 0, 0, withscores=True)
        pipe.expire(key, window_s)
        _, _, count, oldest, _ = await pipe.execute()
    except (RedisError, OSError) as exc:
        # T9 评审轮窄化的异常族（原为裸 except Exception）：只有 Redis 自身故障
        # （连接/协议/超时，RedisError 族）与 socket 层断（OSError 族——内置
        # ConnectionError/TimeoutError 皆其子类，真容器宕机走这条）才染成 503。
        # 其余异常 = 本模块或调用方的编程错误（参数误用、属性拼错…），照旧响亮
        # 外抛进 500 + 日志——编程错误不得被伪装成「依赖不可用」的 503 假安全。
        raise RateLimitUnavailableError() from exc
    if count > limit:
        # 滑窗的诚实账：还要多久最早那位出站，窗内才腾出额度
        retry_after = window_s
        if oldest:
            retry_after = max(1, math.ceil(oldest[0][1] + window_s - now))
        logger.warning("限流命中 bucket=%s identity=%s count=%s/%s retry_after=%s",
                       bucket, identity, count, limit, retry_after)
        raise RateLimitedError(retry_after=retry_after)


def _resolve_limit(limit_per_min: LimitLike) -> int:
    return limit_per_min() if callable(limit_per_min) else limit_per_min


def rate_limit_dep(bucket: str, limit_per_min: LimitLike):
    """鉴权面依赖工厂：identity = str(user.id)。

    闸内嵌 Depends(get_current_user)：① 鉴权先于限流——伪造/缺失 token 的人
    401 走人，不给他在别人的桶里记账的机会（针⑩）；② FastAPI 按请求缓存
    依赖结果，与路由参数 CurrentUser 同源，不多打一次 DB。
    （简报的 request.state.user 形态实测不存在于本仓——身份一直走依赖注入，
    这里以依赖复用达成同一语义。）
    """
    async def _dep(user: Annotated[User, Depends(get_current_user)]) -> None:
        await enforce(identity=str(user.id), bucket=bucket,
                      limit=_resolve_limit(limit_per_min), window_s=DEFAULT_WINDOW_S)
    return _dep


def _client_ip(request: Request) -> str:
    # ⚠️ 反向代理口径（Phase 11a 起）：request.client.host 取的是 TCP 对端地址。容器形态
    # 下 uvicorn 带 --proxy-headers + --forwarded-allow-ips（docker-compose.yml），nginx 侧
    # $proxy_add_x_forwarded_for，于是这里拿到的是 XFF 采信结果而不是 nginx 的地址——
    # 「所有登录塌进代理一个桶」那条老警示已出清（实测成对证据见 docs/06 §6.5）。
    # 残余信任边界：**采信到的不是「真实客户端」**。uvicorn 从右往左扫 XFF 取第一个不可信
    # 地址，而 nginx 是追加——客户端自带的伪造头就坐在真值左边，成了这里的返回值。
    # ⇒ ip 腿只作粗粒度兜底（挡无脑脚本），真隔离靠鉴权身份腿（user.id / email 不可伪造）；
    # 别把 ip 腿当安全边界对外承诺。彻底修法（nginx 覆写而非追加 / 只信最后一跳）留到真上公网。
    client = request.client
    return client.host if client else "unknown"


async def _email_from_body(request: Request) -> str | None:
    """登录面取 email：raw body 里捞，形状不对（非 JSON / 无此键）不硬造身份——
    那种请求路由那头必 422，限流只按 ip 腿记账（爆破者构造畸形包也在计数面上）。
    归一化 strip+lower：桶键与账本一致，大小写换不来额度。
    """
    try:
        data = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        return None
    email = data.get("email") if isinstance(data, dict) else None
    return email.strip().lower() if isinstance(email, str) and email.strip() else None


def rate_limit_login_dep(bucket: str, limit_per_min: LimitLike):
    """登录/注册面（公开面）依赖工厂：IP + email **双键**各自 enforce。

    与鉴权面不同工厂的原因：这里绝不能 Depends(get_current_user)——那是
    「未带 token → 401 先于限流」，公开面人人未带 token，闸就永远不落地。
    两腿先后各计一次：任一腿超限即 429（先 ip 后 email，报错形状相同不分序）。
    body 读取不消费路由的流——实测依据见模块 docstring。
    """
    async def _dep(request: Request) -> None:
        limit = _resolve_limit(limit_per_min)
        identities = [f"ip:{_client_ip(request)}"]
        email = await _email_from_body(request)
        if email:
            identities.append(f"email:{email}")
        # 双键的边角语义（T9 评审轮写明，非缺陷是裁定）：先 ip 腿后 email 腿，
        # ip 腿耗尽即抛 429 —— 该被拒请求**不再走 email 腿的 enforce**，
        # email 桶在这一击零进账（对同 IP 高频爆破者，email 腿恒冻结，直到
        # ip 腿滑出才轮到它记账）。取舍依据：拒绝的响应形状/Retry-After 两腿
        # 本就同形，多计一腿不改变任何被拒请求的命运；少计一腿则保证「换 IP 即
        # 撞 email 腿」的慢速枚举路径始终按 email 独立记账（针⑨双 IP 矩阵验证
        # 的正是这条）。若要两腿恒各计一次，需先收集后判定——收益仅是账面对称。
        for identity in identities:
            await enforce(identity=identity, bucket=bucket, limit=limit,
                          window_s=DEFAULT_WINDOW_S)
    return _dep
