import { useCallback, useEffect, useState } from 'react';

import { uploadDocument } from '../../api/document';
import ChatInput from '../../components/ChatInput';
import ConversationList from '../../components/ConversationList';
import MessageList from '../../components/MessageList';
import { useChatStream } from '../../hooks/useSSE';
import { useChatStore } from '../../stores/chatStore';
import styles from './ChatPage.module.css';

/**
 * AI Chat 页（文档 §5.3）：左侧会话列表 + 中间消息流 + 底部输入区。
 * 页面本身不碰网络和状态机，只负责把 store 和 useChatStream 接到组件上。
 */
export default function ChatPage() {
  // 按字段分别订阅：流式时只有 messages 在变，
  // 这样会话列表不会跟着一起重渲染
  const conversations = useChatStore((state) => state.conversations);
  const conversationsLoading = useChatStore((state) => state.conversationsLoading);
  const conversationsError = useChatStore((state) => state.conversationsError);
  const activeConversationId = useChatStore((state) => state.activeConversationId);
  const messages = useChatStore((state) => state.messages);
  const messagesLoading = useChatStore((state) => state.messagesLoading);
  const messagesError = useChatStore((state) => state.messagesError);
  const isStreaming = useChatStore((state) => state.isStreaming);

  const loadConversations = useChatStore((state) => state.loadConversations);
  const selectConversation = useChatStore((state) => state.selectConversation);
  const startNewConversation = useChatStore((state) => state.startNewConversation);
  const deleteConversation = useChatStore((state) => state.deleteConversation);

  const { send, stop } = useChatStream();

  // 知识库 / 工具开关放页面本地 state 而不是 store：它们只影响"下一次请求带什么参数"，
  // 不参与会话状态，也没别的地方要读它们。进 store 反而多一份全局状态要维护。
  // 两个开关互相独立，可以同时开（见后端 chat_stream 的说明）
  const [useKnowledge, setUseKnowledge] = useState(false);
  const [useTools, setUseTools] = useState(false);

  // 进入页面：加载会话列表；若列表非空且当前没选中会话，自动打开最近的一个。
  // 这正是"刷新恢复"——刷新后直接回到上次的对话，而不用手动再点一次
  useEffect(() => {
    void (async () => {
      await loadConversations();
      const state = useChatStore.getState();
      if (!state.activeConversationId && state.conversations.length > 0) {
        void state.selectConversation(state.conversations[0].id);
      }
    })();
  }, [loadConversations]);

  // 离开页面时中止正在进行的流：否则请求会在后台继续跑并继续写 store，
  // 回到页面时会看到状态已经错乱
  useEffect(() => () => stop(), [stop]);

  // 流式进行中切换/新建/删除会话时先中止生成：
  // 否则旧回复的增量会继续追加到新会话的消息列表里（id 对不上，内容会串）
  const handleSelect = (id: string) => {
    if (isStreaming) stop();
    void selectConversation(id);
  };

  const handleCreate = () => {
    if (isStreaming) stop();
    startNewConversation();
  };

  const handleDelete = (id: string) => {
    if (isStreaming && id === activeConversationId) stop();
    void deleteConversation(id);
  };

  // 重试：向前找最近一条 user 消息再发一对（R12）。失败那一对保留在列表里不清理——
  // 它是"这一轮真失败过"的诚实痕迹。
  // deps 里刻意没有 messages：流式时它每个增量都换引用，一旦进 deps，
  // 每条 MessageItem 每帧都会拿到新的 onRetry，memo 就白做了。
  // 重试只在点击瞬间读一次最新列表，用 getState() 取即可（send 本身是稳定引用）。
  const retry = useCallback(
    (assistantId: string) => {
      const { messages: current } = useChatStore.getState();
      const index = current.findIndex((m) => m.id === assistantId);
      for (let i = index - 1; i >= 0; i -= 1) {
        if (current[i].role === 'user') {
          void send(current[i].content, useKnowledge, useTools);
          return;
        }
      }
    },
    [send, useKnowledge, useTools],
  );

  return (
    <div className={styles.page}>
      <ConversationList
        conversations={conversations}
        activeId={activeConversationId}
        loading={conversationsLoading}
        error={conversationsError}
        onSelect={handleSelect}
        onCreate={handleCreate}
        onDelete={handleDelete}
        onRetryLoad={() => void loadConversations()}
      />

      <div className={styles.chat}>
        <MessageList
          messages={messages}
          loading={messagesLoading}
          error={messagesError}
          streaming={isStreaming}
          onPickSuggestion={(text) => void send(text, useKnowledge, useTools)}
          onRetry={retry}
        />
        <ChatInput
          onSend={(text) => void send(text, useKnowledge, useTools)}
          onAttach={async (file) => { await uploadDocument(file); }}
          onStop={stop}
          streaming={isStreaming}
          useKnowledge={useKnowledge}
          onToggleKnowledge={setUseKnowledge}
          useTools={useTools}
          onToggleTools={setUseTools}
        />
      </div>
    </div>
  );
}
