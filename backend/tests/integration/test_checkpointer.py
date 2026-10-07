"""Checkpointer 的集成面（Phase 7 T3 的轴 + §8 的新针由 Task 4 追加在这个文件里）。

conftest 顶部的 win32 Selector 策略切换是这些用例能跑的前提：
checkpointer._require_supported_loop() 在 Proactor 上直接抛 RuntimeError
（psycopg 3 拒它），pytest-asyncio 默认不切策略。
"""
import uuid

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import text

from app.ai.graph import checkpointer

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
async def clean_singletons():
    """每条用例前后都复位模块级单例。

    为什么必须：_pool/_saver 是模块级的，而 pytest-asyncio 每条用例一个新 loop。
    不复位 ⇒ 第二条用例会拿到绑在**已关闭 loop** 上的池，报
    PoolClosed / "Event loop is closed" —— 那恰好是 #23 的症状，
    把它当成本仓的 bug 查一遍就亏了两小时。
    """
    await checkpointer.shutdown_checkpointer()
    yield
    await checkpointer.shutdown_checkpointer()


async def _purge_thread(thread_id: str) -> None:
    """按 thread_id 精确清掉本用例在 checkpoint 三张表里留下的行。

    走模块级 checkpointer._pool 而不是 saver 的某个私有属性：`_pool` 是
    ensure_setup() 之后一定在的模块单例（checkpointer.py:40），而
    AsyncPostgresSaver 暴露的连接属性名跨版本不稳（本版本挂 .conn，aio.py:57，
    简报正文写的 saver._pool 在本版本不通，按实施订正走 _pool）。
    抽成模块级 helper 而非就地内联：Task 3 的往返针与 Task 4 追加的 #23 用例删的是
    同一批行，两处内联删会重复且易漏——统一走这里。
    表名走字面量（就是 sweeper._CHECKPOINT_TABLES 那三个名字）、值走参数占位，无注入面；
    表名若漂移，DELETE 会直接抛 relation does not exist，红得可见。
    残余风险（Task 3 评审点名）：langgraph 哪天包装/替换了传给 AsyncPostgresSaver 的池，
    _pool 就不再是 saver 实际在用的对象，DELETE 会跑在一条活而闲置的池上而探针行悄悄存活
    ——所以删后逐表回查一次，有残留即失败：把「假设已清干净」换成「看得见的红」。
    _pool 为 None（没开过池）时直接跳过：没有池就没有本用例经手写入的行。
    """
    if checkpointer._pool is None:
        return
    async with checkpointer._pool.connection() as conn:
        for tbl in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            await conn.execute(f"DELETE FROM {tbl} WHERE thread_id = %s", (thread_id,))
            cur = await conn.execute(
                f"SELECT 1 FROM {tbl} WHERE thread_id = %s LIMIT 1", (thread_id,)
            )
            leftover = await cur.fetchone()
            assert leftover is None, (
                f"{tbl} 里仍留有 thread_id={thread_id} 的行——_pool 可能已不是 saver "
                "实际使用的池（langgraph 包/换池）或写入形状漂移；清理失效必须看得见，"
                "不许假设已清干净"
            )


async def test_ensure_setup_is_idempotent_and_saver_is_the_same_object():
    await checkpointer.ensure_setup()
    first = checkpointer.get_checkpointer()
    await checkpointer.ensure_setup()          # 锁内双检：第二次不该再开池
    assert checkpointer.get_checkpointer() is first


async def test_uninitialized_get_refuses_instead_of_falling_back():
    """不许静默回退 InMemorySaver（假安全：重启丢状态，而没人报错）。"""
    with pytest.raises(RuntimeError, match="ensure_setup"):
        checkpointer.get_checkpointer()


async def test_setup_created_the_three_checkpoint_tables(db_session):
    """saver.setup() 建的那三张表必须在，且 sweeper 扫④的 TTL 也认这三个名字。

    名字的唯一来源是 `app/workers/sweeper.py:62` 的 `_CHECKPOINT_TABLES`
    （checkpointer 模块里**没有**这个名字，别去那儿找）——所以这条针真正在证的是
    「saver.setup() 建的表 == sweeper 会去清的表」这个跨模块一致性：
    哪天 langgraph 换表名，sweeper 会静默扫一张不存在的表，而这里先红。
    """
    from app.workers import sweeper

    assert set(sweeper._CHECKPOINT_TABLES) == {
        "checkpoints", "checkpoint_blobs", "checkpoint_writes"}
    for tbl in sweeper._CHECKPOINT_TABLES:
        got = await db_session.scalar(
            text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"), {"t": tbl})
        assert got == 1, f"{tbl} 不存在 —— saver.setup() 没跑穿"


async def test_round_trip_writes_and_reads_a_real_checkpoint():
    """真读写往返：往一个随机 thread 写一个 checkpoint 再读回来。
    这条是"池真的活着"的最强证据 —— healthy 探针与 get_checkpointer() 非 None
    都不代表能读写（#23 那天就是 healthy 全绿、执行面全死）。"""
    await checkpointer.ensure_setup()
    saver = checkpointer.get_checkpointer()
    thread_id = f"p6test-{uuid.uuid4().hex}"
    try:
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        ckpt = empty_checkpoint()
        ckpt["id"] = f"1-{uuid.uuid4().hex[:12]}"
        await saver.aput(config, ckpt, {"step": 1, "source": "loop", "writes": None, "parents": {}}, {})

        found = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
        assert found is not None
        assert found.config["configurable"]["thread_id"] == thread_id
    finally:
        # 无条件收尾：断言红也不该把 p6test-<uuid> 探针行活到下一条用例（同库同表）
        await _purge_thread(thread_id)


async def test_shutdown_resets_singletons_and_fires_hooks():
    """关池必复位 + 必跑 hook：不复位则同进程 shutdown→ensure_setup 之后
    旧编译图仍绑死池（PoolClosed）；hook 是图缓存失效的唯一通道。"""
    fired: list[str] = []

    def hook() -> None:
        fired.append("once")

    # _shutdown_hooks 是模块级列表，shutdown_checkpointer 只遍历调用、从不清空
    # （见 checkpointer.py:145）。注册后必须在 finally 摘除，否则这个 hook 永久残留：
    # 之后每条用例的 clean_singletons teardown 都会再触发它（闭包抓住的 fired 早已换
    # 一轮），并被 Task 4 追加的用例继承成幽灵 hook。
    checkpointer.register_shutdown_hook(hook)
    try:
        await checkpointer.ensure_setup()
        await checkpointer.shutdown_checkpointer()
        assert fired == ["once"]
        with pytest.raises(RuntimeError, match="ensure_setup"):
            checkpointer.get_checkpointer()
    finally:
        if hook in checkpointer._shutdown_hooks:
            checkpointer._shutdown_hooks.remove(hook)


# ==================== #23 中毒环（Phase 11b T4，spec §8） ====================
# 这一段的三条针各有分工，别混：
#   test_23_discard_singletons_lets_a_new_loop_rebuild —— 回归针（修复前红 / 修复后绿）
#   test_23_discard_fires_hooks_and_never_raises       —— 契约针（discard 的两个硬属性）
#   test_23_dead_loop_pool_is_genuinely_unusable        —— 证据针（失效机制真的存在，两侧同绿）
#   另两枚退出码针见 Step 8（走 run_python 子进程）
# 清理复用文件已有的模块级 helper _purge_thread（按 thread_id 删同一批行，
# 两处各写一份就是逐字重复的逻辑块）。


def test_23_discard_singletons_lets_a_new_loop_rebuild(isolated_urls, migrated_schema):
    """#23 主针：死 loop 留下的单例被 discard_singletons() 切断之后，
    ensure_setup() 必须在**新 loop** 上重建一个可用的池，并真读写一个 checkpoint。

    同步用例（不加 async）：本针要自己管两条 loop，交给 pytest-asyncio 就拿不到
    「关掉第一条、再开第二条」这个现场。
    修复前它是红的，红在 `checkpointer.discard_singletons()` 这一行 ——
    AttributeError: module 'app.ai.graph.checkpointer' has no attribute 'discard_singletons'。
    那一刻 autouse 的 clean_singletons 收尾还会多报一个 teardown error
    （它去 await 那条死池的 close()），这是**预期的红形状**，不是第二件事，别去修夹具。
    """
    import asyncio

    old_lock = checkpointer._setup_lock
    loop_a = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop_a)
        loop_a.run_until_complete(checkpointer.ensure_setup())
        old_saver = checkpointer.get_checkpointer()
    finally:
        loop_a.close()                     # 中毒现场：单例还指着绑在已关 loop 上的池

    checkpointer.discard_singletons()      # ← 修复前红在这一行
    assert checkpointer._pool is None
    assert checkpointer._saver is None
    assert checkpointer._setup_lock is not old_lock, "锁没换：旧锁记着已死 loop 的 waiter/owner"
    with pytest.raises(RuntimeError, match="ensure_setup"):
        checkpointer.get_checkpointer()    # 复位必须彻底，不许留半条命

    loop_b = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop_b)
        loop_b.run_until_complete(_rebuild_and_round_trip(old_saver))
    finally:
        # 自己收干净再交回夹具：夹具的 shutdown_checkpointer() 看到 _pool is None 就是 no-op，
        # 不会去 await 那条它接手的死池。
        checkpointer.discard_singletons()
        asyncio.set_event_loop(None)
        loop_b.close()


async def _rebuild_and_round_trip(old_saver) -> None:
    """新 loop 上重开池 + 一次真写读往返（同一张表、同一个公共 helper，不复制形状）。"""
    await checkpointer.ensure_setup()
    saver = checkpointer.get_checkpointer()
    assert saver is not old_saver, "ensure_setup 幂等复用了死池 —— #23 没修好"

    thread_id = f"p6test-23-{uuid.uuid4().hex[:12]}"
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    ckpt = empty_checkpoint()
    ckpt["id"] = f"1-{uuid.uuid4().hex[:12]}"
    await saver.aput(
        config, ckpt, {"step": 1, "source": "p11b-t4", "writes": None, "parents": {}}, {}
    )
    found = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    assert found is not None
    assert found.config["configurable"]["thread_id"] == thread_id

    await _purge_thread(thread_id)
    await checkpointer.shutdown_checkpointer()   # 在它还活着的那条 loop 上关（B 尚未 close）


def test_23_discard_fires_hooks_and_never_raises():
    """discard 的两条硬契约：一定跑 **全部** hook（抛出的 hook 中止不了迭代、也压不掉
    排在它后面的 hook），且**绝不抛**。

    契约 2 不是洁癖：arq 的 on_shutdown 不可达正是「清理路径自己会抛」造出来的，
    如果 discard 也会抛，我们就只是把 bug 挪了一行。
    """
    events: list[str] = []

    def good() -> None:
        events.append("good")

    def bad() -> None:
        raise RuntimeError("hook 内部炸")

    def good2() -> None:
        events.append("good2")

    checkpointer.register_shutdown_hook(good)
    checkpointer.register_shutdown_hook(bad)
    checkpointer.register_shutdown_hook(good2)
    try:
        checkpointer.discard_singletons()          # bad 抛的那条只进日志，不外泄
        # 调用顺序即注册顺序，而 bad 之后的 good2 必须同样跑到 ⇒ 这条断言钉的是
        # 「抛出的 hook 既不中止迭代、也不压掉后续 hook」；实现若改成首个失败就
        # break（或让它外抛），events 会缺 good2，本针立刻红。
        assert events == ["good", "good2"]
        assert checkpointer._pool is None
        assert checkpointer._saver is None
    finally:
        # 摘掉自己登记的 hook：_shutdown_hooks 是模块级列表，留着会污染后面每条用例的
        # shutdown_checkpointer()（而 good/bad/good2 持有本用例的闭包，等于测试之间隔空握手）。
        for hook in (good, bad, good2):
            if hook in checkpointer._shutdown_hooks:
                checkpointer._shutdown_hooks.remove(hook)


# 子进程源码模板：证的是 _run_worker_guarded() 的「退出 vs 复活」策略，所以把 worker 装配
# 整个换成桩（sys.modules 注入），一步都不碰 jobs / document_service / embedding / llm ——
# 那些模块的 import 面里有模型与网络对象，把它们拉进一条离线针既慢又违反
# 「11b 不触发模型下载」的零钱裁定（spec §11）。
_GUARD_TEMPLATE = '''
import sys, time, types, atexit
if sys.platform == "win32":
    import asyncio
    pol = asyncio.get_event_loop_policy()
    if isinstance(pol, asyncio.WindowsProactorEventLoopPolicy):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

fake = types.ModuleType("app.workers.settings")
class WorkerSettings:
    pass
fake.WorkerSettings = WorkerSettings
sys.modules["app.workers.settings"] = fake

import arq
calls = []
def fake_run_worker(worker_settings):
    calls.append(1)
__AFTER_FIRST_CALL__
# 打在 arq 的模块属性上：守护是在**函数体内** `from arq import run_worker`
# （app/workers/__main__.py:43），所以调用期取到的就是被打桩后的这个名字。
arq.run_worker = fake_run_worker
atexit.register(lambda: print("CALLS", len(calls), flush=True))
# 退避不真睡（1s 起、倍增、封顶 30s）；打印实际请求睡多久，好让针断言退避算对了
time.sleep = lambda s: print("SKIPPED_SLEEP", s, flush=True)

import app.workers.__main__ as wmain
wmain._run_worker_guarded()
'''

# 填洞①：每次都抛 = 守护要么退避重启、要么按 flag 退出，复活不了
EXIT_SOURCE = _GUARD_TEMPLATE.replace(
    "__AFTER_FIRST_CALL__", '    raise ConnectionError("simulated redis blip (p11b T4)")'
)
# 填洞②：第二次不抛而是原地 SystemExit(0) = 复活这一步走到了可观测的位置。
# 用 SystemExit 而不是"正常返回"收尾：正常返回会被守护判成 arq 优雅收尾（crashed=False），
# 同样退 0 但语义不同，断言就分不清"复活了"和"没崩过"。
REVIVE_SOURCE = _GUARD_TEMPLATE.replace(
    "__AFTER_FIRST_CALL__",
    '    if len(calls) > 1:\n'
    '        print("REVIVED", len(calls), flush=True)\n'
    '        raise SystemExit(0)\n'
    '    raise ConnectionError("simulated redis blip (p11b T4)")',
)


def test_23_exit_on_fatal_makes_the_guard_process_exit(run_python):
    """修法 2 的正脸：WORKER_EXIT_ON_FATAL=1 时守护**不复活**，退出码 1。"""
    rc, out, err = run_python(EXIT_SOURCE, WORKER_EXIT_ON_FATAL="1", APP_ENV="development")
    assert rc == 1, (rc, out, err[-2000:])
    assert "CALLS 1" in out, out            # 一次都没复活
    assert "SKIPPED_SLEEP" not in out       # 退出优先于退避：不睡、不重试
    assert "worker exited unexpectedly" in err          # 崩溃照旧留痕（D-7 语义没丢）
    assert "WORKER_EXIT_ON_FATAL is set" in err         # 退出这件事本身也留痕


def test_23_without_the_flag_the_guard_revives_in_process(run_python):
    """双形态裁定的另一脸：显式置 0（本机 dev 的默认）⇒ 原地复活，不退进程。

    0 是显式传的而不是"不设"：`run_python` 的 cwd 是 backend/，而 Settings 读
    `env_file=".env"`——虽然本 Task 裁定这个键不进 `.env`，也不该让一条针的正确性
    压在"`backend/.env` 里恰好没有某一行"这种看不见的前提上。
    """
    rc, out, err = run_python(
        REVIVE_SOURCE, WORKER_EXIT_ON_FATAL="0", APP_ENV="development"
    )
    assert rc == 0, (rc, out, err[-2000:])
    assert "REVIVED 2" in out, out          # 第二次进入 run_worker = 复活成功
    assert "CALLS 2" in out
    assert "SKIPPED_SLEEP 1.0" in out       # 第一次退避就是 min_backoff=1.0s


def test_23_dead_loop_pool_is_genuinely_unusable(isolated_urls, migrated_schema):
    """证据针（不是回归针）：#23 的失效机制在当前版本以「死池 close 不掉」这个面存活。

    R-T4-1 重定靶（本窗口实测，工件 t4-fix-measure-pin.txt）：brief 的原前提
    「读穿绑在已关 loop 上的池会抛 loop 错」被 psycopg 3.3.6 / psycopg_pool 3.3.3 证伪
    ——psycopg/waiting.py 每次 wait 现取 get_running_loop()、旧 AsyncFile 的 loop 存储已没了，
    连接在 socket 层不再 loop 亲和。实测：死 loop 的池上
      · aget_tuple 读一条**不存在的** thread → 返回 None（不抛）；
      · 读一条 loop A 上真写过、确实存在的 checkpoint → 跨 loop **正常返回 CheckpointTuple**。
    所以「读穿死池即失效」这一面在该版本根本不成立，针不写它（不是放宽，是无从断）。

    机制换面存活，且这一面比原目标更贴 #23 的真实因果：死 loop 的池 **close 不掉**。
    loop A 关掉后再在 loop B 上 `await pool.close()` 抛 RuntimeError("Event loop is closed")
    ——这正是正常收尾路径 shutdown_checkpointer() 里 checkpointer.py:142 那一句
    （t4-23-red-pin.txt 的 teardown error 就砸在这里，实测在档）。收尾自己会抛 ⇒ 单例清不掉
    ⇒ 守护只能带着死池引用继续跑，这就是为什么单例只能**丢引用**（discard_singletons()，
    修法 1）。所以 "unusable" 的准确说法是 "unrecoverable"：证据落在「连正常清理路径都对它
    无能为力」，比「读会抛」更靠近 bug 本体。

    这条证据的边界（按评审口径收窄，别把论证读过头）：它钉的是**修法 1 的必要性**到此为止。
    · 修法 2（WORKER_EXIT_ON_FATAL ⇒ SystemExit(1)，交 restart: unless-stopped 起全新进程）
      的依据是 spec §8 的双形态裁定，并由两枚退出码针
      test_23_exit_on_fatal_makes_the_guard_process_exit /
      test_23_without_the_flag_the_guard_revives_in_process 各自独立钉住，**不靠本针**。
    · 「discard 之后守护原地复活、重建健康池」也不是本针证的，那是回归针
      test_23_discard_singletons_lets_a_new_loop_rebuild 证的事。

    证据针，discard 不参与判定：断言只测死池的 close 行为，discard_singletons() 只出现在
    finally 里把复位后的单例交回夹具——夹具的 shutdown_checkpointer() 见到 _pool is None 即
    no-op，不会去 await 这条刚被证明「await 就抛」的死池。修复前后此针都绿（它钉机制是否存在，
    不钉修没修好）。若 (1) 跑出来**不抛**，那是唯一需要重判 spec §8 的发现：停下报告，别改针。
    """
    import asyncio

    loop_a = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop_a)
        loop_a.run_until_complete(checkpointer.ensure_setup())
        pool = checkpointer._pool          # 绑在 loop A 上的那条池（shutdown_checkpointer 用的就是它）
    finally:
        loop_a.close()                     # 中毒现场：单例还指着这条 loop 已关的池

    loop_b = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop_b)
        # (1) 唯一断言，独立可伪：正常收尾路径 await pool.close() 在死 loop 的池上必抛 loop 错。
        #     match="loop" 同时认 "Event loop is closed" 与 "different loop" 两种同源形状。
        with pytest.raises(RuntimeError, match="loop"):
            loop_b.run_until_complete(pool.close())
    finally:
        # 复位再交回夹具（不参与上面判定）：夹具若去 await 这条死池的 close() 就是刚证明的抛错。
        checkpointer.discard_singletons()
        asyncio.set_event_loop(None)
        loop_b.close()
