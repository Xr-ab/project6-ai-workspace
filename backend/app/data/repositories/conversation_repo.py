"""会话 / 消息的数据访问。

Repository 层约定（见 docs/02-architecture.md §4）：
    只负责"读写数据"，不做业务判断、不抛业务异常。
    查不到就返回 None / 空列表，怎么处理交给 Service 层。
    事务边界在本层：每次写操作自己 commit，Service 不用管事务。

数据隔离约定（见 docs/05-database-design.md §1.4）：
    所有查询都强制带 organization_id + user_id 过滤，不依赖上层传参是否干净。
    查不到别人的会话时返回 None（而不是报"无权限"），避免泄露 id 是否存在。
"""
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.models import Conversation, Message


# ---------------- 会话 ----------------


async def create_conversation(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    title: str = "新会话",
    model: str | None = None,
) -> Conversation:
    """新建会话。"""
    conversation = Conversation(
        organization_id=organization_id,
        user_id=user_id,
        title=title,
        model=model,
    )
    session.add(conversation)
    await session.commit()
    # created_at / updated_at 是数据库生成的，refresh 一次把它们读回来
    await session.refresh(conversation)
    return conversation


async def get_conversation(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Conversation | None:
    """按 id 取会话；不存在或不属于该组织/用户时返回 None。"""
    stmt = select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.organization_id == organization_id,
        Conversation.user_id == user_id,
    )
    return await session.scalar(stmt)


async def list_conversations(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[Conversation]:
    """会话列表，最近活跃的排前面。"""
    stmt = (
        select(Conversation)
        .where(
            Conversation.organization_id == organization_id,
            Conversation.user_id == user_id,
        )
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(await session.scalars(stmt))


async def delete_conversation(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    """删除会话（消息由外键 CASCADE 一起删）。

    返回是否真的删掉了一条 —— 上层用它判断"会话不存在"。
    """
    stmt = delete(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.organization_id == organization_id,
        Conversation.user_id == user_id,
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount > 0


async def touch_conversation(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
) -> None:
    """更新会话的 last_message_at，让会话列表能按最近活跃排序。"""
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        return
    conversation.last_message_at = func.now()
    await session.commit()


# ---------------- 消息 ----------------


async def next_seq(session: AsyncSession, *, conversation_id: uuid.UUID) -> int:
    """取会话内的下一个序号（从 1 开始）。

    序号在 (conversation_id, seq) 上有唯一约束，靠它保证消息顺序不重不乱。
    """
    stmt = select(func.coalesce(func.max(Message.seq), 0)).where(
        Message.conversation_id == conversation_id
    )
    return (await session.scalar(stmt)) + 1


async def add_message(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    seq: int,
    role: str,
    content: str,
    content_type: str = "text",
    model: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost: float = 0.0,
    error_message: str | None = None,
    citations: list | None = None,
) -> Message:
    """落一条消息。

    cost：由调用方（chat_service.save_message）按 token 现算后传入，本层只搬运。
    默认 0 与列默认一致（NOT NULL DEFAULT 0）——"不可得"不在这里表达。
    """
    message = Message(
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
        seq=seq,
        role=role,
        content=content,
        content_type=content_type,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=cost,
        error_message=error_message,
        citations=citations,
    )
    session.add(message)
    try:
        await session.commit()
    except IntegrityError:
        # next_seq 是「先查 max 再插」且无锁——同一会话并发两条消息会拿到同一个 seq，
        # 后提交者撞 (conversation_id, seq) 唯一约束。回滚、重取序号、再落一次。
        await session.rollback()
        message.seq = await next_seq(session, conversation_id=conversation_id)
        session.add(message)
        await session.commit()
    await session.refresh(message)
    return message


async def list_messages(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    limit: int = 200,
) -> list[Message]:
    """按序号正序取会话内消息（前端渲染顺序）。"""
    stmt = (
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.organization_id == organization_id,
            Message.user_id == user_id,
        )
        .order_by(Message.seq)
        .limit(limit)
    )
    return list(await session.scalars(stmt))


async def list_recent_messages(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    limit: int = 20,
) -> list[Message]:
    """取最近 limit 条消息，返回时按 seq 正序（组装模型上下文用）。

    和 list_messages 的区别（容易踩）：
        list_messages      按 seq 正序 + limit → 取"最早 N 条"，用于前端展示全量历史
        本函数              先按 seq 倒序取 N 条再反转 → 取"最近 N 条"，用于喂模型

    如果直接给 list_messages 传个小 limit 当上下文，喂给模型的永远是对话开头，
    越聊越丢最近的上下文，而且不会报错——只是回答质量悄悄变差。
    """
    stmt = (
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.organization_id == organization_id,
            Message.user_id == user_id,
        )
        .order_by(Message.seq.desc())
        .limit(limit)
    )
    rows = list(await session.scalars(stmt))
    rows.reverse()  # 倒序取到最近 N 条后翻转，恢复成时间正序
    return rows
