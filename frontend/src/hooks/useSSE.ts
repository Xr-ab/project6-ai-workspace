/**
 * 流式对话的编排层：把「网络请求 + AbortController + store 更新」串起来。
 *
 * 为什么单独抽成 hook 而不是塞进 store：
 *   AbortController 的生命周期跟着组件走（组件卸载要中止请求），
 *   属于 React 侧的东西，放在 store 里会和组件生命周期脱节。
 */
import { useCallback, useRef } from 'react';

import { streamChat } from '../api/chat';
import { isAbortError, toReadableError } from '../api/client';
import { useChatStore } from '../stores/chatStore';
import type { ChatMessage } from '../types/chat';

/**
 * 本地消息 id 生成器。流式过程中的消息还没落库、拿不到后端 UUID，
 * 但 React 列表需要稳定 key，所以前端先生成一个临时 id。
 */
let localIdSeq = 0;
function nextLocalId(role: ChatMessage['role']): string {
  localIdSeq += 1;
  return `local-${role}-${localIdSeq}`;
}

export function useChatStream() {
  // 用 ref 而不是 state：AbortController 不需要触发重渲染，
  // 而且必须跨重渲染存活 —— 丢了就再也"停止"不了当前请求
  const abortRef = useRef<AbortController | null>(null);

  const send = useCallback(async (rawText: string, useKnowledge = false, useTools = false) => {
    const text = rawText.trim();
    const store = useChatStore.getState();
    if (!text || store.isStreaming) return;

    // 懒创建会话：点"新建会话"时并不落库，等真正发出第一条消息才建，
    // 这样不会因为点几下就留一堆空会话
    let conversationId = store.activeConversationId;
    if (!conversationId) {
      const conversation = await store.createConversation(text);
      if (!conversation) return; // 创建失败，错误已写进 conversationsError
      conversationId = conversation.id;
    }

    const assistantId = nextLocalId('assistant');
    store.appendMessage({
      id: nextLocalId('user'),
      role: 'user',
      content: text,
      status: 'done',
      error: null,
    });
    store.appendMessage({
      id: assistantId,
      role: 'assistant',
      content: '',
      // 初始态 sending：请求已发出，等第一个增量
      status: 'sending',
      error: null,
    });
    store.setIsStreaming(true);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamChat(conversationId, text, useKnowledge, useTools, controller.signal, {
        onCitations: (citations) => {
          // 引用帧排在所有文本帧之前，所以这里先于 onDelta 触发：
          // 用户开始看到字的时候，来源块已经在下面了
          useChatStore.getState().setMessageCitations(assistantId, citations);
        },
        onToolCall: (toolCall) => {
          // 工具帧也可能排在第一个文本帧之前：模型先查再答。
          // 一条一条追加（一次回复可能调多个工具），挂在正在流式的这条消息上
          useChatStore.getState().appendToolCall(assistantId, toolCall);
        },
        onDelta: (delta) => {
          const current = useChatStore.getState();
          current.appendDelta(assistantId, delta);
          // 收到增量就进入 streaming。重复设置是安全的（store 里做了幂等判断）
          current.setMessageStatus(assistantId, 'streaming');
        },
        onDone: () => {
          useChatStore.getState().setMessageStatus(assistantId, 'done');
        },
        onError: (message) => {
          useChatStore.getState().setMessageStatus(assistantId, 'error', message);
        },
      });
    } catch (error) {
      // 用户点"停止"触发的 abort 不是错误：已产出的内容保留，标记为 stopped
      if (isAbortError(error)) {
        useChatStore.getState().setMessageStatus(assistantId, 'stopped');
      } else {
        // 404（会话被删）/ 422（消息为空）/ 网络失败都会走到这里
        useChatStore.getState().setMessageStatus(assistantId, 'error', toReadableError(error));
      }
    } finally {
      abortRef.current = null;
      const current = useChatStore.getState();
      current.setIsStreaming(false);
      // 回复结束后刷新会话列表：首条消息派生的标题和"最近活跃"排序都变了
      void current.loadConversations();
    }
  }, []);

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  return { send, stop };
}
