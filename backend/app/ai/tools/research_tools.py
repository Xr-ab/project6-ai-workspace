"""调研工具（Phase 3 / docs/04-tool-design.md §2.3）：web_search。

为什么用第三方检索 API（博查 BoCha）而不是自己抓网页：
    "搜网页"看起来只是发个 GET，实际要做的是：搜索引擎选型、结果去重、
    正文提取（不同站点 DOM 完全不同）、反爬对抗、robots 合规。
    这些和本项目的目标（AI 应用工程）无关，属于基础设施。
    博查把"网页正文摘录"直接作为结构化结果返回，正好是给模型吃的形式；
    并且国内可直连（和 DeepSeek 的选型同源，不用挂代理）——
    project5 用的就是它，key 可复用。

为什么结果要截断正文：
    检索结果的正文动辄几千字，一次 5 条就能把上下文吃掉一大半。
    留标题 + 短摘录 + URL 足够让模型判断"这条有没有用"；
    真要细读，Phase 4 之后可以按 URL 再做一次精读（v1 不做）。

为什么 key 没配也要**注册这个工具**（返回可读错误，而不是不注册）：
    不注册的话，模型完全看不到这个工具，用户在"要不要联网查"上得不到任何反馈；
    注册并返回"未配置 key"则给出明确动作（去 .env 填 BOCHA_API_KEY），
    模型也能立刻改用 rag_search。工具集在运行期保持稳定，
    也避免"换台机器工具数量就变了"这类难查的差异。
"""
import httpx
from pydantic import BaseModel, Field

from app.ai.tools.base import Tool, ToolContext, ToolResult
from app.ai.tools.registry import register
from app.core.config import settings

BOCHA_URL = "https://api.bochaai.com/v1/web-search"
TIMEOUT_S = 15.0  # 外部服务必须设超时：不设的话对端挂着不动会一直占住请求
EXCERPT_LIMIT = 500  # 单条结果正文摘录上限（字符）

# 博查在 HTTP 200 之外还会用响应体里的 code 表示业务错误（余额不足、权限不够等），
# 两者都要看：只看 HTTP 状态码的话，"配额用完"会被当成"没搜到结果"，很难查。
OK_CODES = (200, None)


class WebSearchArgs(BaseModel):
    query: str = Field(
        description="检索关键词。给简短的关键词组合（如 '2026 中国 SaaS 市场规模'），不要整段问句"
    )
    max_results: int = Field(default=5, ge=1, le=10, description="返回多少条，默认 5")


async def web_search(args: WebSearchArgs, ctx: ToolContext) -> ToolResult:
    """联网检索，返回标题 / 摘要 / 链接。"""
    if not settings.bocha_api_key:
        return ToolResult(
            ok=False,
            error=(
                "联网检索未配置（缺少 BOCHA_API_KEY）。"
                "请改用 rag_search 查企业知识库；若确需联网，请联系管理员配置检索服务。"
            ),
        )

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.post(
                BOCHA_URL,
                headers={"Authorization": f"Bearer {settings.bocha_api_key}"},
                # 博查入参：query 必填；summary=True 才会返回正文摘录；count 是条数
                json={"query": args.query, "summary": True, "count": args.max_results},
            )
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPStatusError as exc:
        # 把状态码和响应片段带出来：401 是 key 错、403 是没权限/余额，都靠它区分
        detail = exc.response.text[:200]
        return ToolResult(
            ok=False, error=f"检索服务返回 {exc.response.status_code}：{detail}"
        )
    except httpx.HTTPError as exc:
        return ToolResult(ok=False, error=f"检索服务请求失败：{type(exc).__name__}: {exc}")

    if payload.get("code") not in OK_CODES:
        return ToolResult(
            ok=False,
            error=f"检索服务返回错误：code={payload.get('code')} msg={payload.get('msg')}",
        )

    # 响应结构：data.webPages.value[]；正文优先取 summary（要了 summary=True 才有），退回 snippet
    pages = payload.get("data", {}).get("webPages", {}).get("value", [])
    results = [
        {
            "title": item.get("name", ""),
            "url": item.get("url", ""),
            # 截断而不是丢弃：模型需要靠开头判断这条值不值得引用
            "content": (item.get("summary") or item.get("snippet") or "")[:EXCERPT_LIMIT],
        }
        for item in pages
    ]
    if not results:
        # 空结果 ≠ 工具失败（口径对齐 rag_search，见其注释）：
        # 搜索引擎正常应答但这个词就是没命中，是**业务结果**。
        # 报 ok=False 的实害：模型把"搜不到"当成"调用坏了"，换个大小写原样重试几轮；
        # 而真正该做的（换关键词再试一次 / 承认查不到并告知用户）恰恰没人做。
        return ToolResult(
            ok=True,
            data={
                "query": args.query,
                "results": [],
                "note": (
                    f"没有检索到与 '{args.query}' 相关的结果。"
                    "这通常说明关键词不常见——可换同义词或更通用的说法再搜一次；"
                    "仍没有就如实告诉用户没查到，不要编造答案。"
                ),
            },
            rows=0,
        )

    return ToolResult(
        ok=True,
        data={
            "query": args.query,
            "results": results,
            "note": "内容为搜索引擎返回的网页摘录，可能过期或与事实不符；引用时请给出 URL",
        },
        rows=len(results),
        truncated=len(results) == args.max_results,
    )


register(
    Tool(
        name="web_search",
        description=(
            "联网检索公开网页，返回标题、摘要和链接。\n"
            "何时用：问题涉及**企业外部**的公开信息，且知识库和业务库都答不了 —— "
            "行业动态、政策法规、竞品公开信息、通用常识的最新版本。\n"
            "何时不用：企业内部数据（客户/产品/销售用 query_* 或 sql_query）、"
            "企业上传的文档（用 rag_search）。企业知识库能答的不要联网。"
        ),
        args_schema=WebSearchArgs,
        executor=web_search,
        tool_type="research",
    )
)