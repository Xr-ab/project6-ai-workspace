"""角色→工具权限静态表（app/ai/tools/permissions.py:7 / 18）。

「新增工具不进表 = member 不可用」是默认拒绝，不是遗漏；这条只能靠针守着。
"""
from app.ai.tools.permissions import MEMBER_PERMISSIONS, ROLE_TOOL_PERMISSIONS, tool_allowed


def test_admin_bypass_is_expressed_as_none_not_an_enum():
    assert ROLE_TOOL_PERMISSIONS["admin"] is None
    assert tool_allowed("admin", "tools.data_statistics") is True
    assert tool_allowed("admin", "tools.不存在的工具") is True


def test_member_face_is_the_documented_matrix():
    for perm in ("tools.rag_search", "tools.document_retriever", "tools.web_search",
                 "tools.query_customer", "tools.query_product", "tools.query_sales",
                 "tools.query_stock_levels", "tools.query_stock_movements",
                 "tools.suggest_replenishment"):
        assert tool_allowed("member", perm) is True, perm


def test_data_analysis_family_stays_admin_only():
    assert tool_allowed("member", "tools.data_statistics") is False


def test_unknown_tool_and_unknown_role_both_deny():
    assert tool_allowed("member", "tools.brand_new_tool") is False
    assert tool_allowed("editor", "tools.rag_search") is False    # 表外角色 → 空集 → 全拒
    assert tool_allowed("", "tools.rag_search") is False


def test_member_set_contains_no_admin_only_family_members():
    # tools.data_statistics 是 permissions.py 里真实存在的 admin-only 名；另两枚
    # tools.write_back / tools.grant_role 全仓不存在——保留它们是刻意的形状：钉「一枚尚未
    # 发明的权限永远不该溜进 member 集」，将来谁把它们加进 MEMBER_PERMISSIONS 这条立刻红。
    admin_only = {"tools.data_statistics", "tools.write_back", "tools.grant_role"}
    assert MEMBER_PERMISSIONS.isdisjoint(admin_only)
