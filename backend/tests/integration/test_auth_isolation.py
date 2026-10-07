"""多租户隔离的集成面（spec §5.1）：org 过滤是**真 SQL** 在挡，不是 mock 在挡。

为什么这层非集成不可：task_repo 的每条读都带 organization_id × user_id 双条件，
这类"漏一个 where 就静默越权"的形状，unit 层的假 session 只会断言"我传了什么参数"，
断言不了"数据库真的一条都不给"。

身份口径（Task 3 纪律 1）：不调 /auth/register、不跑 seed_dev_users，
现造 org+user 再由 create_access_token 直铸 token——身份链上三要素与真登录同形，
而测试里不出现任何口令字面量。

路由口径（Task 3 实施订正）：agents 路由器挂在 /api/v1 下、自身前缀是 /agents
（main.py:128 + agents.py:45），所以读面实际路径是 /api/v1/agents/tasks 等，
简报正文里的 /api/v1/tasks 是简写，这里按真实路由写。
"""
import uuid
from dataclasses import dataclass

import pytest
from app.data.models import Task, TaskRun
from tests.conftest import Identity

pytestmark = pytest.mark.db


@dataclass
class Seeded:
    task: Task
    run: TaskRun


async def _seed_task(session, *, who: Identity, status: str = "completed") -> Seeded:
    """给这个身份名下一条 task + 一条 run（两个端点都要 JOIN 得到它们）。"""
    task = Task(
        id=uuid.uuid4(), organization_id=who.organization_id, user_id=who.user_id,
        title="p6test 隔离探针", question="这条属于谁的库？",
        task_type="agent_analysis", status=status,
    )
    session.add(task)
    await session.flush()
    run = TaskRun(id=uuid.uuid4(), task_id=task.id, run_no=1, status=status, progress=100)
    session.add(run)
    await session.commit()
    return Seeded(task=task, run=run)


def _auth(identity: Identity) -> dict[str, str]:
    return {"Authorization": f"Bearer {identity.token}"}


async def test_list_returns_only_own_org_rows(db_session, api_client, make_identity):
    a = await make_identity()
    b = await make_identity()
    mine = await _seed_task(db_session, who=a)
    theirs = await _seed_task(db_session, who=b)

    resp = await api_client.get("/api/v1/agents/tasks", headers=_auth(a))
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert str(mine.task.id) in ids
    assert str(theirs.task.id) not in ids


async def test_cross_org_detail_is_404_not_403(db_session, api_client, make_identity):
    """"不存在"与"别人的"同形 404（exceptions.py:108 的裁定：不给探测 id 是否存在的口子）。
    这条针守的是"哪天有人把它改成 403 顺手泄露了存在性"。"""
    a = await make_identity()
    b = await make_identity()
    theirs = await _seed_task(db_session, who=a)

    resp = await api_client.get(f"/api/v1/agents/tasks/{theirs.task.id}", headers=_auth(b))
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "TASK_404001"


async def test_cross_org_run_and_trace_are_404(db_session, api_client, make_identity):
    """轮询面两条：GET /task-runs/{id} 与 /task-runs/{id}/trace 的归属校验
    都落在 task_repo.get_task_run 的 JOIN 上（agent_task_service:383/450 实读）。"""
    a = await make_identity()
    b = await make_identity()
    theirs = await _seed_task(db_session, who=a)

    for path in (f"/api/v1/agents/task-runs/{theirs.run.id}",
                 f"/api/v1/agents/task-runs/{theirs.run.id}/trace"):
        resp = await api_client.get(path, headers=_auth(b))
        assert resp.status_code == 404, f"{path} → {resp.status_code} {resp.text}"
        assert resp.json()["code"] == "TASK_404001"


async def test_same_org_other_user_is_also_404(db_session, api_client, make_identity):
    """双过滤的另一半：同 org 不同 user 也挡得住（Phase 9a 的针是 org 腿，这条补 user 腿）。

    真行为在 task_repo.get_task(..., organization_id, user_id)（agent_task_service 详情链）
    与 list_tasks 的 user_id 过滤（agents.py:187-188）上。上一版把 task 种在了**第三个 org**
    （make_identity 每次都新建 org），404 其实来自 org 腿 ⇒ user 腿零覆盖。
    这一版把 colleague 建成「与 owner 同一 org 的另一名 user」（make_identity(organization_id=...)），
    于是 org 腿必然通过，只有 user 腿能挡 —— 详情 404 且列表也看不见。"""
    owner = await make_identity()
    colleague = await make_identity(organization_id=owner.organization_id)  # 同 org，不同 user
    theirs = await _seed_task(db_session, who=owner)

    # 前提坐实：同 org、异 user —— 挡下来的只可能是 user 腿，不是 org 腿。
    assert colleague.organization_id == owner.organization_id
    assert colleague.user_id != owner.user_id

    # user 腿（详情）：同事读不到 owner 名下那条，即便同 org。
    detail = await api_client.get(
        f"/api/v1/agents/tasks/{theirs.task.id}", headers=_auth(colleague)
    )
    assert detail.status_code == 404, detail.text
    assert detail.json()["code"] == "TASK_404001"

    # user 腿（列表）：同组织同事的列表里也不该出现 owner 的行。
    mate_rows = await api_client.get("/api/v1/agents/tasks", headers=_auth(colleague))
    assert mate_rows.status_code == 200, mate_rows.text
    assert str(theirs.task.id) not in {row["id"] for row in mate_rows.json()}

    # 正证：org 腿放行、user 腿才是那道闸 —— owner 自己读得到（否则上面 404 是端点坏不是隔离）。
    own_rows = await api_client.get("/api/v1/agents/tasks", headers=_auth(owner))
    assert own_rows.status_code == 200, own_rows.text
    assert str(theirs.task.id) in {row["id"] for row in own_rows.json()}


@pytest.mark.parametrize(
    "header",
    [
        None,                                    # 完全没带
        {"Authorization": "Bearer "},             #  schemes 对了但凭证为空
        {"Authorization": "Bearer not.a.jwt"},    # 形状对但验签不过
        {"Authorization": "Basic YWxpY2U6MTIz"},  #  scheme 不对 → HTTPBearer 给 None → 401
    ],
    ids=["missing", "empty-credential", "garbage-token", "wrong-scheme"],
)
async def test_401_matrix(api_client, make_identity, header):
    """四路都必须是 401 AUTH_401001（deps.py:24-30 的三条出口 + 06 §5 的探测防护）。"""
    identity = await make_identity()
    headers = {} if header is None else header
    resp = await api_client.get("/api/v1/agents/tasks", headers=headers)
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == "AUTH_401001"
    # 反证 401 是"这一发没带对凭证"而不是端点本身坏了：同一端点带真 token 必 200。
    # （上一版写 `assert identity.token` —— 刚铸的 token 恒真，永远不会红，是空断言。）
    good = await api_client.get("/api/v1/agents/tasks", headers=_auth(identity))
    assert good.status_code == 200, good.text


async def test_token_org_claim_is_not_the_authority(db_session, api_client, make_identity):
    """身份换源（8a T5）的机制面，实测订正版。

    简报原文期望"伪造 org claim ⇒ 200 且读到自己 org 的行"。实读裁定这条路不通：
    list_tasks 的查询确实用 DB 行上的 organization_id（agents.py:184 `user.organization_id`，
    端点从不读 token 的 org claim），但 get_current_user 在 deps.py:34-36 先立了一道
    一致性闸——token.claim.org 与 DB 的 user.organization_id 对不上就直接 InvalidTokenError。
    ⇒ 拿真 user + 别人的 org 铸 token，落点是 401 AUTH_401001，而不是 200。
    这比"claim 只是缓存"更强：伪造 claim 不但换不来越权，根本进不了端点。
    断言从"200 读自己行"改成"401 拒伪造"，钉的是这条真实安全性质（简报与本仓实现不一致，
    按实现为准；见 report 的 concern 一节）。
    """
    from app.core.security import create_access_token
    from tests.conftest import Identity

    a = await make_identity()
    b = await make_identity()
    mine = await _seed_task(db_session, who=a)

    # 真 token（claim == DB org）：读得到自己 org 的那条 ⇒ 证明读面用的是 DB 归属
    own = await api_client.get("/api/v1/agents/tasks", headers=_auth(a))
    assert own.status_code == 200, own.text
    assert str(mine.task.id) in {row["id"] for row in own.json()}

    # 伪造 token（a 的 user，b 的 org）：deps.py:34 的一致性闸当场拒之 401
    forged = Identity(organization_id=a.organization_id, user_id=a.user_id,
                      token=create_access_token(a.user_id, b.organization_id, "member"))
    resp = await api_client.get("/api/v1/agents/tasks", headers=_auth(forged))
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == "AUTH_401001"
