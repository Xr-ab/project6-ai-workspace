"""企业模拟业务数据种子（Phase 3）。

为什么单独建 scripts/ 目录：
    种子脚本不是应用代码（不被服务 import）、也不是迁移（不改表结构）。
    放 app/ 会被打进服务包，放 migrations/ 会和 alembic 的目录语义混淆。

幂等：每次跑先按 organization_id 清空 5 张表再重新插入。
    所以重复执行结果一致，不会累积脏数据 —— 调试时随时可以从干净基线重来。

可重现：random.seed(42) 固定随机序列，每次跑出的是同一批数据。
    否则"昨天那条 SQL 查出 12 万、今天变 11 万"，排查分不清是代码问题还是数据变了。

数据里埋了三个"故事"，让 SQL 分析能得出非平凡结论：
    ① 华东区 2026-04 起销量大幅下滑（模拟市场下滑）→ 区域对比/趋势分析能看出来
    ② SKU-007 成本接近售价（毛利异常低）→ 产品毛利排行能揪出来
    ③ 有 high 风险 + key 级客户（高风险高贡献）→ 客户维度分析有取舍题

用法（在 backend/ 目录下）：
    .venv\\Scripts\\python.exe scripts\\seed_business_data.py
"""
import asyncio
import random
import sys
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

# 和 scratch/ 下的验证脚本同一套办法：直接 python xxx.py 就能跑
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select

from app.data.db import AsyncSessionLocal
from app.data.models import Customer, Order, Product, Region, Sale

RANDOM_SEED = 42
START = date(2025, 1, 1)
END = date(2026, 9, 20)
# 华东区从这天起销量下滑（故事 ①）。两处叠加（数量减半 + 频率减半）后
# 实测月均降幅约 75% —— 够显著，不至于被日常波动淹没。
# 实测口径：2026 年 1-3 月月均 603 万 → 4-9 月月均 117 万，
# 同期其他区域月均有升有降但都在 ±60% 内。
EAST_DECLINE_FROM = date(2026, 4, 1)

# (省 code, 省名, [(市 code, 市名), ...])
REGION_TREE = [
    ("R-EC", "华东", [("R-EC-SH", "上海"), ("R-EC-HZ", "杭州")]),
    ("R-NC", "华北", [("R-NC-BJ", "北京"), ("R-NC-TJ", "天津")]),
    ("R-SC", "华南", [("R-SC-GZ", "广州"), ("R-SC-SZ", "深圳")]),
    ("R-CC", "华中", [("R-CC-WH", "武汉"), ("R-CC-CS", "长沙")]),
    ("R-WC", "西南", [("R-WC-CD", "成都"), ("R-WC-CQ", "重庆")]),
    ("R-NW", "西北", [("R-NW-XA", "西安"), ("R-NW-LZ", "兰州")]),
]

# (code, 客户名, 行业, 省 code, tier, risk_level)
CUSTOMERS = [
    ("C001", "恒远科技有限公司", "信息技术", "R-EC", "key", "low"),
    ("C002", "云图数据服务", "信息技术", "R-EC", "key", "medium"),
    ("C003", "海通物流集团", "物流运输", "R-EC", "standard", "low"),
    ("C004", "锦程贸易", "批发零售", "R-EC", "small", "high"),
    ("C005", "北方重工", "装备制造", "R-NC", "key", "low"),
    ("C006", "京畿传媒", "文化传媒", "R-NC", "standard", "medium"),
    ("C007", "津门食品", "食品加工", "R-NC", "small", "low"),
    ("C008", "南粤电子", "电子制造", "R-SC", "key", "low"),
    ("C009", "鹏城网络", "信息技术", "R-SC", "standard", "medium"),
    ("C010", "珠江医疗", "医疗健康", "R-SC", "standard", "low"),
    ("C011", "长江建材", "建筑材料", "R-CC", "small", "high"),
    ("C012", "楚天教育", "教育培训", "R-CC", "standard", "low"),
    ("C013", "湘江零售", "批发零售", "R-CC", "small", "medium"),
    ("C014", "蜀道能源", "能源电力", "R-WC", "key", "low"),
    ("C015", "山城汽车", "汽车制造", "R-WC", "standard", "low"),
    ("C016", "天府软件", "信息技术", "R-WC", "small", "medium"),
    ("C017", "长安机械", "装备制造", "R-NW", "standard", "low"),
    ("C018", "丝路贸易", "批发零售", "R-NW", "small", "high"),
    ("C019", "昆仑材料", "新材料", "R-NW", "standard", "medium"),
    ("C020", "东海智造", "电子制造", "R-EC", "key", "low"),
]

# (sku, 名称, 品类, 售价, 成本)
PRODUCTS = [
    ("SKU-001", "企业级数据平台授权", "软件授权", 128000, 42000),
    ("SKU-002", "智能分析套件", "软件授权", 68000, 21000),
    ("SKU-003", "边缘计算服务器", "硬件设备", 45000, 33000),
    ("SKU-004", "工业传感器模组", "硬件设备", 3200, 1850),
    ("SKU-005", "标准运维服务包", "服务订阅", 36000, 15000),
    ("SKU-006", "高级技术支持年费", "服务订阅", 88000, 30000),
    ("SKU-007", "智能网关 X1", "硬件设备", 5800, 5600),  # 故事 ②：毛利异常低
    ("SKU-008", "数据治理咨询", "咨询服务", 150000, 68000),
    ("SKU-009", "云资源托管", "服务订阅", 24000, 16000),
    ("SKU-010", "存储扩展柜", "硬件设备", 18600, 12400),
    ("SKU-011", "安全审计模块", "软件授权", 42000, 13000),
    ("SKU-012", "现场实施服务", "咨询服务", 52000, 31000),
    ("SKU-013", "备件更换服务", "备件", 2600, 1900),
    ("SKU-014", "培训认证课程", "咨询服务", 9800, 3200),
    ("SKU-015", "定制开发工时包", "咨询服务", 96000, 54000),
]


def money(value: float | Decimal) -> Decimal:
    """转成两位小数。

    走 str() 而不是 Decimal(float)：Decimal(0.1) 会拿到 0.1000000000000000055…，
    Decimal("0.1") 才是干净的 0.1。
    """
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def _clear(session, org_id: uuid.UUID) -> None:
    """按组织清空业务表。

    删除顺序 = 外键依赖的逆序：sales / orders 引用 customers 和 products，
    customers 引用 regions，所以必须"先删引用方"，
    否则会撞 ForeignKeyViolation。
    """
    for model in (Sale, Order, Customer, Region, Product):
        await session.execute(delete(model).where(model.organization_id == org_id))
    await session.commit()


async def _seed_regions(session, org_id: uuid.UUID) -> dict[str, Region]:
    """建两级区域树，返回 {code: Region}。"""
    by_code: dict[str, Region] = {}
    for province_code, province_name, cities in REGION_TREE:
        province = Region(
            organization_id=org_id,
            code=province_code,
            name=province_name,
            level="province",
        )
        session.add(province)
        await session.flush()  # 拿到 province.id 给子级用；不 commit，仍在同一事务里
        by_code[province_code] = province
        for city_code, city_name in cities:
            city = Region(
                organization_id=org_id,
                code=city_code,
                name=city_name,
                level="city",
                parent_id=province.id,
            )
            session.add(city)
            by_code[city_code] = city
    await session.commit()
    return by_code


async def _seed_customers(session, org_id: uuid.UUID, regions: dict[str, Region]) -> list[Customer]:
    customers = [
        Customer(
            organization_id=org_id,
            code=code,
            name=name,
            industry=industry,
            region_id=regions[region_code].id,
            tier=tier,
            risk_level=risk,
        )
        for code, name, industry, region_code, tier, risk in CUSTOMERS
    ]
    session.add_all(customers)
    await session.commit()
    return customers


async def _seed_products(session, org_id: uuid.UUID) -> list[Product]:
    products = [
        Product(
            organization_id=org_id,
            sku=sku,
            name=name,
            category=category,
            unit_price=money(price),
            unit_cost=money(cost),
        )
        for sku, name, category, price, cost in PRODUCTS
    ]
    session.add_all(products)
    await session.commit()
    return products


def _order_quantity(customer: Customer, day: date, east_region_id: uuid.UUID) -> int:
    """订单里的产品数量。华东区在分界日之后减半（故事 ①）。"""
    quantity = random.randint(2, 30)
    if customer.region_id == east_region_id and day >= EAST_DECLINE_FROM:
        quantity = max(1, quantity // 2)
    return quantity


def _pick_customer(
    customers: list[Customer], day: date, east_region_id: uuid.UUID
) -> Customer:
    """随机挑客户；华东在分界日后有一半概率被替换成非华东客户（故事 ①）。

    为什么要同时降低"下单频率"而不只是"下单数量"：
        实测发现只减数量的话，华东月度销售额在 400 万 ~ 830 万之间乱跳，
        滑没下滑根本看不出来 —— 单日订单数本身随机（0~3 单），
        月度只有二十来单，噪声把信号完全盖住了。
        降低频率等于把信号强度翻倍，趋势才稳定可检测。
    """
    customer = random.choice(customers)
    if (
        customer.region_id == east_region_id
        and day >= EAST_DECLINE_FROM
        and random.random() < 0.5
    ):
        return random.choice([c for c in customers if c.region_id != east_region_id])
    return customer


async def _seed_orders(
    session,
    org_id: uuid.UUID,
    customers: list[Customer],
    products: list[Product],
    regions: dict[str, Region],
) -> list[Order]:
    """逐日生成订单明细，返回落库的订单列表（sales 要拿它聚合）。

    客户和产品用 random.choice 随机挑而不是轮询：
    随机组合会出现"某客户反复买同一款"，自然形成可分析的集中度；
    轮询则每个组合出现次数一模一样，算出来太整齐、不像真实数据。
    """
    east_region_id = regions["R-EC"].id
    orders: list[Order] = []
    day = START
    seq_in_day = 0
    while day <= END:
        # 周末不发货，订单量低 —— 让按星期聚合的 SQL 能看出规律
        daily = random.choice([0, 1, 2, 3]) if day.weekday() < 5 else random.choice([0, 1])
        for _ in range(daily):
            seq_in_day += 1
            customer = _pick_customer(customers, day, east_region_id)
            product = random.choice(products)
            quantity = _order_quantity(customer, day, east_region_id)
            unit_price = product.unit_price
            orders.append(
                Order(
                    organization_id=org_id,
                    order_no=f"SO-{day.strftime('%Y%m%d')}-{seq_in_day:04d}",
                    customer_id=customer.id,
                    product_id=product.id,
                    region_id=customer.region_id,
                    order_date=day,
                    quantity=quantity,
                    unit_price=unit_price,
                    amount=money(unit_price * quantity),
                    status="completed" if random.random() > 0.06 else "cancelled",
                )
            )
        day += timedelta(days=1)

    session.add_all(orders)
    await session.commit()
    return orders


async def _seed_sales(
    session, org_id: uuid.UUID, orders: list[Order], products: list[Product]
) -> int:
    """由订单聚合出销售汇总（region + product + 日期三个维度）。

    为什么 sales 不独立造假数据、而是从 orders 推导：
        独立生成的话两张表的数字对不上（orders 加起来不等于 sales 加起来），
        任何"交叉验证"型分析都会得出矛盾结论，排查时先怀疑代码。
        派生保证口径一致，也演示了"明细 → 汇总"的正常数仓关系。

    为什么 customer_id 留空：
        聚合维度是区域+产品+日期，客户信息在这一层已经被"抹掉"了 ——
        这正是聚合的语义，不是数据缺失。要看客户维度就去查 orders。

    为什么跳过 cancelled：
       已取消订单不能算销售额。这是最关键的一条口径 —— 不排除的话，
       销售汇总会虚高，而虚高的数字看起来完全正常（最难发现的一类错误）。
    """
    cost_of = {p.id: p.unit_cost for p in products}
    buckets: dict[tuple, dict] = defaultdict(
        lambda: {"quantity": 0, "amount": Decimal("0"), "cost": Decimal("0")}
    )
    for o in orders:
        if o.status != "completed":
            continue
        key = (o.region_id, o.product_id, o.order_date)
        b = buckets[key]
        b["quantity"] += o.quantity
        b["amount"] += o.amount
        b["cost"] += money(cost_of[o.product_id] * o.quantity)

    rows = [
        Sale(
            organization_id=org_id,
            region_id=region_id,
            product_id=product_id,
            customer_id=None,  # 见 docstring
            sale_date=sale_date,
            quantity=b["quantity"],
            amount=b["amount"],
            cost=b["cost"],
            profit=b["amount"] - b["cost"],
        )
        for (region_id, product_id, sale_date), b in buckets.items()
    ]
    session.add_all(rows)
    await session.commit()
    return len(rows)


async def main() -> None:
    random.seed(RANDOM_SEED)
    # 8a：settings.dev_* 已删，业务数据种在首个迁移种下的开发组织名下（常量留在脚本侧）
    org_id = uuid.UUID("00000000-0000-0000-0000-000000000001")

    async with AsyncSessionLocal() as session:
        await _clear(session, org_id)
        regions = await _seed_regions(session, org_id)
        customers = await _seed_customers(session, org_id, regions)
        products = await _seed_products(session, org_id)
        orders = await _seed_orders(session, org_id, customers, products, regions)
        n_sales = await _seed_sales(session, org_id, orders, products)

        # 从库里重新统计（不是用内存里的 len）—— 顺便验证数据真的落盘了
        counts = {}
        for name, model in (
            ("regions", Region),
            ("customers", Customer),
            ("products", Product),
            ("orders", Order),
            ("sales", Sale),
        ):
            counts[name] = await session.scalar(
                select(func.count()).select_from(model).where(model.organization_id == org_id)
            )

    print(f"org_id = {org_id}")
    for name, n in counts.items():
        print(f"  {name:10s} {n:5d}")
    completed = sum(1 for o in orders if o.status == "completed")
    print(f"  （orders 中 completed {completed} / cancelled {len(orders) - completed}；sales 由 completed 聚合）")
    assert counts["sales"] == n_sales


if __name__ == "__main__":
    asyncio.run(main())