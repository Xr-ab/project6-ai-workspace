"""会话管理 API（Phase 1）：新建 / 列表 / 详情 / 删除。

和 chat.py 的分工：
    chat.py  = "发消息" + SSE 流式返回（长连接、text/event-stream）
    本文件   = 会话本身的增删查（普通 JSON 请求响应）

为什么这些接口不放在 chat.py 里：
    两者的"响应形态"完全不同（流式 vs 一次性 JSON），混在一起会让
    chat.py 同时承担两种返回协议，排查问题时不好定位。
"""
import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.db import get_db
from app.data.models import ToolCall
from app.schemas.conversation import (
    ConversationCreate,
    ConversationDetailOut,
    ConversationOut,
    MessageOut,
    ToolCallOut,
)
from app.api.deps import CurrentUser
from app.application import conversation_service

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _tool_call_out(row: ToolCall) -> ToolCallOut:
    """tool_calls 表的一行 → 前端展示模型。

    两个字段是"派生"而不是同名搬运：
        ok    ← status == "ok"（库里存 ok/error 两个值，前端只关心成功与否）
        args  ← input_json（存进去的就是模型给的原始参数，见 chat_service）
    """
    return ToolCallOut(
        message_id=row.message_id,
        name=row.tool_name,
        tool_type=row.tool_type,
        args=row.input_json,
        ok=row.status == "ok",
        error=row.error_message,
        rows=row.rows_returned,
        duration_ms=row.duration_ms,
        truncated=row.truncated,
    )


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    user: CurrentUser,
    body: ConversationCreate,
    session: AsyncSession = Depends(get_db),
) -> ConversationOut:
    """新建会话。"""
    conversation = await conversation_service.create_conversation(
        session, title=body.title,
        organization_id=user.organization_id, user_id=user.id,
    )
    return ConversationOut.model_validate(conversation)


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    user: CurrentUser,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> list[ConversationOut]:
    """会话列表（前端侧边栏用）。"""
    conversations = await conversation_service.list_conversations(
        session, organization_id=user.organization_id, user_id=user.id,
        limit=limit, offset=offset,
    )
    return [ConversationOut.model_validate(c) for c in conversations]


@router.get("/{conversation_id}", response_model=ConversationDetailOut)
async def get_conversation(
    user: CurrentUser,
    conversation_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> ConversationDetailOut:
    """会话详情 + 历史消息 + 工具调用记录（刷新页面恢复对话）。"""
    conversation, messages, tool_calls = (
        await conversation_service.get_conversation_detail(
            session, conversation_id=conversation_id,
            organization_id=user.organization_id, user_id=user.id,
        )
    )
    # 详情 = 会话字段 + messages + tool_calls，所以先按摘要模型 dump 出字段再补两块，
    # 不用手写一遍字段列表（字段加了自动跟着走）
    return ConversationDetailOut(
        **ConversationOut.model_validate(conversation).model_dump(),
        messages=[MessageOut.model_validate(m) for m in messages],
        tool_calls=[_tool_call_out(tc) for tc in tool_calls],
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    user: CurrentUser,
    conversation_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> None:
    """删除会话（消息一起删）。"""
    await conversation_service.delete_conversation(
        session, conversation_id=conversation_id,
        organization_id=user.organization_id, user_id=user.id,
    )
