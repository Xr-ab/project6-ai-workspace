"""Phase 11c 离线压测器（spec §5）：closed-loop asyncio + httpx，不引 k6/locust。

三条刻意的取舍，写给半年后的读者：
  1. **closed-loop**：一个虚拟用户串行发请求、每次之间自报 100ms 思考时间。量到的是
     「这批并发下的终端延迟」，不是开环到达率下的排队延迟；两者别混引（spec §2 表一第 2 行）。
  2. **延迟含 body**：计时包到 `aread()` 之后。StreamingResponse 不在压测面里（spec §11）。
  3. **统计口径是纯函数**：真跑只负责产出 (毫秒, 状态码) 原始样本；「数字能不能信」全在这
     几个函数里，所以它们能在没有容器的机器上被单测证（spec §8）。

路径口径（读码的人最容易踩的一处）：`Target.path` **一律从 origin 起算**、自带 `/api/v1`
前缀，`main` 只做 `origin.rstrip("/")`。理由是两个真实存在的挂载点不对称：
业务路由挂在 `/api/v1` 下（app/main.py 的 include_router），而 `/healthz` 挂在 app 根上，
且 nginx 的 `location = /healthz` 自己应答一段 json、根本不反代到 app（frontend/nginx.conf）。
所以「base 里预置 /api/v1」会让 floor 行永远 404，而那一行是「floor = nginx 静态参照」
这条口径的载体。约定由 test_read_targets_are_all_non_mutating 的两根针钉住，改的人不会无声得利。

真跑的生命周期纪律（默认项目零接触、拆栈前取证）在 docker/run_on_demo_stack.sh 里，不在这里。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@dataclass(frozen=True)
class Target:
    key: str
    method: str
    path: str
    money: bool = False


# 六条 app 侧只读面：spec §5.2 表格点名的六个 key，按 spec 的表序写（两条 stats 聚合在最前——
# 它们是「聚合是 SQL 活，最可能先撞池」（spec §5.2:133）这条归因的唯一设计见证，换了就没人补得回来）。
# 全部是 GET ⇒ 零写入零模型调用，唯一例外是 SUBMIT_TARGET（202 容量档要测的就是这条队列入口）。
# 集合由 test_read_target_keys_are_the_six_faces_the_spec_names 按名字钉住；条数由
# test_read_targets_are_all_non_mutating 钉住（Task 4 的「7 target × 4 档 = 28 行」算式依赖 6+1）。
# path 自带 /api/v1（见模块 docstring 的路径口径）——这里写的是最终请求路径，不是路由前缀。
READ_TARGETS: tuple[Target, ...] = (
    Target("stats_overview", "GET", "/api/v1/stats/overview?range=all"),
    Target("stats_usage", "GET", "/api/v1/stats/usage?range=all&group_by=task_type"),
    Target("tasks_list", "GET", "/api/v1/agents/tasks?limit=20&offset=0"),
    Target("audit_page", "GET", "/api/v1/auth/audit-log?page=1&page_size=20"),
    Target("workflows_list", "GET", "/api/v1/workflows"),    # 六条里最轻的 app 行（小表整读，spec §5.2:141）
    Target("evals_runs_list", "GET", "/api/v1/evaluations/runs"),
)
FLOOR_TARGET = Target("floor_nginx", "GET", "/healthz")        # nginx 静态：零转发零 DB 的参照行
SUBMIT_TARGET = Target("submit_202", "POST", "/api/v1/agents/tasks", money=True)


# 限流窗口的口径数据化（R24）：summarise_probe 从前收一个「看着像参与计算其实没参与」的
# wait_s 形参——滑窗下的期望值是 limit+1 这个不变量，与静置秒数无关，所以那个形参是死参数。
# 现在改由下面这两枚常量把「静置必须盖满一个窗口」这件事写成数据：WINDOW_S 抄自
# app/core/rate_limit.py:46 的 DEFAULT_WINDOW_S = 60；探针默认静置 SETTLE_S = 61.0 必须严格
# 大于窗口，否则睡不空上一段残留、量到的是窗口相位而不是限流值（该不等式由探针单测那根针钉住）。
WINDOW_S = 60      # core/rate_limit.py:46 DEFAULT_WINDOW_S（三档全是 per-min 口径）
SETTLE_S = 61.0    # run_probe 默认静置；前提 SETTLE_S > WINDOW_S


# ---- 统计纯函数 ----


def percentile(values: list[float], p: float) -> float:
    """最近秩（nearest-rank）。样本上千时与线性插值差不到一个桶，但口径可复述。"""
    if not values:
        raise ValueError("percentile 不接受空样本（空 = 一个请求都没发出去，那是配置错）")
    s = sorted(values)
    k = max(0, min(len(s) - 1, math.ceil(p / 100.0 * len(s)) - 1))
    return float(s[k])


def summarise(timings_ms: list[float], statuses: list[int], exc_count: int,
              duration_s: float) -> dict:
    """ok 只算 2xx：429 与 5xx 都不算成率，但各自单列一栏（spec §5.4 表头）。

    两处「不许把没量到伪装成量到了」的口径：
      * 空样本的百分位是 None，不是 0.0ms。最想看的那一行恰恰是全超时的饱和档（n>0、ok=0、
        exc=n），那里印 p50=0.0ms 就是一张「看起来有数」的假表——spec §8 说这种脚本比没有
        压测更糟，`percentile` 自己也因此对空样本 raise。`measured` 一列顺手交代真有几个样本。
      * `codes` 直方图 + `http_other`：404（双前缀事故）或 403（audit_page 撞上非 admin）
        过去只呈现成 n>0 / ok=0，看不出原因。markdown 的 12 列被 spec §5.4 冻住，所以这两样
        只进 JSON，位置同 `success_rate`。
    """
    ok = sum(1 for c in statuses if 200 <= c < 300)
    total = len(statuses) + exc_count
    codes: dict[str, int] = {}
    for c in statuses:
        codes[str(c)] = codes.get(str(c), 0) + 1
    return {
        "n": total,
        "ok": ok,
        "http_429": sum(1 for c in statuses if c == 429),
        "http_5xx": sum(1 for c in statuses if c >= 500),
        "http_other": sum(1 for c in statuses
                          if not 200 <= c < 300 and c != 429 and c < 500),
        "exc": exc_count,
        "qps": round(total / duration_s, 2) if duration_s > 0 else 0.0,
        "measured": len(timings_ms),
        "codes": codes,
        "p50": round(percentile(timings_ms, 50), 2) if timings_ms else None,
        "p95": round(percentile(timings_ms, 95), 2) if timings_ms else None,
        "p99": round(percentile(timings_ms, 99), 2) if timings_ms else None,
        "max": round(max(timings_ms), 2) if timings_ms else None,
        "success_rate": round(ok / total, 4) if total else 0.0,
    }


def render_markdown(rows: list[dict]) -> str:
    head = "| target | level | n | ok | http_429 | http_5xx | exc | qps | p50 | p95 | p99 | max |"
    sep = "|" + "---|" * 12

    def cell(v: object) -> object:
        # 没量到样本的百分位是 None，这里渲染成 ASCII 的 '-'。不用 em dash：控制台是 GBK，
        # 非 ASCII 分隔符会在报告里变成 '?' 或直接把 print 炸掉（RK-c8）。
        return "-" if v is None else v

    out = [head, sep]
    for r in rows:
        cells = [cell(r[k]) for k in ("target", "level", "n", "ok", "http_429", "http_5xx",
                                      "exc", "qps", "p50", "p95", "p99", "max")]
        out.append("| " + " | ".join(str(c) for c in cells) + " |")
    return "\n".join(out) + "\n"


def summarise_probe(codes: list[int], *, limit_per_min: int,
                    retry_after: str = "", code: str = "",
                    code_expect: str = "COMMON_429001", exc: int = 0) -> dict:
    """限流边界探针的口径：第几个请求撞上 429、错误码是不是预期那枚、与滑窗推算的是第几个。

    窗口是**滑动**窗口（core/rate_limit.py:49-73：一次 pipeline 先 `zremrangebyscore(key, 0,
    now - window_s)` 把窗外旧条目扫掉，再 zadd/zcard 判定；该函数 docstring `:51` 自己写的
    就是「滑窗判定」），不是固定窗口。所以期望值是 `limit_per_min + 1` 这个不变量，与静置
    秒数无关——静置只是「起测前把窗睡空」这个前提，由模块常量 SETTLE_S > WINDOW_S 写成数据
    （旧实现把这前提做成了一个 wait_s 形参还拿 wait_s/60 乘速率，limit=20、wait=61 时凑巧得
    21，limit=60 会算出 62、limit=600 会算出 610，全都是假的 miss；那个不参与任何计算的形参
    已在 R24 删掉）。

    为什么滑窗下「静置 > WINDOW_S ⇒ 第 limit+1 个才是第一个 429」仍然成立：静置期间一枚请求都
    不发，探针自己这批的进站时刻全落在随后这几十秒内，窗口（WINDOW_S=60）滑到第 limit+1 枚时
    窗内已攒满 limit 条，于是它被判定超限；前面的每一枚都还在窗内凑数、全部放行。反过来静置
    不足才会出问题：上一段的残留把窗提前填满，第一个 429 落在更早的位置，量到的是窗口相位而不是
    限流值——run_probe 用 settle_s=SETTLE_S 保证这件事。

    `code_match` 与边界的 `match` 是两件事，各答 spec §5.3 的一条：前者答「响应体里的 code
    是不是 COMMON_429001」，后者答「第一个 429 是不是落在第 limit+1 个」。code 由调用方**观测**
    而来（见 _one 的返回值），这里只比较，不代填。
    """
    first = next((i + 1 for i, c in enumerate(codes) if c == 429), None)
    expected = limit_per_min + 1
    return {
        "requests_sent": len(codes) + exc,
        "first_429_index": first,
        "expected_first_429_at": expected,
        "match": first == expected,
        "retry_after": retry_after,
        "code": code,
        "code_match": bool(code) and code == code_expect,
        "exc": exc,
    }


# ---- 证据解析（纯函数，形状由 Task 1/2 的产物决定） ----


def parse_census(text: str) -> dict:
    """普查文本 → {counts, fingerprint, dropped}。

    `dropped` 是「带 `=` 但值不是纯数字」的行，原样留着：截断过的 census 以前会静默变成
    一个更小的 counts，读数的人看不出来少了哪张表——那正是 spec §5.4 要 census 的理由。
    """
    counts: dict[str, int] = {}
    dropped: list[str] = []
    fingerprint = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("FINGERPRINT="):
            fingerprint = line.split("=", 1)[1]
        elif "=" in line:
            k, _, v = line.rpartition("=")
            if v.isdigit():
                counts[k] = int(v)
            else:
                dropped.append(line)
    return {"counts": counts, "fingerprint": fingerprint, "dropped": dropped}


def parse_receipts(text: str) -> dict:
    keys = ("down_v_rc", "port_check_rc", "home_census_diff_rc")
    out: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        for key in keys:
            if line.startswith(key + "="):
                toks = line.split("=", 1)[1].split()
                if not toks:
                    # 键在、值空（`port_check_rc=`）是损坏行：抛带键名的 ValueError，绝不抛裸
                    # IndexError——后者会绕过 classify_receipts 的 except ValueError，让损坏文件
                    # 与「还没写」在 main 里读起来一模一样（R23 要拆穿的正是这层混淆）。
                    raise ValueError(f"拆栈收据 {key} 有空值（receipts.txt 这一行被截断或写坏了）")
                tok = toks[0]
                out[key] = int(tok) if tok.lstrip("-").isdigit() else tok
    missing = set(keys) - set(out)
    if missing:
        raise ValueError(f"拆栈收据缺键 {sorted(missing)}（receipts.txt 的形状变了，docs/12 的表就不能读）")
    return out


# 收据占位串的两个兜底分支值（R23）：说 not_written_at_run_time 而不是 from_previous_lifecycle——
# 「文件验证为空」那一分支根本没有上一次可借，旧值是一张写进 JSON 的谎，会和 receipts_source
# 自相矛盾。三键齐全，所以 build_context 的 parse_receipts 守卫照样过。
PLACEHOLDER_RECEIPTS = ("down_v_rc=not_written_at_run_time\n"
                        "port_check_rc=not_written_at_run_time\n"
                        "home_census_diff_rc=not_written_at_run_time\n")


def classify_receipts(*, path_given: bool, path_text: str | None) -> tuple[str, str]:
    """把「有没有给 --receipts-file」与「读到了什么」分成五条互斥来路。

    为什么必须是独立纯函数（R23 的核心）：main 从前对 parse_receipts 的任何 ValueError 一律
    贴 "this_lifecycle_receipts_not_yet_written"——那是它从没验过的出处声明，损坏的文件和还没
    写出的文件因此读起来没差别，而 parse_receipts 还会对 `key=`（空值）漏一枚裸 IndexError，
    except ValueError 抓不住。真实时序里三种状态都得活着区分（一次 run 烧掉整个 demo 栈，
    「非空但缺三键」是合法的运行中状态，绝不能中断它）：
      * docker/run_on_demo_stack.sh:75 把 receipts.txt 截断；
      * 三个键 down_v/port/home_census_diff 要到 :383/:386/:393 才写，即内层命令（我们自己）
        返回之后；
      * 计划 Task 4 Step 2 的 P6_STOP_WORKER 块（尚未落进脚本）会在内层命令之前往 receipts.txt
        追加若干非键行。
    五条分支（复审计 Minor 2：「给了路径但文件不在」与「压根没给路径」也不能共用一句来路——
    那是同一个「出处被假设而不是被验出来」的毛病，只是范围更小）：
      * 没给路径 → 占位 + `no_receipts_file_passed`；
      * 给了路径而文件不存在 → 占位 + `path_given_but_file_absent`；
      * 文件 strip 后为空 → 占位 + 「验证为空 = 本次还没走到拆栈」，**唯一**能说 not_yet_written 的分支；
      * 非空但被 parse_receipts 拒 → 占位 + 报出看到的非空行数（让读者分清拆栈前的杂行与形状变了）；
      * 非空且通过 → 原文 + `parsed_from_receipts_file`。
    """
    if not path_given:
        return PLACEHOLDER_RECEIPTS, "no_receipts_file_passed"
    if path_text is None:
        return PLACEHOLDER_RECEIPTS, "path_given_but_file_absent"
    if not path_text.strip():
        return PLACEHOLDER_RECEIPTS, "receipts_file_empty_not_yet_written_this_lifecycle"
    try:
        parse_receipts(path_text)
    except ValueError:
        seen = sum(1 for line in path_text.splitlines() if line.strip())
        return PLACEHOLDER_RECEIPTS, (
            f"receipts_file_has_{seen}_non_empty_lines_without_the_three_keys")
    return path_text, "parsed_from_receipts_file"


def build_context(*, stack: str, census_text: str, receipts_text: str, rl: str,
                  pool: str, git_head: str, worker_state: str,
                  worker_max_jobs: str = "", ports: str = "",
                  receipts_source: str = "") -> dict:
    """报告 JSON 的上下文块（spec §5.4）。缺任一项直接 raise：

    一张没有上下文的表在半年后就是无法证伪的噪音——这正是 spec §10 RK-c5 要防的事。
    `limits` 块要 `worker_max_jobs`、`stack` 块要四个宿主端口，都从 CLI 传进来（Task 4 的计划
    禁止「每个数字都从工件原文抄」之外的手抄，所以这里宁可拒产表）。

    `receipts_source` 交代 receipts 那三个值的来路（classify_receipts 判出的五种之一：没给路径 /
    给了但文件不在 / 验证为空所以本次还没写到拆栈 / 非空但缺三键 / 齐三键的原文），这样占位串
    不会被后来人当成观测到的 rc 读。
    """
    if not census_text.strip():
        raise ValueError("缺数据普查（census）：没有它，qps 不知道是对着多少行数据的库测出来的")
    if not receipts_text.strip():
        raise ValueError("缺拆栈收据（receipts）：没有它，这份表不知道是哪个生命周期产的")
    if not worker_max_jobs.strip():
        raise ValueError("缺 worker_max_jobs：202 档的队列深度没有它就无法归因（spec §5.4 的 limits 块）")
    if not ports.strip():
        raise ValueError("缺四个宿主端口：没有它，读者不知道这组数打在哪个端口组合上（spec §5.4 的 stack 块）")
    if not receipts_source.strip():
        raise ValueError("缺 receipts_source：收据来路必须可见，否则占位串会被读成观测值")
    return {
        "stack": stack,
        "host_ports": ports,
        "census": parse_census(census_text),
        "rate_limits": {"task_per_min": rl, "worker_max_jobs": worker_max_jobs},
        "worker_state": worker_state,
        "pool": pool,
        "git_head": git_head,
        "receipts": parse_receipts(receipts_text),
        "receipts_source": receipts_source,
    }


# ---- 客户端 ----


def login(origin: str, email: str, password: str) -> str:
    """取一个 admin token。**token 只返回、永不打印**（全局凭据禁令）。

    origin 在这里也 rstrip 一次：它接的是 `args.origin`（不是已 rstrip 的 base），P6_ORIGIN
    带尾斜杠时会拼出 `http://host//api/v1/auth/login`。两条路径各自归自己的入口，不共用字符串。
    """
    r = httpx.post(f"{origin.rstrip('/')}/api/v1/auth/login",
                   json={"email": email, "password": password}, timeout=30.0)
    if r.status_code != 200:
        raise RuntimeError(f"login failed: {r.status_code} code={r.json().get('code', '')}")
    return r.json()["access_token"]


async def _one(client: httpx.AsyncClient, base: str, t: Target, headers: dict,
               body: dict | None) -> tuple[float, int, str, str]:
    """发一枚请求，返回 (毫秒, 状态码, Retry-After, 响应体里的 code)。

    code 从已经读进内存的 body 里解析（`aread()` 之后取字节，不再有 IO 代价），解析不出
    JSON 对象或没有 code 键时给空串——探针要的正是「响应原文里写了什么」，不是我们的预设。
    """
    started = time.monotonic()
    r = await client.request(t.method, base + t.path, headers=headers,
                             json=body if t.method == "POST" else None)
    content = await r.aread()
    elapsed_ms = (time.monotonic() - started) * 1000.0
    try:
        parsed = json.loads(content.decode("utf-8", "replace"))
    except ValueError:
        parsed = None
    code = str(parsed.get("code", "")) if isinstance(parsed, dict) else ""
    return elapsed_ms, r.status_code, r.headers.get("Retry-After", ""), code


async def run_target(client, base, t: Target, headers: dict, *, concurrency: int,
                     duration_s: float, warmup_s: float, body: dict | None = None) -> dict:
    """closed-loop：起 concurrency 个协程，各自「发一个 → 读完整 → 睡 100ms」直到时长耗尽。

    warmup 期的样本整段丢弃（不计时、不计状态码、也不计异常）。

    qps 的方向要说清（docs/12 抄的就是这句）：`measure` 是在**发请求之前**判定的，所以窗口
    末尾起跑、在窗口之外才返回的那批在途请求也计入 `n`，而分母仍是配置的 `duration_s` ⇒
    这一列**轻微高估**（不是低估）。样本上千时误差在 1/K 量级，但方向写反的注释会教错人。

    错误路径必须走完闭环：失败也要睡满 100ms 思考时间。少了这一睡，一枚连不上的面会让 K 个
    协程空转刷满整个窗口——exc 无上限、并发模型不再是你设计的 closed-loop，还会把一次性 demo
    栈打成自伤。
    """
    timings: list[float] = []
    statuses: list[int] = []
    exc = 0
    deadline = time.monotonic() + duration_s + warmup_s

    async def worker():
        nonlocal exc
        while time.monotonic() < deadline:
            measure = time.monotonic() >= deadline - duration_s
            try:
                ms, status, _ra, _ec = await _one(client, base, t, headers, body)
                if measure:
                    timings.append(ms)
                    statuses.append(status)   # _one 的第二槽是状态码（R29 改名，别再叫 code 让人以为塞错了桶）
            except Exception:
                if measure:
                    exc += 1
            await asyncio.sleep(0.1)

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return {"target": t.key, "level": f"c{concurrency}",
            **summarise(timings, statuses, exc, duration_s)}


async def run_levels(client, base, targets, headers, levels, *, duration_s, warmup_s,
                     submit_body: dict | None = None) -> list[dict]:
    rows: list[dict] = []
    for t in targets:
        for c in levels:
            rows.append(await run_target(client, base, t, headers, concurrency=c,
                                         duration_s=duration_s, warmup_s=warmup_s,
                                         body=submit_body if t is SUBMIT_TARGET else None))
    return rows


async def run_probe(client, base, headers, *, limit_per_min: int, n: int,
                    body: dict, settle_s: float = SETTLE_S) -> dict:
    """限流边界档：先把滑窗睡空，再串行发最多 n 个请求，量第一次 429 落在第几个、错误码是什么。

    `settle_s=SETTLE_S`（61.0）不是保守，是口径（core/rate_limit.py 是滑窗，见 summarise_probe）：
    静置期间一枚都不发，WINDOW_S=60 的窗被 `zremrangebyscore` 扫空，第一个 429 才一定落在第
    limit+1 个。睡得不够 WINDOW_S 才会串味——上一段留下的进站把窗提前凑满，那时第 21 个不红、
    第 9 个反而红，量到的是窗口相位而不是限流值。前提 SETTLE_S > WINDOW_S 由模块常量与探针单测
    那根针共同守着（R24：这件事写成数据，不再靠一个不参与计算的 wait_s 形参）。

    传输层失败（连不上、Redis 宕导致的 503 之后的异常）单独记成 `exc`，不再往 codes 里塞伪
    状态码 0：那会占掉一个位置，把「限流器根本没响」和「客户端发不出去」读成同一张表。
    """
    await asyncio.sleep(settle_s)
    codes: list[int] = []
    exc = 0
    retry_after = ""
    err_code = ""
    for _ in range(n):
        try:
            _ms, status, ra, resp_code = await _one(client, base, SUBMIT_TARGET, headers, body)
        except Exception:
            exc += 1
            # 节流（R28）：成功路径自带一次真 HTTP 往返，节奏由服务器决定；异常路径不然——容器
            # 连不上时 --probe-n 一调大就会 hot-spin 打空一个死容器。补一个短睡把失败也变成闭环。
            await asyncio.sleep(0.05)
            continue
        codes.append(status)
        if status == 429:
            retry_after = ra
            err_code = resp_code   # 观测值：spec §5.3 问的就是响应体里的 code 是不是 COMMON_429001
            break
    return summarise_probe(codes, limit_per_min=limit_per_min,
                           retry_after=retry_after, code=err_code, exc=exc)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 11c 离线压测器（只允许在一次性 demo 栈上跑）")
    p.add_argument("--origin", default=os.environ.get("P6_ORIGIN", ""), help="如 http://127.0.0.1:8081")
    p.add_argument("--mode", choices=("read", "submit", "probe"), default="read")
    p.add_argument("--levels", default="1,8,32,64", help="逗号分隔的并发数")
    p.add_argument("--duration", type=float, default=15.0, help="每个 (target,level) 的测量秒数")
    p.add_argument("--warmup", type=float, default=2.0)
    p.add_argument("--probe-n", type=int, default=25, help="边界档最多发这么多，别把窗口烧穿")
    p.add_argument("--rl", default="20", help="报告用：本容器 task 桶的限流值")
    p.add_argument("--worker-state", default="stopped", choices=("stopped", "running"))
    p.add_argument("--census-file", default="", help="$ART_DIR/census-before.txt")
    p.add_argument("--receipts-file", default="",
                   help="$ART_DIR/receipts.txt（本次拆栈前必然还没写完；实际来路记进 context.receipts_source）")
    p.add_argument("--pool", default="15", help="asyncpg 适配池 5+10（docs/02 §9）")
    p.add_argument("--worker-max-jobs", default="",
                   help="报告用：本容器 worker 的 max_jobs（spec §5.4 的 limits 块，空则拒绝产表）")
    p.add_argument("--ports", default="",
                   help="报告用：四个宿主端口，逗号分隔如 5543,5637,8110,8081（spec §5.4 的 stack 块）")
    p.add_argument("--git-head", default="")
    p.add_argument("--stack", default="p6demo")
    p.add_argument("--out", required=True, help="输出目录（Markdown + JSON 同名双件）")
    return p.parse_args(argv)


async def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.origin:
        print("缺 --origin（或 P6_ORIGIN）", file=sys.stderr)
        return 2
    # base 只到 origin：Target.path 自带 /api/v1（floor 行必须留在根，见模块 docstring）。
    base = args.origin.rstrip("/")
    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    # spec §5.1 要求连接复用。默认 httpx 只留 20 条 keep-alive，c32/c64 那两档就会有一半样本
    # 付 TCP/连接建立的钱——floor 行与被测行不再是同一套客户端成本，spec §5.2 的
    # 「workflows_list − floor_nginx ≈ app 那一跳」当场失效。上限取本 run 的最大并发。
    # 不把它垫到 >=2（R25，控制器驳回）：闭环里每枚协程同时只有一发在途，--levels 1 时单连接池
    # 不构成约束；服务端主动关连接是被测系统的性质，不是要在客户端抹平的假象。
    max_concurrency = max(levels, default=1) if args.mode != "probe" else 1

    # ---- 取证先于 traffic（一次生命周期烧不起第二次）----
    # 计划 Task 4 Step 2/4/5 的真跑法会把 --receipts-file 指到 $ART_DIR/receipts.txt，而这个文件
    # 在 docker/run_on_demo_stack.sh:75 被截断、三个键要到 :383/:386/:393 才写——也就是内层
    # 命令（就是我们自己）返回之后。所以内层命令里读到的永远是空文件或上半程的杂行。
    # 结论：普查/收据/上下文全部在 login 与任何请求之前解析并校验，跑完只写工件。
    census_text = Path(args.census_file).read_text(encoding="utf-8") if args.census_file else ""
    # 收据来路交给纯函数 classify_receipts（R23）：main 只交出「旗标有没有给」与「读到什么（没有
    # 就是 None）」这两件原料，五条分支（没给 / 给了但文件不在 / 验证为空 / 非空缺三键 / 齐三键）
    # 与占位串全在那里面判，出处是被验出来的而不是被假设的。坏值守卫也在里面，早于任何请求就响。
    receipts_path = Path(args.receipts_file) if args.receipts_file else None
    receipts_text, receipts_source = classify_receipts(
        path_given=bool(args.receipts_file),
        path_text=receipts_path.read_text(encoding="utf-8")
        if receipts_path is not None and receipts_path.exists() else None)
    # 时序（计划开头裁定第 5 条）：本次的 down_v/port/diff 要等脚本拆完栈才存在，而 :75 每次运行
    # 开头就把这个文件重开——所以内层跑着的时候**不可能**拿到本次的三键收据。JSON 里能带的只有
    # classify_receipts 判出的那五种情形之一，具体哪一种由 context.receipts_source 具名交代；
    # 本次的真收据以拆栈后的 $ART_DIR/receipts.txt 原文为准，docs/12 直接引那份原文，
    # PORT_RELEASED 那几行本来就是拆栈之后才存在的，不属于这份 JSON。
    context = build_context(stack=args.stack, census_text=census_text,
                            receipts_text=receipts_text, rl=args.rl,
                            pool=args.pool, git_head=args.git_head,
                            worker_state=args.worker_state,
                            worker_max_jobs=args.worker_max_jobs, ports=args.ports,
                            receipts_source=receipts_source)

    token = login(args.origin, os.environ["P6_DEMO_EMAIL"], os.environ["P6_DEMO_PASSWORD"])
    headers = {"Authorization": f"Bearer {token}"}
    submit_body = {"question": os.environ.get(
        "P6_SUBMIT_TEXT", "压测占位问题：用一句话说明本月销售趋势"), "task_type": "agent_analysis"}

    probe = None
    rows: list[dict] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=False,
                                 limits=httpx.Limits(max_connections=max_concurrency,
                                                     max_keepalive_connections=max_concurrency)) as client:
        # follow_redirects=False 是显式表态而非依赖默认（httpx 默认也 False）：一旦跟随重定向，
        # 302→200 会把两跳的耗时塞进同一枚样本、还报成一次 ok——R26 要堵的就是这条缝。summarise
        # 侧已把 3xx 归进 http_other（见测试），两头夹住才叫「3xx 不可能被吞」。
        if args.mode == "probe":
            probe = await run_probe(client, base, headers, limit_per_min=int(args.rl),
                                    n=args.probe_n, body=submit_body)
        else:
            targets = [FLOOR_TARGET, *READ_TARGETS] if args.mode == "read" else [SUBMIT_TARGET]
            rows = await run_levels(client, base, targets, headers, levels,
                                    duration_s=args.duration, warmup_s=args.warmup,
                                    submit_body=submit_body)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    md = [f"# Load test {args.mode} {stamp}", "",
          f"- stack `{args.stack}` / origin `{args.origin}` / worker `{args.worker_state}`"
          f" / task 桶 `{args.rl}` 每分钟 / 适配池 `{args.pool}` / HEAD `{args.git_head}`", ""]
    if rows:
        md += [render_markdown(rows)]
    if probe is not None:
        md += ["## 限流边界探针", "", "```json",
               json.dumps(probe, indent=2, ensure_ascii=False), "```", ""]
    (out_dir / f"loadtest-{args.mode}-{stamp}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    payload = {"generated_at": stamp, "mode": args.mode, "origin": args.origin,
               "levels": levels, "rows": rows, "probe": probe, "context": context}
    (out_dir / f"loadtest-{args.mode}-{stamp}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
