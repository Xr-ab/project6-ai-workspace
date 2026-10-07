/** Chat 域 API：会话增删查 + 流式对话。页面不直接拼 URL，只调这里的方法。 */
import { ApiError, request, streamRequest } from './client';
import { parseSSEStream } from './sse';
import type { Citation, Conversation, ConversationDetail, ToolCallInfo } from '../types/chat';

/** 会话列表（后端已按最近活跃倒序） */
export function listConversations(limit = 50, offset = 0): Promise<Conversation[]> {
  return request<Conversation[]>('/conversations', { params: { limit, offset } });
}

/** 新建会话 */
export function createConversation(title = '新会话'): Promise<Conversation> {
  return request<Conversation>('/conversations', { method: 'POST', body: { title } });
}

/** 会话详情 + 历史消息（刷新页面恢复对话用） */
export function getConversation(id: string): Promise<ConversationDetail> {
  return request<ConversationDetail>(`/conversations/${id}`);
}

/** 删除会话（后端 204，无响应体） */
export function deleteConversation(id: string): Promise<void> {
  return request<void>(`/conversations/${id}`, { method: 'DELETE' });
}

export interface StreamChatHandlers {
  /** 收到引用来源。一定排在第一个 onDelta 之前，且未开知识库时不触发 */
  onCitations: (citations: Citation[]) => void;
  /** 收到一段增量文本 */
  onDelta: (text: string) => void;
  /**
   * 收到一次工具调用记录（Phase 3）。一次回复可能触发多次，
   * 且**总是出现在该工具跑完之后**（含耗时、行数等结果信息）。
   * 未开工具开关时不触发
   */
  onToolCall: (toolCall: ToolCallInfo) => void;
  /** 正常结束（收到 [DONE]） */
  onDone: () => void;
  /** 失败（错误帧 / 断流）。message 已是可直接展示的中文文案 */
  onError: (message: string) => void;
}

/**
 * 发一条消息并流式接收回复。
 *
 * 三个回调保证「必然只触发其中一个终止回调」（onDone / onError），
 * 上层不需要自己判断该不该结束 loading。
 *
 * 抛错的情况只有两种：请求还没建立就失败（404/422/网络），
 * 或者被 AbortSignal 中止 —— 这两种由调用方 catch。
 */
export async function streamChat(
  conversationId: string,
  message: string,
  /** 是否检索知识库。开启后先检索再回答，并推 citations 帧 */
  useKnowledge: boolean,
  /** 是否允许模型自主调用工具（Phase 3）。开启后模型可自行决定查什么 */
  useTools: boolean,
  signal: AbortSignal,
  handlers: StreamChatHandlers,
): Promise<void> {
  // 这里用 fetch + ReadableStream 而不是 EventSource：EventSource 只能发 GET，
  // 拿不到请求体，会话 id 和消息正文就没法传
  const response = await streamRequest(
    '/chat/stream',
    {
      conversation_id: conversationId,
      message,
      use_knowledge: useKnowledge,
      use_tools: useTools,
    },
    signal,
  );

  if (!response.body) {
    throw new ApiError('服务器没有返回流式响应体', response.status);
  }

  for await (const event of parseSSEStream(response.body)) {
    switch (event.type) {
      case 'citations':
        handlers.onCitations(event.citations);
        break;
      case 'delta':
        handlers.onDelta(event.text);
        break;
      case 'tool_call':
        handlers.onToolCall(event.toolCall);
        break;
      case 'done':
        handlers.onDone();
        return;
      case 'error':
        // 关键：错误帧本身就是终止信号 —— 后端出错时不会再推 [DONE]。
        // 如果这里继续等 [DONE]，报错时 UI 会永远转圈
        handlers.onError(event.message);
        return;
    }
  }

  // 走到这里说明流被关闭了，但既没收到 [DONE] 也没收到错误帧：
  // 属于连接被中途掐断（网络抖动 / 后端进程退出）。已渲染的内容可能不完整，
  // 按失败处理，而不是假装成功
  handlers.onError('连接意外中断，回复可能不完整，请重试');
}
