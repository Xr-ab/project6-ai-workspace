"""角色→工具权限静态表（06 §6.2/§6.3 的字面兑现；映射留行不造管理面）。

06 §6.3 默认矩阵：member 可 rag_search/web_search/query_*；数据分析类 admin。
未列名的两个按族归（Global 裁定 6）：document_retriever 随 rag 家族给 member，
data_statistics 随数据分析归 admin。新增工具不进表 = member 不可用（默认拒绝）。
"""
MEMBER_PERMISSIONS = {
    "tools.rag_search", "tools.document_retriever", "tools.web_search",
    "tools.query_customer", "tools.query_product", "tools.query_sales",
    # Phase 10：外部库存域三把（只读、按组织过滤）。不进表 = member 静默 403 + 刷 tool_denied 审计。
    # 同 role 同权限是**诚实限制**不是设计（spec §6）：外部性是进程与数据源的外部性。
    "tools.query_stock_levels", "tools.query_stock_movements", "tools.suggest_replenishment",
}

ROLE_TOOL_PERMISSIONS: dict[str, set[str] | None] = {"member": MEMBER_PERMISSIONS, "admin": None}


def tool_allowed(role: str, permission: str) -> bool:
    perms = ROLE_TOOL_PERMISSIONS.get(role, set())  # 未知角色 → 空集 → 全拒
    return perms is None or permission in perms
