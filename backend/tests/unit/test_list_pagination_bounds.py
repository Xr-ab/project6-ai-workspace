"""分页参数边界针：列表端点的 `limit` / `page_size` 上下界必须在接口契约上就有。

失效模式（本针抓到的真 bug，2026-10-03）：`GET /api/v1/agents/tasks` 声明的是
`Query(50, le=200)` —— 少了 `ge=1`，而三个兄弟端点（documents / conversation /
evaluations）本来就是 `Query(50, ge=1, le=200)`。少了这一位之后：

    ?limit=-1  →  参数校验全过  →  task_repo 的 `.limit(-1)`  →  PG 抛
                  「LIMIT must not be negative」  →  **500**

而 docs/06 §7.1 承诺「过滤参数一律在服务端校验，非法值 422」。所以这不是"少一层防御"，
是**接口对调用方撒了谎**：文档说会拒，实际是炸。

**为什么打 `app.openapi()` 而不是路由对象**：本仓的 FastAPI 版本把 `include_router`
变成了惰性包装（`app.routes` 里是 8 个 `_IncludedRouter`，`path` 为 None），走路由对象
要自己拼 prefix；同一个仓库里已经有一条探针这么错过一次（取到空集 → 断言"全绿" → 假绿）。
`app.openapi()` 是**同一份声明的对外投影**、45 条路径齐全，既避开惰性包装，又顺带把
"下界有没有说给调用方"这件事一起验了 —— 契约与运行时是同一个 `FieldInfo` 的两个投影。

覆盖口径：凡带**默认值**的 `limit` / `page_size`（即分页参数，必填参数如
`/evaluations/compare` 的 base/head 不在此列）都必须同时有下界与上界 ——
下界防负值 500，上界防一次把整张表拉出来。
"""
import pytest

from app.main import app


def _pagination_params() -> dict[str, dict]:
    """{路径?参数名: schema}：把每个分页参数的**对外边界声明**取出来。

    取 `schema` 而不是 `required` 之类的旁支：`minimum` / `maximum` 就是 FastAPI
    从 `Query(default, ge=, le=)` 投影出来的那两位，正是被测对象本身。
    """
    found: dict[str, dict] = {}
    for path, methods in app.openapi()["paths"].items():
        for method, operation in methods.items():
            for param in operation.get("parameters", []):
                name = param.get("name")
                if name not in ("limit", "page_size"):
                    continue
                if "schema" not in param or "default" not in param["schema"]:
                    continue  # 必填参数不是分页旋钮
                found[f"{method.upper()} {path}?{name}"] = param["schema"]
    return found


def test_pagination_parameter_census_is_not_empty():
    """仪器自检：**先证明这份清单不是空的**，再拿它下结论。

    取到空集时下面两组断言会逐条"通过"，那是假绿 —— 一条什么都没查的针。
    所以清单本身钉成字面量：新增列表端点（新的一档分页旋钮）时这条会红，
    逼作者回来确认新端点也有上下界，而不是让新端点悄悄落在针的覆盖之外。
    """
    found = _pagination_params()
    assert set(found) == {
        "GET /api/v1/agents/tasks?limit",
        "GET /api/v1/conversations?limit",
        "GET /api/v1/documents?limit",
        "GET /api/v1/evaluations/datasets?limit",
        "GET /api/v1/evaluations/runs?limit",
        "GET /api/v1/auth/audit-log?page_size",
        # Phase 9b 新增的报告列表口。**这条清单断言刚刚真的红过一次**（第一版没这行），
        # 正好证明它不是形式主义：新加一个分页端点，这份覆盖清单必须回来确认一次。
        "GET /api/v1/reports?limit",
    }, f"分页参数清单变了，本针覆盖面需同步：{sorted(found)}"


@pytest.mark.parametrize("name", sorted(_pagination_params()))
def test_pagination_has_a_lower_bound(name):
    """下界（minimum）：这一位就是「`?limit=-1` 是 422 还是 500」的全部区别。"""
    schema = _pagination_params()[name]
    assert "minimum" in schema, (
        f"{name} 缺下界：负数会一路进 SQL LIMIT，PG 直接 500（docs/06 §7.1 承诺 4xx）"
    )
    assert schema["minimum"] >= 1, f"{name} 的下界是 {schema['minimum']}，应当 ≥1"


@pytest.mark.parametrize("name", sorted(_pagination_params()))
def test_pagination_has_an_upper_bound(name):
    """上界（maximum）：没有它，一次请求就能把整张表拉出来。"""
    schema = _pagination_params()[name]
    assert "maximum" in schema, f"{name} 缺上界：单次请求可拉全表"
    assert schema["maximum"] <= 200, f"{name} 的上界是 {schema['maximum']}，超过 docs/06 §7.1 的 200"
