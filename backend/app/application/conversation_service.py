"""会话管理业务逻辑（Phase 1）。

分层约定（见 docs/02-architecture.md §4）：
    路由   = 接参 → 调 Service → 返回（不做业务判断）
    Service= 业务判断、抛业务异常（本文件）
    Repository = 只读写数据，查不到返回 None

身份从哪来（Phase 8a 换源）：
    身份从 router 的 CurrentUser 传入（8a）：本层不再自取身份，
    各函数收 keyword-only 的 organization_id / user_id 进参。
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ConversationNotFoundError
from app.data.models import Conversation, Message, ToolCall
from app.data.repositories import conversation_repo, tool_call_repo


async def create_conversation(
    session: AsyncSession,
    *,
    title: str,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Conversation:
    """新建会话，返回落库后的会话对象（已带上数据库生成的时间戳）。"""
    return await conversation_repo.create_conversation(
        session,
        organization_id=organization_id,
        user_id=user_id,
        title=title,
        model=settings.llm_model,
    )


async def list_conversations(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[Conversation]:
    """会话列表，最近活跃的排前面。"""
    return await conversation_repo.list_conversations(
        session,
        organization_id=organization_id,
        user_id=user_id,
        limit=limit,
        offset=offset,
    )


async def get_conversation_detail(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[Conversation, list[Message], list[ToolCall]]:
    """取会话详情 + 历史消息 + 工具调用记录（前端刷新恢复对话）。

    工具调用一起返回，是为了让"刷新后重放调用过程"只需要一个请求。
    单独开一个 GET /conversations/{id}/tool-calls 的话，每次切换会话都要多发
    一次请求，而调用记录本来就只属于这个会话，天然该跟着详情走。
    """
    conversation = await conversation_repo.get_conversation(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
    )
    # Repository 查不到就是 None（它不抛异常），在这里翻译成业务异常。
    # "不存在"和"不属于当前用户"走同一个分支 —— 不泄露 id 是否存在。
    if conversation is None:
        raise ConversationNotFoundError()

    messages = await conversation_repo.list_messages(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
    )
    # 只按 conversation_id 过滤即可：会话归属已经在上面校验过了，
    # 拿不到不属于自己的 conversation_id（越权会在上面那步就抛 404）
    tool_calls = await tool_call_repo.list_tool_calls(
        session, conversation_id=conversation_id
    )
    return conversation, messages, tool_calls


async def delete_conversation(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """删除会话（消息由外键 ON DELETE CASCADE 一起删）。"""
    deleted = await conversation_repo.delete_conversation(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
    )
    # 删除用的是 DELETE ... WHERE，没有报错只代表"SQL 执行成功"，
    # 要看 rowcount 才知道到底删没删到东西。
    if not deleted:
        raise ConversationNotFoundError()
