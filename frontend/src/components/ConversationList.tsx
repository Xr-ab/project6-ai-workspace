import type { MouseEvent as ReactMouseEvent } from 'react';

import type { Conversation } from '../types/chat';
import styles from './ConversationList.module.css';

interface ConversationListProps {
  conversations: Conversation[];
  activeId: string | null;
  loading: boolean;
  error: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onDelete: (id: string) => void;
  onRetryLoad: () => void;
}

/** 时间展示：只显示到分钟，且把后端的 ISO 字符串转成本地时间 */
function formatTime(value: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** Chat 页左侧的会话列表：新建 / 切换 / 删除 */
export default function ConversationList({
  conversations,
  activeId,
  loading,
  error,
  onSelect,
  onCreate,
  onDelete,
  onRetryLoad,
}: ConversationListProps) {
  const handleDelete = (event: ReactMouseEvent, conversation: Conversation) => {
    // 阻止冒泡，否则点删除会同时触发父级的"切换会话"
    event.stopPropagation();
    // Phase 1 用原生 confirm 做二次确认：引入自研 Modal 组件属于后续 Phase 的范围
    if (!window.confirm(`确定删除会话「${conversation.title}」？该会话的消息会一并删除。`)) {
      return;
    }
    onDelete(conversation.id);
  };

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <span className={styles.headerTitle}>会话</span>
        <button type="button" className={styles.newButton} onClick={onCreate}>
          + 新建
        </button>
      </div>

      <div className={styles.list}>
        {loading && <p className={styles.hint}>加载中…</p>}

        {!loading && error && (
          <div className={styles.errorBox}>
            <p className={styles.errorText}>{error}</p>
            <button type="button" className={styles.retryButton} onClick={onRetryLoad}>
              重试
            </button>
          </div>
        )}

        {!loading && !error && conversations.length === 0 && (
          <p className={styles.hint}>还没有会话，点「+ 新建」开始第一次对话。</p>
        )}

        {conversations.map((conversation) => (
          <div
            key={conversation.id}
            role="button"
            tabIndex={0}
            className={
              conversation.id === activeId
                ? `${styles.item} ${styles.itemActive}`
                : styles.item
            }
            onClick={() => onSelect(conversation.id)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                onSelect(conversation.id);
              }
            }}
          >
            <div className={styles.itemBody}>
              <span className={styles.itemTitle}>{conversation.title}</span>
              <span className={styles.itemTime}>
                {formatTime(conversation.last_message_at ?? conversation.created_at)}
              </span>
            </div>
            <button
              type="button"
              className={styles.deleteButton}
              title="删除会话"
              aria-label={`删除会话 ${conversation.title}`}
              onClick={(event) => handleDelete(event, conversation)}
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
