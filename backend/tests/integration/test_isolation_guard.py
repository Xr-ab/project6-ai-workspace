"""隔离守卫自己的针（spec §6 硬闸 / §12 RK8：误连真库不是测试失败，是数据事故）。

**离线**（不打 db / redis marker）：守卫是纯字符串判据，一次都不连接，所以它必须
能被 `pytest -m "not db and not redis"` 跑到 —— 那正是「本机没起 p6test 就误跑全量」
这一次最需要响的信号。红侧的形状见计划正文的裁定：不真连 5432，而是断言判据存在。

conftest 里三个判据函数收 url 作**参数**而不是读 `settings`：读 settings 的时机是
import 期，判据就没法在离线层被逐条喂 hostile URL 了。夹具负责喂。

**runner 上的假红陷阱（终评实测抓出）**：`_ci_runner()` 认 `GITHUB_ACTIONS == "true"`
就整体放行（裁定 R-CI：runner 的 postgres 是每次新建的一次性容器）。这条豁免是对的，
但它会让本文件七枚「必须拒」的针在 CI 上全部 `DID NOT RAISE` —— 而 runner 一定会注入
那个变量。所以本模块用 autouse 夹具先把豁免面擦掉，让「拒得下来」这件事在**本机与 CI
同一种颜色**；豁免本身由 `test_ci_runner_is_exempt` / `test_only_the_literal_true_bypasses`
两枚针各自显式 setenv 来钉，不靠环境碰巧干净。

**夹具自己也有针**（终评复审计 Important-1）：见文件末 `test_ci_env_injection_cannot_redden_this_file`。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _pytest.outcomes import Exit  # pytest.exit 抛的就是它；私有路径但 8.x 稳定

from tests.conftest import (
    UnsafeTarget,
    assert_isolated_database,
    assert_isolated_redis,
    require_isolated_for_markers,
    require_isolated_targets,
)


@pytest.fixture(autouse=True)
def _no_ci_exemption(monkeypatch):
    """守卫针必须在「无豁免」下判：runner 注入 GITHUB_ACTIONS=true 时，
    不复位就会让七枚拒止针集体变成「DID NOT RAISE」的假红（红侧工件
    `.superpowers/sdd/2026-10-01-phase11b-tests-and-ci/frx-guard-cienv-red.txt`）。"""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


# 本机默认项目的真库真 redis（compose 的 :- 默认值就是它们）
HOSTILE_PG = "postgresql+asyncpg://app:app_dev_pwd@localhost:5432/ai_workspace"
HOSTILE_REDIS = "redis://localhost:6379/0"
# 一次性项目 p6test 的地址（docker/integration.env 的端口）
OK_PG = "postgresql+asyncpg://app:app_dev_pwd@localhost:5433/ai_workspace"
OK_REDIS = "redis://localhost:6380/0"
# 不写端口 = 驱动回落默认端口，正是要拦的那一种形状（不是"看起来不一样就放过"）
HOSTILE_PG_NO_PORT = "postgresql+asyncpg://app:app_dev_pwd@localhost/ai_workspace"
HOSTILE_REDIS_NO_PORT = "redis://localhost/0"
# Phase 10 的外部库存域：同 host 同端口，只换库名，也要挡
HOSTILE_ENTERPRISE = "postgresql+asyncpg://app:app_dev_pwd@localhost:5433/enterprise_data"

# 子进程自跑的坐标：cwd 必须是 backend（pytest.ini 在那儿），哨兵变量只用来防递归
BACKEND_ROOT = Path(__file__).resolve().parents[2]
_CI_SUBPROCESS_SENTINEL = "P6_GUARD_SUBPROCESS"


def test_default_pg_port_is_refused():
    with pytest.raises(UnsafeTarget, match="5432"):
        assert_isolated_database(HOSTILE_PG)


def test_pg_url_without_port_is_refused():
    with pytest.raises(UnsafeTarget):
        assert_isolated_database(HOSTILE_PG_NO_PORT)


def test_enterprise_database_is_refused_even_on_the_test_port():
    with pytest.raises(UnsafeTarget, match="enterprise_data"):
        assert_isolated_database(HOSTILE_ENTERPRISE)


def test_disposable_pg_port_passes():
    assert assert_isolated_database(OK_PG) is None


def test_default_redis_port_is_refused():
    with pytest.raises(UnsafeTarget, match="6379"):
        assert_isolated_redis(HOSTILE_REDIS)


def test_redis_url_without_port_is_refused():
    with pytest.raises(UnsafeTarget):
        assert_isolated_redis(HOSTILE_REDIS_NO_PORT)


def test_disposable_redis_port_passes():
    assert assert_isolated_redis(OK_REDIS) is None


def test_guard_message_names_the_script():
    """文案必须给出路：只说"不行"的守卫会在半夜被人绕过。"""
    with pytest.raises(UnsafeTarget, match="run_integration_local"):
        assert_isolated_database(HOSTILE_PG)


def test_guard_never_opens_a_socket():
    """判据不得连接：192.0.2.1 是 RFC 5737 TEST-NET-1，路由不到任何服务。
    端口用非默认的 5433 ⇒ 判据应当放行；若它偷偷连过一次，这条会挂十几秒后抛连接错。"""
    assert assert_isolated_database(
        "postgresql+asyncpg://app:app_dev_pwd@192.0.2.1:5433/ai_workspace"
    ) is None


def test_ci_runner_is_exempt(monkeypatch):
    """豁免面（裁定 R-CI）：GitHub 的 service container 就挂在 localhost:5432，
    守的却是每次新建、随 runner 销毁的一次性库 —— 事故前提不成立。"""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert assert_isolated_database(HOSTILE_PG) is None
    assert assert_isolated_redis(HOSTILE_REDIS) is None


def test_only_the_literal_true_bypasses(monkeypatch):
    """不自造豁免面：判据只认 runner 实际注入的字面 "true"。"""
    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    with pytest.raises(UnsafeTarget):
        assert_isolated_database(HOSTILE_PG)


def test_require_turns_refusal_into_session_wide_exit():
    """夹具入口的形状：UnsafeTarget → pytest.exit（整轮停，不是逐条红）。"""
    with pytest.raises(Exit) as excinfo:
        require_isolated_targets(HOSTILE_PG, OK_REDIS)
    # pytest 9.1.1 的 Exit 把退出码存在 .returncode（_pytest/outcomes.py:72），
    # 不是简报原文写的 .code —— 文档事实被实测推翻，按实测实现（见 task-3-report 关注点）。
    assert excinfo.value.returncode == 7
    require_isolated_targets(OK_PG, OK_REDIS)  # 两个都干净 ⇒ 不抛

    # marker 门控的 autouse 闸（_isolation_gate 走的就是 require_isolated_for_markers）：
    # 逐 marker 证明它真的会落，且只判被标的那一侧 —— 不走 db_session 的真 socket 面
    # （ensure_setup / alembic 子进程 / get_redis）全靠这一层兜底。
    # db marker + 脏 DB URL ⇒ 拒（db 腿）。
    with pytest.raises(Exit) as db_leg:
        require_isolated_for_markers(HOSTILE_PG, OK_REDIS, db=True, redis=False)
    assert db_leg.value.returncode == 7
    # redis marker + 脏 redis URL ⇒ 拒（补齐夹具链从没判过的 redis 腿）。
    with pytest.raises(Exit) as redis_leg:
        require_isolated_for_markers(OK_PG, HOSTILE_REDIS, db=False, redis=True)
    assert redis_leg.value.returncode == 7
    # 只标 db 时脏 redis URL 不该误伤（marker 门控：不判未标的一侧）——两侧对称的证据。
    require_isolated_for_markers(OK_PG, HOSTILE_REDIS, db=True, redis=False)
    require_isolated_for_markers(HOSTILE_PG, HOSTILE_REDIS, db=False, redis=False)  # 都没标 ⇒ 放行


def test_ci_env_injection_cannot_redden_this_file():
    """钉的是**夹具本身**，不是守卫行为（终评复审计 Important-1）。

    为什么只能开子进程：本机环境天生没有 `GITHUB_ACTIONS`，所以「删掉
    `_no_ci_exemption`」这件事在任何默认命令下都不会红——七枚拒止针照样绿。
    而 runner 一定会注入那个变量，唯一的探测器本来是「跑一次 CI」，可 CI 至今
    没跑过（`docs/09-roadmap.md` F 表 #26）。这条针把 CI 环境搬进本机，让缺陷的
    复现不再依赖有没有 runner：灌进环境变量重跑本文件，仍须全绿。

    rc==0 单独就够：pytest 的退出码里 1=有失败、5=没收到用例，所以「绿」必然
    意味着子进程真的把这批针跑了一遍。哨兵变量防递归——子进程里这条直接返回，
    其余各条照跑。
    """
    if os.environ.get(_CI_SUBPROCESS_SENTINEL):
        return
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__)), "-q"],
        cwd=str(BACKEND_ROOT),
        env={**os.environ, "GITHUB_ACTIONS": "true", _CI_SUBPROCESS_SENTINEL: "1"},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=100,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
