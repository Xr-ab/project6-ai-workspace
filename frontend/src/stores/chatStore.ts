/**
 * Chat 页状态（Zustand）。
 *
 * 为什么用 Zustand 而不是 Context：流式回复时每收到一个增量就要更新一次消息内容，
 * 是高频细粒度更新。Context 的 value 一变，整棵子树都会重渲染；
 * Zustand 的 set 只通知订阅了对应切片的组件。
 *
 * 职责边界：本 store 只管"状态 + 会话增删查"，流式请求的编排放在
 * hooks/useSSE.ts —— 那里需要 AbortController，属于 React 生命周期范畴。
 */
import { create } from 'zustand';

import * as chatApi from '../api/chat';
import { ApiError, toReadableError } from '../api/client';
import type {
  ChatMessage,
  Citation,
  Conversation,
  Message,
  MessageStatus,
  ToolCallInfo,
} from '../types/chat';

/** 后端历史消息 → 前端消息（库里的消息都是已经落库完成的，直接是终态） */
function toChatMessage(message: Message, toolCalls?: ToolCallInfo[]): ChatMessage {
  return {
    id: message.id,
    // 后端 role 目前是 user/assistant，未来可能有 system/tool，非 user 一律按助手侧渲染
    role: message.role === 'user' ? 'user' : 'assistant',
    content: message.content,
    status: message.error_message ? 'error' : 'done',
    error: message.error_message,
    // 引用随消息落库了，所以刷新页面恢复对话时来源块照样在。
    // null → undefined：两者在 UI 上都是"不渲染"，统一成 undefined 少一种分支
    citations: message.citations ?? undefined,
    toolCalls,
  };
}

/**
 * 把会话级的工具调用按 message_id 分组。
 *
 * 后端返回的是平铺的一串（每次调用带自己的 message_id），前端要展示在"它所属的
 * 那条回复"下面，所以这里聚合一次。同一 message_id 下按接口给的顺序追加即可 ——
 * 后端已经按 (created_at, started_at) 排好，正好是真实的调用先后。
 */
function groupToolCallsByMessage(toolCalls: ToolCallInfo[] | undefined) {
  const grouped = new Map<string, ToolCallInfo[]>();
  for (const toolCall of toolCalls ?? []) {
    if (!toolCall.message_id) continue;
    const list = grouped.get(toolCall.message_id);
    if (list) {
      list.push(toolCall);
    } else {
      grouped.set(toolCall.message_id, [toolCall]);
    }
  }
  return grouped;
}

/** 用首条消息派生标题：后端 title 有 200 字上限，这里截断避免超长 */
function deriveTitle(text: string): string {
  return text.length > 30 ? `${text.slice(0, 30)}…` : text;
}

interface ChatState {
  // ---- 会话列表（左侧栏）----
  conversations: Conversation[];
  conversationsLoading: boolean;
  conversationsError: string | null;

  // ---- 当前会话与消息流 ----
  activeConversationId: string | null;
  messages: ChatMessage[];
  messagesLoading: boolean;
  messagesError: string | null;

  /** 是否有正在进行的流式回复。全局只允许一条，用来禁用输入框 */
  isStreaming: boolean;

  loadConversations: () => Promise<void>;
  /** 新建会话（带标题）。用于"发第一条消息时懒创建" */
  createConversation: (title: string) => Promise<Conversation | null>;
  /** 点击"新建会话"：只是清空当前视图回到草稿态，不落库（避免产生一堆空会话） */
  startNewConversation: () => void;
  selectConversation: (id: string) => Promise<void>;
  deleteConversation: (id: string) => Promise<void>;

  // ---- 流式过程中的本地状态更新 ----
  appendMessage: (message: ChatMessage) => void;
  appendDelta: (id: string, text: string) => void;
  /** 写入引用来源（citations 帧到达时调一次，早于所有文本增量） */
  setMessageCitations: (id: string, citations: Citation[]) => void;
  /** 追加一次工具调用记录（tool_call 帧到达时调一次，可能来多次） */
  appendToolCall: (id: string, toolCall: ToolCallInfo) => void;
  setMessageStatus: (id: string, status: MessageStatus, error?: string | null) => void;
  setIsStreaming: (value: boolean) => void;
}

export const useChatStore = create<ChatState>((set, get) => ({
  conversations: [],
  conversationsLoading: false,
  conversationsError: null,

  activeConversationId: null,
  messages: [],
  messagesLoading: false,
  messagesError: null,

  isStreaming: false,

  loadConversations: async () => {
    set({ conversationsLoading: true, conversationsError: null });
    try {
      const conversations = await chatApi.listConversations();
      set({ conversations, conversationsLoading: false });
    } catch (error) {
      set({ conversationsLoading: false, conversationsError: toReadableError(error) });
    }
  },

  createConversation: async (title) => {
    try {
      const conversation = await chatApi.createConversation(deriveTitle(title));
      set((state) => ({
        conversations: [conversation, ...state.conversations],
        activeConversationId: conversation.id,
        messages: [],
        messagesError: null,
      }));
      return conversation;
    } catch (error) {
      // 创建失败就没有会话可发消息，把错误抛给列表区展示
      set({ conversationsError: toReadableError(error) });
      return null;
    }
  },

  startNewConversation: () =>
    set({
      activeConversationId: null,
      messages: [],
      messagesError: null,
      messagesLoading: false,
    }),

  selectConversation: async (id) => {
    // 先清空消息再加载：否则加载期间会短暂显示上一个会话的内容
    set({
      activeConversationId: id,
      messages: [],
      messagesLoading: true,
      messagesError: null,
    });
    try {
      const detail = await chatApi.getConversation(id);
      // 竞态保护：请求返回时用户可能已经切到别的会话了，
      // 这时不能再把旧会话的消息写进去
      if (get().activeConversationId !== id) return;
      // 工具调用是平铺返回的，这里按 message_id 分回各自的助手消息
      const toolCallsByMessage = groupToolCallsByMessage(detail.tool_calls);
      set({
        messages: detail.messages.map((message) =>
          toChatMessage(message, toolCallsByMessage.get(message.id)),
        ),
        messagesLoading: false,
      });
    } catch (error) {
      if (get().activeConversationId !== id) return;
      // 404 是"会话已被删掉"这种可预期的情况，给一句更明确的提示，
      // 顺便刷新列表让侧边栏同步
      const notFound = error instanceof ApiError && error.status === 404;
      set({
        messagesLoading: false,
        messagesError: notFound ? '会话不存在，可能已被删除' : toReadableError(error),
      });
      if (notFound) void get().loadConversations();
    }
  },

  deleteConversation: async (id) => {
    try {
      await chatApi.deleteConversation(id);
    } catch (error) {
      set({ conversationsError: toReadableError(error) });
      return;
    }
    set((state) => {
      const isActive = state.activeConversationId === id;
      return {
        conversations: state.conversations.filter((c) => c.id !== id),
        // 删掉的正好是当前打开的会话：清空消息区，回到"新会话"草稿态
        activeConversationId: isActive ? null : state.activeConversationId,
        messages: isActive ? [] : state.messages,
        messagesError: isActive ? null : state.messagesError,
      };
    });
  },

  appendMessage: (message) => set((state) => ({ messages: [...state.messages, message] })),

  appendDelta: (id, text) =>
    set((state) => ({
      // 只替换目标消息对象，其余消息引用不变 —— 配合组件的 memo
      // 可以让历史消息不参与重渲染
      messages: state.messages.map((message) =>
        message.id === id ? { ...message, content: message.content + text } : message,
      ),
    })),

  setMessageCitations: (id, citations) =>
    set((state) => ({
      messages: state.messages.map((message) =>
        message.id === id ? { ...message, citations } : message,
      ),
    })),

  appendToolCall: (id, toolCall) =>
    set((state) => ({
      // 和 citations 的区别：引用一次到齐、工具调用是逐条来的，所以是追加。
      // 仍然只替换目标消息对象，其余消息引用不变（配合 memo 跳过重渲染）
      messages: state.messages.map((message) =>
        message.id === id
          ? { ...message, toolCalls: [...(message.toolCalls ?? []), toolCall] }
          : message,
      ),
    })),

  setMessageStatus: (id, status, error = null) =>
    set((state) => {
      const target = state.messages.find((message) => message.id === id);
      // 状态没变化就返回原 state：流式时每个增量都会调一次，避免无谓重渲染
      if (!target || target.status === status) return state;
      return {
        messages: state.messages.map((message) =>
          message.id === id ? { ...message, status, error } : message,
        ),
      };
    }),

  setIsStreaming: (value) => set({ isStreaming: value }),
}));
