"""企业库存与补货数据域：建库 + 建表 + 种子（Phase 10 spec §3.2）。

目录语义（照 scripts/seed_business_data.py 的头注三条纪律）：
    不是应用代码（不被服务 import）、不是迁移（本库刻意**不进 alembic**，见下）。

为什么不进 alembic：alembic.ini/env.py 绑的是单库 settings.database_url，
    把第二个 database 塞进同一条迁移链要引入多 engine + 版本表分库，收益只是「少一个脚本」。
    代价如实登记：这个库没有版本管理 ⇒ 本批它是只读、单版本、可全量重建的演示数据，
    脚本自带 DROP + 重建；真要演进时再补第二个 alembic -c 配置（spec §2.3 / §9）。

幂等：DROP TABLE IF EXISTS 重建 + 行 id 用 uuid5 从**该行全部业务字段**派生
    （inventory_levels 由 (product, region) 一整对、stock_movements 由
     (product, region, direction, day, qty, 对内序号) ⇒ 两条派生都是单射，撞不了主键）。
    实证口径——Step 3 证的是这两件事，不是口头承诺：
        ① 相邻两次运行的 stdout 逐字节一致（diff 无输出）；
        ② 两张表「除 now() 时间戳列以外」逐列拼串的 md5 一致（内容级，不只看行数）。
    边界如实登记，不把话说满：
        · updated_at / created_at 是 DEFAULT now()，每次重建时刻必然不同 ⇒ 不进 ①② 的对比
          （自检只数行、不比时刻，spec §3.2 的幂等口径本来就是这个意思）；
        · 首次真建库那一次打印 createdb: … 已创建，此后每次都是 已存在，跳过
          （PG 判存的必然结果）⇒ ① 只对「之后任意相邻两次」成立，
          拿建库那次去比会看到这一行差异，那不是幂等失败（原文登记在 idempotent.out）。
    ⇒ 因此这里不写「连跑两次内容逐字节一致」那种一口价的断言，能证成的只有上面 ①+②。
可重现：random.seed(42)。

数据里埋的故事（这是本批出口问题的答案，不是装饰）：
    ① 恰好 3 行低于安全库存，且分属近 90 天毛利前 3 的品类 ⇒ 「哪些要补」有非平凡答案
    ② 出库流水都在 90 天窗口内、入库流水全在窗口外 ⇒ 「只出不进」= 断货的机制解释
    ③ 健康行的 on_hand 构造为 safety + 日均×覆盖天数 + 50 ⇒ 建议量恒为 0，
       「suggested_qty > 0 的行数 = 3」是可**证明**的，不是碰运气数出来的

⚠️ 跨库引用（本批唯一的数据设计新概念）：product_id / region_id 指向 ai_workspace 的同值 uuid，
   但**建不了 FK**（跨 database 不可能）。这层隐式引用只有「种子读真实 id」这一条路能保证正确，
   所以本脚本先读 ai_workspace 的 products/regions，再写 enterprise 库（见 read_source）。

用法（在 backend/ 目录下）：
    .venv\\Scripts\\python.exe scripts\\create_enterprise_db.py
"""
import asyncio
import math
import random
import re
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.enterprise_dsn import enterprise_database_url

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")  # 与 seed_business_data.py 同源
RANDOM_SEED = 42
COVER_DAYS = 30            # 与 suggest_replenishment 的 days_of_cover 默认一致
CONSUMPTION_WINDOW = 90    # 日均出库窗口，**与 app/mcp/inventory_data.py 的常量同源**
LOW_ROWS = 3               # 埋的故事行数（毛利前 3 品类各一行）
OUT_MAX_AGE = 89           # 出库流水最老的一天（< 窗口 ⇒ 全部计入日均）
IN_MIN_AGE = 120           # 入库流水最早 / IN_MAX_AGE 最老（> 窗口 ⇒ 窗口内 in_total = 0）
IN_MAX_AGE = 200

DDL_INVENTORY = """
CREATE TABLE inventory_levels (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    -- 隐式跨库引用 ai_workspace.products.id：同值 uuid，但跨 database 建不了 FK。
    -- 这是两条链能接起来的唯一黏合剂（spec §3.2），所以种子必须读真实 id 再写。
    product_id uuid NOT NULL,
    region_id uuid NOT NULL,          -- 隐式跨库引用 ai_workspace.regions.id（省级）
    region_name varchar(100) NOT NULL, -- 省名的**副本**：跨库 join 拿不到名字，只能自带一份（R129①）
    on_hand_qty integer NOT NULL,
    safety_stock_qty integer NOT NULL,
    leadtime_days integer NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

DDL_MOVEMENTS = """
CREATE TABLE stock_movements (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    product_id uuid NOT NULL,
    region_id uuid NOT NULL,
    movement_date date NOT NULL,
    qty integer NOT NULL,
    direction varchar(10) NOT NULL,   -- 'in' / 'out'：应用层枚举、库层字符串（docs/05 全局口径）
    created_at timestamptz NOT NULL DEFAULT now()
)
"""

DDL_INDEXES = [
    "CREATE INDEX idx_inv_org_product ON inventory_levels (organization_id, product_id)",
    "CREATE INDEX idx_mv_org_date ON stock_movements (organization_id, movement_date)",
    # 后一条正是「算日均消耗」那条 SQL 的形状（organization_id + direction + movement_date 前缀）
    "CREATE INDEX idx_mv_org_product_date ON stock_movements (organization_id, product_id, movement_date)",
]

_IDENT = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def _assert_ident(name: str) -> str:
    """标识符要拼进 CREATE DATABASE / DROP TABLE 的字符串里（PG 不支持这类语句的绑定参数），
    所以只能靠白名单校验兜。库名来自 settings（配置面），不是模型输入，但一样不放过。"""
    if not _IDENT.fullmatch(name):
        raise ValueError(f"非法数据库/表标识符：{name!r}")
    return name


async def ensure_database() -> None:
    """建库。PG 不允许 CREATE DATABASE 进事务，所以连 **postgres 维护库** + AUTOCOMMIT。"""
    target = _assert_ident(settings.enterprise_db_name)
    admin_url = enterprise_database_url(settings.database_url, "postgres")  # 复用纯函数，不手写第二遍
    eng = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with eng.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": target}
            )
            if exists:
                print(f"createdb: {target} 已存在，跳过")
            else:
                await conn.execute(text(f'CREATE DATABASE "{target}"'))
                print(f"createdb: {target} 已创建")
    finally:
        await eng.dispose()


async def read_source() -> tuple[list[dict], list[dict], list[str]]:
    """从 ai_workspace 读**真实** products / 省级 regions / 近 90 天毛利前 3 品类。

    为什么必须先读再写：补货清单里的 product_id 要能被应用侧 query_product 认出来，
    否则两跳断在第一跳（spec §3.2 硬要求 1）。
    为什么毛利窗口用「今天往回 90 天」：与出口问题那句「近 90 天」同口径，
    也与 server 侧的 CONSUMPTION_WINDOW 同口径——三处一把尺。
    """
    eng = create_async_engine(settings.database_url)
    try:
        async with eng.connect() as conn:
            products = [
                dict(r)
                for r in (
                    await conn.execute(
                        text("SELECT id, name, category FROM products "
                             "WHERE organization_id = :org AND category IS NOT NULL ORDER BY name"),
                        {"org": str(ORG_ID)},
                    )
                ).mappings()
            ]
            regions = [
                dict(r)
                for r in (
                    await conn.execute(
                        text("SELECT id, name FROM regions "
                             "WHERE organization_id = :org AND level = 'province' ORDER BY name"),
                        {"org": str(ORG_ID)},
                    )
                ).mappings()
            ]
            since = date.today() - timedelta(days=CONSUMPTION_WINDOW)
            top = [
                r[0]
                for r in await conn.execute(
                    text("SELECT p.category, SUM(s.profit) AS profit FROM sales s "
                         "JOIN products p ON p.id = s.product_id "
                         "WHERE s.organization_id = :org AND s.sale_date >= :since "
                         "GROUP BY p.category ORDER BY profit DESC LIMIT 3"),
                    {"org": str(ORG_ID), "since": since},
                )
            ]
    finally:
        await eng.dispose()
    if not products or not regions:
        raise RuntimeError("ai_workspace 里没有 products/regions —— 先跑 scripts/seed_business_data.py")
    if len(top) != 3:
        raise RuntimeError(f"毛利前 3 品类只拿到 {len(top)} 个（窗口 {since} 之后没数据？）")
    return products, regions, top


def _mv(pid: uuid.UUID, rid: uuid.UUID, day: date, qty: int, direction: str, seq: int) -> dict:
    """行 id 从 (product, region, direction, day, qty, seq) 派生 ⇒ 同输入同 id（幂等的根据）。

    seq（本 (产品, 省) 内的第几笔流水）是**必须有**的那一项，不是装饰：
    day 与 qty 是两次独立随机抽取，同一对里的六笔出库完全可能抽到同一个 (day, qty)，
    少了 seq 那条流水的 id 就一样 ⇒ 换一份数据重种时会在 INSERT 上撞重复主键（现在这份
    恰好没抽中，所以是个潜伏崩溃）。有了 seq，(direction, seq) 在一对内天然唯一，
    再乘上 (pid, rid) 全局唯一 ⇒ id 派生在构造上就是单射，不靠运气。
    """
    return {
        "id": uuid.uuid5(uuid.NAMESPACE_DNS,
                         f"p10-mv-{pid}-{rid}-{direction}-{day}-{qty}-{seq}"),
        "organization_id": ORG_ID,
        "product_id": pid,
        "region_id": rid,
        "movement_date": day,
        "qty": qty,
        "direction": direction,
    }


def build_rows(products, regions, top_categories, today: date):
    """内存里造出全部行，返回 (inventory_rows, movement_rows, low_pairs)。

    低库存的挑法（可复核，不是随机碰）：毛利前 3 品类各取**名字排序第一个**产品，
    打在 regions[0] 那个省上 ⇒ 恰好 3 行、且分属 3 个不同品类。
    """
    random.seed(RANDOM_SEED)
    low_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for cat in top_categories:
        in_cat = [p for p in products if p["category"] == cat]
        if not in_cat:
            raise RuntimeError(f"品类 {cat} 里没有产品，种子的「3 行低库存」前提不成立")
        low_pairs.add((in_cat[0]["id"], regions[0]["id"]))

    inv_rows, mv_rows = [], []
    for p in products:
        for r in regions:
            pid, rid = p["id"], r["id"]
            out_qty = 0
            for k in range(6):  # 每个 (产品, 省) 六笔出库，全落在窗口内
                qty = random.randint(5, 40)
                out_qty += qty
                mv_rows.append(_mv(pid, rid, today - timedelta(days=random.randint(1, OUT_MAX_AGE)),
                                   qty, "out", k))
            # 入库那笔的序号接在六笔出库之后（同一对内 seq 不重复 ⇒ id 单射，见 _mv 的头注）
            mv_rows.append(_mv(pid, rid, today - timedelta(days=random.randint(IN_MIN_AGE, IN_MAX_AGE)),
                               random.randint(200, 400), "in", 6))  # 入库全在窗口外 ⇒ 只出不进
            daily_avg = out_qty / CONSUMPTION_WINDOW
            safety = random.randint(40, 120)
            if (pid, rid) in low_pairs:
                on_hand = int(safety * 0.5)
            else:
                on_hand = math.ceil(safety + daily_avg * COVER_DAYS) + 50
            inv_rows.append({
                "id": uuid.uuid5(uuid.NAMESPACE_DNS, f"p10-inv-{pid}-{rid}"),
                "organization_id": ORG_ID,
                "product_id": pid,
                "region_id": rid,
                "region_name": r["name"],
                "on_hand_qty": on_hand,
                "safety_stock_qty": safety,
                "leadtime_days": random.randint(3, 21),
            })
    return inv_rows, mv_rows, low_pairs


async def recreate_tables() -> None:
    url = enterprise_database_url(settings.database_url, settings.enterprise_db_name)
    eng = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with eng.connect() as conn:
            for tbl in ("stock_movements", "inventory_levels"):
                await conn.execute(text(f"DROP TABLE IF EXISTS {tbl}"))
            await conn.execute(text(DDL_INVENTORY))
            await conn.execute(text(DDL_MOVEMENTS))
            for ddl in DDL_INDEXES:
                await conn.execute(text(ddl))
    finally:
        await eng.dispose()


async def write_rows(inv_rows, mv_rows) -> None:
    url = enterprise_database_url(settings.database_url, settings.enterprise_db_name)
    eng = create_async_engine(url)
    ins_inv = text(
        "INSERT INTO inventory_levels (id, organization_id, product_id, region_id, region_name, "
        "on_hand_qty, safety_stock_qty, leadtime_days) VALUES "
        "(:id, :organization_id, :product_id, :region_id, :region_name, "
        ":on_hand_qty, :safety_stock_qty, :leadtime_days)"
    )
    ins_mv = text(
        "INSERT INTO stock_movements (id, organization_id, product_id, region_id, movement_date, qty, direction) "
        "VALUES (:id, :organization_id, :product_id, :region_id, :movement_date, :qty, :direction)"
    )
    try:
        async with eng.begin() as conn:
            await conn.execute(ins_inv, inv_rows)
            await conn.execute(ins_mv, mv_rows)
    finally:
        await eng.dispose()


def suggested(on_hand: int, safety: int, out_qty: int, days_of_cover: int) -> int:
    """与 app/mcp/inventory_data.replenishment_suggestions **同一行算术**（两份实现是有意的：
    spec §7 针⑧ 的数字对账要的就是「独立再算一遍」，一处写错两处对不上才能炸）。"""
    daily = out_qty / CONSUMPTION_WINDOW
    return max(0, math.ceil(safety + daily * days_of_cover - on_hand))


async def selfcheck(products, regions, top_categories) -> None:
    """打印实测计数（spec §3.2 硬要求 2：自检并打印，不是 assert 完事）。"""
    url = enterprise_database_url(settings.database_url, settings.enterprise_db_name)
    eng = create_async_engine(url)
    try:
        async with eng.connect() as conn:
            n_inv = await conn.scalar(text("SELECT count(*) FROM inventory_levels"))
            n_mv = await conn.scalar(text("SELECT count(*) FROM stock_movements"))
            n_low = await conn.scalar(
                text("SELECT count(*) FROM inventory_levels WHERE on_hand_qty < safety_stock_qty"))
            today = date.today()
            since = today - timedelta(days=CONSUMPTION_WINDOW)
            out_in = await conn.scalar(
                text("SELECT count(*) FROM stock_movements "
                     "WHERE direction = 'out' AND movement_date >= :since"), {"since": since})
            in_in = await conn.scalar(
                text("SELECT count(*) FROM stock_movements "
                     "WHERE direction = 'in' AND movement_date >= :since"), {"since": since})
            rows = [dict(r) for r in (await conn.execute(text(
                "SELECT product_id, region_id, on_hand_qty, safety_stock_qty "
                "FROM inventory_levels"))).mappings()]
            out_map: dict[tuple, int] = {}
            for r in (await conn.execute(text(
                "SELECT product_id, region_id, SUM(qty) AS q FROM stock_movements "
                "WHERE direction = 'out' AND movement_date >= :since "
                "GROUP BY product_id, region_id"), {"since": since})).mappings():
                out_map[(r["product_id"], r["region_id"])] = int(r["q"])
    finally:
        await eng.dispose()

    n_sugg = sum(1 for r in rows
                 if suggested(r["on_hand_qty"], r["safety_stock_qty"],
                              out_map.get((r["product_id"], r["region_id"]), 0), COVER_DAYS) > 0)

    print(f"  products={len(products)} regions={len(regions)} 毛利前3={top_categories}")
    print(f"  inventory_levels 行数 = {n_inv}（期望 {len(products) * len(regions)}）")
    print(f"  stock_movements 行数 = {n_mv}")
    print(f"  低库存行数 = {n_low}（期望 {LOW_ROWS}）")
    print(f"  建议补货行数 = {n_sugg}（期望 {LOW_ROWS}）")
    print(f"  窗口内出库行数 = {out_in}（期望 >0）｜窗口内入库行数 = {in_in}（期望 0）")
    for label, ok in (
        ("inventory_levels 行数", n_inv == len(products) * len(regions)),
        ("低库存行数", n_low == LOW_ROWS),
        ("建议补货行数", n_sugg == LOW_ROWS),
        ("窗口内入库为 0", in_in == 0),
    ):
        if not ok:
            raise RuntimeError(f"种子自检失败：{label} 不符（{label} 实测见上一段打印）")
    print("自检通过")


async def main() -> None:
    print(f"today = {date.today()}（窗口 {date.today() - timedelta(days=CONSUMPTION_WINDOW)} 起）")
    await ensure_database()
    products, regions, top_categories = await read_source()
    inv_rows, mv_rows, low_pairs = build_rows(products, regions, top_categories, date.today())
    await recreate_tables()
    await write_rows(inv_rows, mv_rows)
    await selfcheck(products, regions, top_categories)


if __name__ == "__main__":
    asyncio.run(main())
