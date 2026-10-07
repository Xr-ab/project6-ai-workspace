"""reports 时间窗过滤的行为面（Phase 9b 三处留白之一）。

为什么必须有真库这一条：`since` 谓词只影响 SQL 的 WHERE，离线层能钉的全是声明面
（OpenAPI 里有 `range` 这个参数、`list`/`count` 签名一致 —— 见
`tests/unit/test_report_layer.py` 的 C 组）。而真正会错的是下面这三件事，
它们都只在"行真的落在库里、时间真的铺开"时才看得出来：

1. **窗口边界算得对不对**：`week` 到底是滚动 7 天还是自然周、`today` 是不是本地日
   00:00 —— 这一条由 `stats_repo.range_start` 保证，本针演的是"报告这条查询
   确实接上了它"，而不是又一遍口径证明。
2. **`total` 与当页同源**：列表筛了、计数没筛，是分页接口最隐蔽的谎 —— 第一页
   看起来完全正常，翻到第二页或者点"下一页"变灰时才露。这条只能数出来。
3. **时间谓词与可见性谓词能叠加**：`user_id`（member 档）和 `since` 各写各的分支，
   组合起来少一条就是越权或多筛。

行上的 `created_at` 是**显式写进去的**而不是靠 `server_default`：不写死的"现在"
过 8 小时再跑，`today` 窗就会莫名少一行（那是针自己的形状问题，不是产品的 bug）。
"""
import uuid
from datetime import datetime, timedelta

import pytest

from app.application import report_service
from app.data.models import Organization, Report, User

pytestmark = [pytest.mark.db]

# 四条行的年龄（天）。0 = 此刻，其余依次落在 today / week / month 之外。
# 3 与 10 之间留了 4 天的余量，45 又远出 30 天的窗 —— 边界两侧都不靠分钟级运气。
AGES_IN_DAYS = (0, 3, 10, 45)


async def _seed(db_session, *, org_id, user_id, ages=AGES_IN_DAYS, prefix="rpt"):
    """种 `ages` 份报告（每份一个 created_at），返回按年龄排序的 (title, created_at) 列表。"""
    now = datetime.now().astimezone()
    rows = []
    for i, age in enumerate(ages):
        rows.append(
            Report(
                id=uuid.uuid4(),
                organization_id=org_id,
                user_id=user_id,
                title=f"{prefix}-{age}d-{i}",
                report_type="analysis",
                content={"executive_summary": "探针"},
                created_at=now - timedelta(days=age, minutes=i),
            )
        )
    db_session.add_all(rows)
    await db_session.commit()
    return rows


async def _list(db_session, *, org_id, user_id=None, range_key="all"):
    return await report_service.list_reports(
        db_session,
        organization_id=org_id,
        user_id=user_id,
        report_type=None,
        limit=50,
        offset=0,
        range_key=range_key,
    )


@pytest.mark.parametrize(
    ("range_key", "expected_ages"),
    [
        ("all", [0, 3, 10, 45]),
        ("month", [0, 3, 10]),
        ("week", [0, 3]),
        ("today", [0]),
    ],
)
async def test_range_windows_select_the_rows_the_window_owns(
    db_session, make_identity, range_key, expected_ages
):
    """四档窗各筛各的：`today` 只剩此刻那条，`all` 一档不少。

    断言的是**年龄集合**而不是条数：条数对了但筛错了行（比如把 45 天前的留下、
    把今天的丢掉）是同类 bug，只看 `len` 抓不到。
    """
    idn = await make_identity("admin")
    await _seed(db_session, org_id=idn.organization_id, user_id=idn.user_id)
    rows, total = await _list(db_session, org_id=idn.organization_id, range_key=range_key)
    assert [r.title.split("-")[1] for r in rows] == [f"{a}d" for a in expected_ages]
    assert total == len(expected_ages), f"{range_key} 窗的 total 与当页不同源：{total}"


async def test_total_and_page_agree_when_the_window_is_bigger_than_the_page(db_session, make_identity):
    """`total` 必须是全窗条数、`items` 只当页 —— 两条不许互相顶替。

    `month` 窗里有 3 行，`limit=2`：期望 `total=3` 而 items 只有 2。
    这一条钉的是"计数没被 limit 截过"（反过来 `total=len(rows)` 那种写法在这里红），
    前端「共 N 份」和末页页码全靠它。
    """
    idn = await make_identity("admin")
    await _seed(db_session, org_id=idn.organization_id, user_id=idn.user_id)
    rows, total = await report_service.list_reports(
        db_session,
        organization_id=idn.organization_id,
        user_id=None,
        report_type=None,
        limit=2,
        offset=0,
        range_key="month",
    )
    assert total == 3, f"计数被当页长度顶掉了：{total}"
    assert len(rows) == 2


async def test_time_window_and_visibility_scope_stack(db_session, make_identity):
    """时间窗与 user 档叠加：member 只看自己的、且在窗内 —— 两条谓词都得在。

    同 org 两个 member 各两种年龄（A: 0d/10d，B: 3d/45d），`week` 窗：
      - A 档（user_id=A）→ 只剩 0d（10d 出窗、B 的行出 scope）
      - admin 档（user_id=None）→ 0d + 3d（两个用户的行都算，45d/10d 出窗）
    少了 `user_id` 谓词会让 A 看见 B 的报告；少了 `since` 会让 A 看见自己的 10d。
    """
    org = Organization(id=uuid.uuid4(), name=f"p6test-org-{uuid.uuid4().hex[:8]}")
    db_session.add(org)
    await db_session.flush()
    a = await make_identity("member", organization_id=org.id)
    b = await make_identity("member", organization_id=org.id)
    await _seed(db_session, org_id=org.id, user_id=a.user_id, ages=(0, 10), prefix="a")
    await _seed(db_session, org_id=org.id, user_id=b.user_id, ages=(3, 45), prefix="b")

    rows, total = await _list(db_session, org_id=org.id, user_id=a.user_id, range_key="week")
    assert [r.title for r in rows] == [t for t in (f"a-0d-0",)]
    assert total == 1

    admin_rows, admin_total = await _list(db_session, org_id=org.id, range_key="week")
    assert {r.user_id for r in admin_rows} == {a.user_id, b.user_id}
    assert admin_total == 2


async def test_other_organizations_rows_stay_out_of_the_window(db_session, make_identity):
    """隔壁 org 同档窗：一行都不许漏进来（时间过滤不是放行理由）。

    这条与上一条合起来才是完整形状：`since` 加在 WHERE 链上，最容易犯的错是
    "把 org 谓词写成只有时间窗分支才有"。
    """
    mine = await make_identity("admin")
    theirs = await make_identity("admin")
    await _seed(db_session, org_id=mine.organization_id, user_id=mine.user_id)
    await _seed(db_session, org_id=theirs.organization_id, user_id=theirs.user_id, prefix="other")

    rows, total = await _list(db_session, org_id=mine.organization_id, range_key="all")
    assert total == len(AGES_IN_DAYS)
    assert all(not r.title.startswith("other-") for r in rows), "跨 org 漏行"
