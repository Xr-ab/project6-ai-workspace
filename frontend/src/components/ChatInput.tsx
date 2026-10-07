import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent } from 'react';

import { toReadableError } from '../api/client';
import styles from './ChatInput.module.css';

interface ChatInputProps {
  onSend: (text: string) => void;
  onStop: () => void;
  /** 是否正在生成：生成中禁用发送并显示"停止生成" */
  streaming: boolean;
  /** 附件上传（复用知识库写口）。抛错由本组件展示中文，页面不兜 */
  onAttach: (file: File) => Promise<void>;
  /** 是否检索知识库（Phase 2 RAG） */
  useKnowledge: boolean;
  onToggleKnowledge: (value: boolean) => void;
  /** 是否允许模型自主调用工具（Phase 3 Tool Calling） */
  useTools: boolean;
  onToggleTools: (value: boolean) => void;
}

/** 多行输入框的最大高度，超过后输入框内部滚动（避免把消息区挤没） */
const MAX_HEIGHT = 180;

/**
 * 胶囊开关。用 button + aria-pressed 而不是 checkbox：
 * 它是一个"模式开关"，视觉上要能一眼看出当前是开还是关。
 * 抽出来是因为知识库和工具两个开关长得一样、行为也一样，只该写一遍。
 */
function Toggle({
  label,
  title,
  active,
  onToggle,
}: {
  label: string;
  title: string;
  active: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className={active ? `${styles.toggle} ${styles.toggleOn}` : styles.toggle}
      onClick={onToggle}
      aria-pressed={active}
      title={title}
    >
      <span className={styles.toggleDot} />
      {label}
    </button>
  );
}

/** 底部输入区：Enter 发送 / Shift+Enter 换行 / 生成中可停止 / 可开关知识库与工具 */
export default function ChatInput({
  onSend,
  onStop,
  streaming,
  onAttach,
  useKnowledge,
  onToggleKnowledge,
  useTools,
  onToggleTools,
}: ChatInputProps) {
  const [value, setValue] = useState('');
  const [attaching, setAttaching] = useState(false);
  const [attachNotice, setAttachNotice] = useState<string | null>(null);
  // R126（终评 F5②）：202 成功文案与失败文案原来共用中性 `.attachNotice`，失败看起来像成功。
  // 分成两个 state——布局仍走 .attachNotice，失败只叠一个红色变体类（同 MessageList 的
  // copiedTip/copiedTipError 配对，不新发明颜色值）。
  const [attachError, setAttachError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // 随内容自动增高：先重置成 auto 再按 scrollHeight 设置，
  // 否则高度只会涨不会跌（删字后收不回去）
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
  }, [value]);

  const submit = () => {
    const text = value.trim();
    if (!text || streaming) return;
    // 先清空再发送：发送是异步的，清空让输入框立刻回到可用状态
    setValue('');
    onSend(text);
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter') return;
    // 中文输入法组词过程中的 Enter 是在"选词上屏"，不是发送 ——
    // 不判断的话拼音打到一半按回车就会把半截拼音发出去
    if (event.nativeEvent.isComposing) return;
    // Shift+Enter 交给浏览器默认行为（插入换行）
    if (event.shiftKey) return;
    event.preventDefault();
    submit();
  };

  const attach = async (file?: File) => {
    if (!file) return;
    setAttaching(true);
    setAttachNotice(null);
    setAttachError(null);
    try {
      await onAttach(file);
      // 202 口径（排雷-F）：这句只承诺"收下了"，绝不写"已可检索"
      setAttachNotice(`已收下「${file.name}」，正在后台索引；排队期间还检索不到，稍后在知识库可见。`);
    } catch (err) {
      setAttachError(toReadableError(err));
    } finally {
      setAttaching(false);
      // 清空 value，否则同一个文件再选一次不会触发 change
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  return (
    <div className={styles.wrapper}>
      <div className={styles.box}>
        <textarea
          ref={textareaRef}
          className={styles.textarea}
          value={value}
          rows={1}
          placeholder="输入消息，Enter 发送，Shift + Enter 换行"
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={handleKeyDown}
          disabled={streaming}
        />

        <div className={styles.actions}>
          <input
            ref={fileRef}
            type="file"
            className={styles.fileInput}
            accept=".md,.txt,.pdf,.docx,.csv,.xlsx"
            onChange={(event) => void attach(event.target.files?.[0])}
          />
          <button
            type="button"
            className={styles.attachButton}
            onClick={() => fileRef.current?.click()}
            disabled={attaching || streaming}
            title="附件会存入知识库；后台索引完成前检索不到它"
          >
            {attaching ? '上传中…' : '附件'}
          </button>
          <Toggle
            label="知识库"
            title="开启后先检索知识库再回答，并在回复下方展示引用来源"
            active={useKnowledge}
            onToggle={() => onToggleKnowledge(!useKnowledge)}
          />
          <Toggle
            label="工具"
            title="开启后模型可自行决定调用工具（查数据库 / 算数 / 读文件 / 查知识库），调用过程会显示在回复上方"
            active={useTools}
            onToggle={() => onToggleTools(!useTools)}
          />

          {streaming && <span className={styles.tip}>正在生成回复…</span>}

          {streaming ? (
            <button type="button" className={styles.stopButton} onClick={onStop}>
              停止生成
            </button>
          ) : (
            <button
              type="button"
              className={styles.sendButton}
              onClick={submit}
              disabled={!value.trim()}
            >
              发送
            </button>
          )}
        </div>
        {attachNotice && <p className={styles.attachNotice}>{attachNotice}</p>}
        {attachError && (
          <p className={`${styles.attachNotice} ${styles.attachNoticeError}`}>{attachError}</p>
        )}
      </div>
    </div>
  );
}
