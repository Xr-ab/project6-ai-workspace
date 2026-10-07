"""三条 Demo 的共用工具箱（spec §6.2 的「脚本层」）。

设计约束只有一条：**判定必须可脱离容器测**。所以这里除复用 `loadtest` 的 login（避免同一段登录
逻辑出现第二份）之外，不 import 任何项目内部模块，全文件无 asyncio（三条 Demo 都是同步 httpx），
网络只在 ApiClient 内部发生，时钟与 sleep 都是 poll 的参数。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from loadtest.loadtest import login  # DRY：登录只有一份实现

TERMINAL_STATUSES = frozenset({"completed", "failed", "rejected"})
API_PREFIX = "/api/v1"
LLM_HARD_STOP = 40        # 单位是 agent 节点数（不是补全次数，见 llm_node_count）；spec §6.5 的尺子以 worker 日志实测为准


class DemoAssertion(Exception):
    """机械断言失败：带着「期望什么」的原文，让工件自己解释自己。"""


class DemoTimeout(Exception):
    """轮询超时。**带着最后一次读数**——只报「超时」的脚本会让人去猜是不是差一步。"""

    def __init__(self, message: str, last: Any):
        super().__init__(message)
        self.last = last


def env_required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise KeyError(f"缺环境变量 {name}（Demo 只能在 docker/run_on_demo_stack.sh 里跑）")
    return value


def require(cond: bool, what: str) -> None:
    if not cond:
        raise DemoAssertion(f"机械断言失败：{what}")


def classify(status: str, result: object) -> str:
    """任务终态 + 结果 → 三值判定。

    `completed` 但没有 result 是装配层能造出来的**假通过**，单独判红；
    `rejected` 在 Demo 3 里由「不批准」分支产生，那条路径期望的就是它，所以不叫 fail。
    """
    if status == "completed":
        return "pass" if result is not None else "empty_result"
    return f"wrong_terminal:{status}"


# ---- SSE ----


def parse_sse(raw: str) -> list[dict]:
    """把 `data: <json>` 帧解成 list；`[DONE]` 变成 {"done": True} 哨兵。

    后端每一帧都是 `data: <json>\\n\\n`（api/chat.py 原文），注释行与空行按 SSE 规范跳过。
    """
    frames: list[dict] = []
    for block in raw.split("\n\n"):
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                frames.append({"done": True})
            elif payload:
                frames.append(json.loads(payload))
    return frames


def first_frame_is_citations(frames: list[dict]) -> bool:
    return bool(frames) and isinstance(frames[0].get("citations"), list) and len(frames[0]["citations"]) > 0


def text_blob(frames: list[dict]) -> str:
    return "".join(f.get("text", "") for f in frames if "text" in f)


def citation_document_ids(frames: list[dict]) -> set[str]:
    out: set[str] = set()
    for f in frames:
        for c in f.get("citations") or []:
            if c.get("document_id"):
                out.add(str(c["document_id"]))
    return out


# ---- Trace ----


def llm_node_count(nodes: list[dict]) -> int:
    """带 model 的 agent 节点数 = 补全次数的**代理下界**，不是补全次数本身。

    一手换算：11a 那次真跑 `agent_runs=9` 对 `chat/completions`=13（`backend/scratch/
    _p11a_t5/exit-round2-after.txt` + `worker-log-exit.out`），比值 1.44；节点内部工具环的
    多次补全被并进同一条 span（`app/ai/graph/errors.py:171` 起）。真正的 40 次尺子是每轮
    `worker.log` 里 `grep -c 'chat/completions'`，Task 7 跑完一条就抄一次实测。
    """
    return sum(1 for n in nodes if n.get("model"))


def within_hard_stop(nodes: list[dict], limit: int = LLM_HARD_STOP) -> None:
    """超硬停判红。判定只能落在终态之后：span 由 `task_runner._persist_agent_runs` 在链路
    结束时一次性落库（`app/application/task_runner.py:169`），运行中途 API 读不到 ⇒
    中途的天花板是 `poll` 的 `timeout_s`，这道闸负责的是「超限之后本批不再往下跑」。
    """
    n = llm_node_count(nodes)
    require(n <= limit, f"模型调用数 {n} 超过硬停 {limit}（spec §6.5 的预算纪律）")


def cost_face(client: "ApiClient") -> dict:
    """全窗统计里的钱门读数（三个键，别的都不取）。

    口径提醒（docs/13 必须一起写）：`pricing_configured=False` 时 `total_cost` 恒 0
    （`app/ai/pricing.py:15-22`：两把单价键**都**有值才算配置好），
    所以「成本 0」有两种成因，两个值要一起落进回执才说得清是哪一种。
    """
    r = client.get("/stats/overview", params={"range": "all"})
    require(r.status_code == 200, f"/stats/overview 应 200，实得 {r.status_code}")
    cards = r.json()["cards"]
    return {"total_cost": cards["total_cost"],
            "pricing_configured": cards["pricing_configured"],
            "total_tokens": cards["total_tokens"]}


# ---- 轮询 ----


def poll(fetch: Callable[[], Any], until: Callable[[Any], bool], *,
         timeout_s: float, interval_s: float,
         clock: Callable[[], float] = time.monotonic,
         sleeper: Callable[[float], None] = time.sleep) -> Any:
    """带 deadline 的轮询（同步版：三条 Demo 都是同步 httpx 客户端）。

    `clock` 与 `sleeper` 是参数 ⇒ 「超时」这条分支能被测，不用真等 40 秒。
    """
    deadline = clock() + timeout_s
    last: Any = None
    while True:
        last = fetch()
        if until(last):
            return last
        if clock() >= deadline:
            raise DemoTimeout(
                f"轮询 {timeout_s}s 超时，最后一次读数："
                f"{json.dumps(last, ensure_ascii=False, default=str)[:400]}", last)
        sleeper(interval_s)


# ---- 客户端 ----


class ApiClient:
    """带 token 的同步 httpx 客户端，路径一律自动前缀 /api/v1。"""

    def __init__(self, origin: str, token: str, *, transport=None):
        self.api_base = origin.rstrip("/") + API_PREFIX
        self._c = httpx.Client(transport=transport, timeout=httpx.Timeout(60.0),
                               headers={"Authorization": f"Bearer {token}"})

    def get(self, path: str, params: dict | None = None) -> httpx.Response:
        return self._c.get(self.api_base + path, params=params)

    def post(self, path: str, json_body: dict | None = None) -> httpx.Response:
        return self._c.post(self.api_base + path, json=json_body or {})

    def post_form(self, path: str, *, data: dict, files: dict) -> httpx.Response:
        return self._c.post(self.api_base + path, data=data, files=files)

    def stream_chat(self, payload: dict) -> list[dict]:
        """把 SSE 一次读到底再解析（Demo 要的是「帧序列」这份证据，不是打字机效果）。"""
        with self._c.stream("POST", self.api_base + "/chat/stream", json=payload) as r:
            require(r.status_code == 200, f"/chat/stream 应 200，实得 {r.status_code}")
            raw = r.read().decode("utf-8")
        return parse_sse(raw)

    def login(self, origin: str, email: str, password: str) -> str:
        return login(origin, email, password)

    def close(self) -> None:
        self._c.close()


def make_client() -> tuple[ApiClient, str]:
    """从环境变量装配（P6_ORIGIN / P6_DEMO_EMAIL / P6_DEMO_PASSWORD）。返回 (client, origin)。"""
    origin = env_required("P6_ORIGIN")
    token = login(origin, env_required("P6_DEMO_EMAIL"), env_required("P6_DEMO_PASSWORD"))
    return ApiClient(origin, token), origin


# ---- 回执 ----


def write_receipt(art_dir: str, name: str, payload: dict) -> Path:
    """回执 = 一份自解释的 JSON：结论 + 机械断言清单 + 关键 id + 模型调用数。

    绝不写 token、绝不写口令、绝不写响应里可能带凭据的原文（全局凭据禁令）。
    """
    p = Path(art_dir)
    p.mkdir(parents=True, exist_ok=True)
    out = p / f"{name}-receipt.json"
    body = dict(payload)
    body.setdefault("written_at", time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z")
    out.write_text(json.dumps(body, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return out
