"""Chat 接口的请求 / 响应模型（Pydantic）。

为什么单独一层 schemas：
    路由函数只负责"接参 → 调 Service → 返回"，参数校验交给 Pydantic。
    校验不通过时 FastAPI 自动返回 422，路由里不用写 if 判断。
"""
import uuid

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """一次流式对话请求。"""

    conversation_id: uuid.UUID = Field(
        ...,
        description="会话 id（前端先调 POST /conversations 拿到，再带着它发消息）",
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="用户输入内容",
    )
    use_knowledge: bool = Field(
        False,
        description="是否检索知识库（Phase 2 RAG）。开启后先检索再回答，并返回引用来源",
    )
    use_tools: bool = Field(
        False,
        description=(
            "是否允许模型自主调用工具（Phase 3 Tool Calling）。"
            "开启后模型可自行决定查知识库、算数、读表格、查数据库，"
            "调用过程会以 tool_call 帧推给前端"
        ),
    )
