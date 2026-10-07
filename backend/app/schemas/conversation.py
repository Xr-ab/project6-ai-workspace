"""会话 / 消息接口的请求 / 响应模型（Pydantic）。

为什么响应模型不直接用 ORM 对象：
    ORM 对象带着 organization_id / user_id / structured 等内部字段，
    直接返回会把内部数据结构和敏感字段一起暴露出去。
    响应模型是"对外契约"——只有这里列出的字段才会出现在 JSON 里。
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    """新建会话请求。"""

    title: str = Field("新会话", min_length=1, max_length=200)


class ConversationOut(BaseModel):
    """会话摘要（列表 / 详情共用）。"""

    # from_attributes=True：允许直接从 SQLAlchemy 对象构造（按属性名取值），
    # 省掉手写字段搬运。注意它只按名字取，不会自动过滤敏感字段——
    # 敏感字段是靠"这里没声明"来挡掉的。
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    model: str | None
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    """一条消息。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    seq: int
    role: str
    content: str
    content_type: str
    prompt_tokens: int
    completion_tokens: int
    error_message: str | None
    # Phase 2 RAG 引用来源：一条 assistant 消息可能引用多份资料。
    # 前端用它渲染"来源引用块"（[1] 员工手册 第2页）。普通对话没有，为 None。
    citations: list | None
    created_at: datetime


class ToolCallOut(BaseModel):
    """一次工具调用的展示信息（Phase 3）。

    这个形状有两个消费者，且**必须是同一个**：
        /chat/stream 的 tool_call 帧（实时，调用完一条立刻推一条）
        /conversations/{id} 的 tool_calls 字段（刷新页面后重放调用过程）
    两边字段名一样，前端就只需要一个类型、不需要转换函数；
    加字段时两边一起加（缺字段这里会直接报校验错，不会静默漏掉）。

    只给"能展示的元信息"，不给完整结果（output_json 在库里，需要时另取）：
    sql_query 一次能返回 200 行，把结果再推一遍前端只是搬第三次数据。
    """

    # 挂在哪条助手消息上。实时帧里恒为 None —— 推帧时助手消息还没落库，
    # 还没 id；历史里靠它把调用过程放回对应的那条回复下面
    message_id: uuid.UUID | None = None
    name: str
    tool_type: str
    # 模型这次到底查了什么（SQL 文本 / 查询词）。存的就是模型给的原始参数
    args: dict | None
    ok: bool
    error: str | None
    rows: int | None
    duration_ms: int | None
    truncated: bool


class ConversationDetailOut(ConversationOut):
    """会话详情 = 会话本身 + 消息列表（前端刷新页面时用它恢复对话）。"""

    messages: list[MessageOut] = Field(default_factory=list)
    # 本会话的全部工具调用（按时间正序）。不塞进 MessageOut 里：
    # 一条消息可能调多次工具，嵌套会让 MessageOut 变成一个复合结构，
    # 而前端只要按 message_id 分组就能挂回对应的消息
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
