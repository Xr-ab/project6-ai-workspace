import { memo, useCallback, useEffect, useRef, useState } from 'react';

import MarkdownView from './MarkdownView';
import type { ChatMessage, ToolCallInfo } from '../types/chat';
import styles from './MessageList.module.css';

interface MessageListProps {
  messages: ChatMessage[];
  loading: boolean;
  error: string | null;
  /** 空态下点击建议问题：直接发送 */
  onPickSuggestion: (text: string) => void;
  /** 失败消息点「重试」：由页面找回那轮的 user 内容再发一次（R12） */
  onRetry: (assistantMessageId: string) => void;
  /** 本轮是否正在流式（R58）。`send` 开头有 `if (!text || store.isStreaming) return;` 的闸，
   *  流式中点重试是**静默 no-op**——按钮照旧可见照旧可点却什么都不发生。组件层不订阅 store
   *  （全仓只有页面订阅，这是既有纪律），所以由页面把这个布尔传下来，用在重试按钮的 disabled 上。 */
  streaming: boolean;
}

/** 空态建议问题（文档 §5.3：新会话欢迎语 + 建议问题） */
const SUGGESTIONS = [
  '用三句话解释什么是 RAG（检索增强生成）',
  '帮我写一段 Python 读取 CSV 并统计缺失值的代码',
  '对比向量数据库和传统全文检索的适用场景',
];

/**
 * 工具调用的一块：可折叠的一行摘要 + 展开后的参数。
 *
 * 用原生 <details>/<summary> 而不是 useState 自己做折叠：
 * 展开/收起是浏览器自带的交互，键盘操作和无障碍语义都是免费的，
 * 也不需要为每条调用记录在组件里各存一个布尔 state。
 */
const ToolCallItem = memo(function ToolCallItem({ tool }: { tool: ToolCallInfo }) {
  // 行数只对"查数据"类工具有意义（python_calculator 就没有），
  // 没有就不显示，而不是显示个 0
  const meta: string[] = [];
  if (typeof tool.rows === 'number') meta.push(`${tool.rows} 条结果`);
  if (tool.truncated) meta.push('已截断');

  return (
    <li>
      <details className={styles.tool}>
        <summary className={styles.toolSummary}>
          <span className={styles.toolChevron} aria-hidden="true">
            ▸
          </span>
          <span
            className={
              tool.ok ? styles.toolStatus : `${styles.toolStatus} ${styles.toolStatusError}`
            }
          >
            {tool.ok ? '成功' : '失败'}
          </span>
          <span className={styles.toolName}>{tool.name}</span>
          {meta.length > 0 && <span className={styles.toolMeta}>{meta.join(' · ')}</span>}
          {typeof tool.duration_ms === 'number' && (
            <span className={styles.toolDuration}>{tool.duration_ms} ms</span>
          )}
        </summary>
        {/* 模型这次到底查了什么。缩进 2 空格是为了让人能读 SQL */}
        <pre className={styles.toolArgs}>
          {tool.args && Object.keys(tool.args).length > 0
            ? JSON.stringify(tool.args, null, 2)
            : '（无参数）'}
        </pre>
      </details>
      {/* 失败原因放在折叠外面：出错了要直接看见，不该还让用户点一下 */}
      {tool.error && <p className={styles.toolError}>{tool.error}</p>}
    </li>
  );
});

/** 复制到剪贴板。navigator.clipboard 只在安全上下文可用（http://localhost 算），
 *  但它仍会因权限策略 reject——失败要出声，不能静默当成功（否则用户以为复制到了）。 */
async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/**
 * 单条消息。用 memo 包一层：流式时每来一个增量，messages 数组都会重建，
 * 但只有"正在流式的那条"内容变了，其余消息引用不变 —— memo 让它们跳过重渲染。
 */
const MessageItem = memo(function MessageItem({
  message,
  copyState,
  streaming,
  onCopy,
  onRetry,
}: {
  message: ChatMessage;
  /** 仅当本条刚被点过复制时非 null。整份复制提示由外层统一持有一份，
   *  这样「同时只出现一个已复制」，且点下一条时上一条的提示自然消失 */
  copyState: { ok: boolean } | null;
  /** 上一轮还在流式时重试按钮置灰（R58：不置灰就是个点了没动静的死按钮） */
  streaming: boolean;
  onCopy: (messageId: string, text: string) => void;
  onRetry: (messageId: string) => void;
}) {
  const isUser = message.role === 'user';

  return (
    <div className={isUser ? `${styles.row} ${styles.rowUser}` : styles.row}>
      <div className={styles.avatar}>{isUser ? '我' : 'AI'}</div>
      <div className={styles.body}>
        {/* 工具调用过程（Phase 3）。放在气泡**上方**：模型是先查完再作答，
            从上往下读正好是它实际的取数顺序；放下面会像个事后补充的脚注 */}
        {!isUser && message.toolCalls && message.toolCalls.length > 0 && (
          <div className={styles.tools}>
            <span className={styles.toolsLabel}>工具调用</span>
            <ol className={styles.toolList}>
              {message.toolCalls.map((tool, index) => (
                // 工具名可能重复（同一轮调两次 sql_query），所以带上下标做 key。
                // 列表只追加不删改，用下标是安全的
                <ToolCallItem key={`${tool.name}-${index}`} tool={tool} />
              ))}
            </ol>
          </div>
        )}

        <div
          className={[
            styles.bubble,
            isUser ? styles.bubbleUser : styles.bubbleAssistant,
            message.status === 'error' ? styles.bubbleError : '',
          ]
            .filter(Boolean)
            .join(' ')}
        >
          {isUser ? (
            // 用户输入按纯文本渲染并保留换行：用户输入里可能有 < > 之类的字符，
            // 走 Markdown 会被当成标签解析，既不安全也没必要
            <div className={styles.userText}>{message.content}</div>
          ) : message.content ? (
            <MarkdownView content={message.content} />
          ) : (
            // 助手消息在 sending 阶段还没有任何内容，给个占位提示，
            // 否则气泡是空的，看起来像渲染坏了
            <span className={styles.placeholder}>
              {message.status === 'sending' ? '正在思考…' : ''}
            </span>
          )}

          {/* 流式进行中：内容末尾跟一个闪烁光标，形成打字机观感 */}
          {message.status === 'streaming' && <span className={styles.cursor} />}
        </div>

        <div className={styles.meta}>
          {message.status === 'sending' && <span className={styles.metaSending}>发送中…</span>}
          {message.status === 'stopped' && <span className={styles.metaMuted}>已停止生成</span>}
          {message.status === 'error' && (
            <span className={styles.metaError}>{message.error ?? '生成失败'}</span>
          )}
        </div>

        {/* 动作条是助手侧专属（R86）：复制要 !isUser，重试只在 status==='error'，
            而用户消息恒为 done 且没有错误态，所以整条对它必空 —— `.body` 是带 gap 的 flex column，
            空 div 仍要吃掉一个 4px gap，每条用户气泡下方多一道死缝隙。
            R126（终评 F6，R86 剩下的那一半）：助手空轮同样不该渲空条——stopped 且 content==='' 时
            复制被 !content 挡、重试只在 error，条里什么都没有；error 即使无内容也有重试，故放行。 */}
        {!isUser && (message.status === 'done' || message.status === 'stopped' || message.status === 'error')
          && (message.content || message.status === 'error') && (
          <div className={styles.actions}>
            {message.content && (
              <button type="button" className={styles.actionBtn} onClick={() => onCopy(message.id, message.content)}>
                复制
              </button>
            )}
            {message.status === 'error' && (
              <button
                type="button"
                className={styles.actionBtn}
                onClick={() => onRetry(message.id)}
                disabled={streaming}
                title={streaming ? '上一轮还在生成，先等它结束' : undefined}
              >
                重试
              </button>
            )}
            {copyState && (
              <span
                className={copyState.ok ? styles.copiedTip : `${styles.copiedTip} ${styles.copiedTipError}`}
              >
                {copyState.ok ? '已复制' : '复制失败'}
              </span>
            )}
          </div>
        )}

        {/* 引用来源。有才渲染：普通对话（未开知识库）没有 citations */}
        {message.citations && message.citations.length > 0 && (
          <div className={styles.citations}>
            <span className={styles.citationsLabel}>引用来源</span>
            <ol className={styles.citationList}>
              {message.citations.map((citation) => (
                <li key={`${citation.document_id}-${citation.chunk_index}`} className={styles.citation}>
                  {/* 编号就是正文里 [n] 的 n，由后端统一分配，这里原样显示 */}
                  <span className={styles.citationIndex}>{citation.index}</span>
                  <span className={styles.citationName} title={citation.filename}>
                    {citation.filename}
                  </span>
                  {citation.page !== null && (
                    <span className={styles.citationPage}>第 {citation.page} 页</span>
                  )}
                  {/* 相似度给个粗略百分比就够：它是"相关度提示"，
                      不是精确指标，显示 4 位小数反而像在装精确 */}
                  <span className={styles.citationScore}>
                    {Math.round(citation.score * 100)}%
                  </span>
                </li>
              ))}
            </ol>
          </div>
        )}
      </div>
    </div>
  );
});

export default function MessageList({
  messages,
  loading,
  error,
  streaming,
  onPickSuggestion,
  onRetry,
}: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  // 每次消息变化（含每个流式增量）都把视图拉到底部，保证始终看到最新内容。
  // 用 scrollIntoView 而不是改 scrollTop，是因为容器高度由 CSS 决定，
  // 这里不需要知道具体数值
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [messages]);

  // 提示只存一份，且 id 与 ok 分开：合在一个对象里时「同一条消息先成功后失败」
  // 只换对象不换 id，而外层按 id 比对下发 props，会漏掉这次文案变化
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [copiedOk, setCopiedOk] = useState(false);

  const handleCopy = useCallback((id: string, text: string) => {
    void copyToClipboard(text).then((ok) => {
      setCopiedId(id);
      setCopiedOk(ok);
    });
  }, []);

  if (loading) {
    return (
      <div className={styles.center}>
        <p className={styles.hint}>正在加载历史消息…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.center}>
        <div className={styles.errorCard}>
          <p className={styles.errorText}>{error}</p>
          <p className={styles.errorHint}>可以从左侧选择其它会话，或新建一个会话。</p>
        </div>
      </div>
    );
  }

  if (messages.length === 0) {
    return (
      <div className={styles.center}>
        <div className={styles.welcome}>
          <h2 className={styles.welcomeTitle}>开始新的对话</h2>
          <p className={styles.welcomeText}>
            输入问题后按 Enter 发送，Shift + Enter 换行。回复会逐字流式输出。
          </p>
          <div className={styles.suggestions}>
            {SUGGESTIONS.map((text) => (
              <button
                key={text}
                type="button"
                className={styles.suggestion}
                onClick={() => onPickSuggestion(text)}
              >
                {text}
              </button>
            ))}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.list}>
      {messages.map((message) => (
        <MessageItem
          key={message.id}
          message={message}
          copyState={message.id === copiedId ? { ok: copiedOk } : null}
          streaming={streaming}
          onCopy={handleCopy}
          onRetry={onRetry}
        />
      ))}
      {/* 滚动锚点，始终位于列表最底部 */}
      <div ref={bottomRef} />
    </div>
  );
}
