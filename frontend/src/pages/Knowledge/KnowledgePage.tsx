import { useCallback, useEffect, useRef, useState } from 'react';
import type { ChangeEvent } from 'react';

import { toReadableError } from '../../api/client';
import {
  deleteDocument,
  listDocuments,
  reindexDocument,
  uploadDocument,
} from '../../api/document';
import type { DocumentStatus, KnowledgeDocument } from '../../types/document';
import styles from './KnowledgePage.module.css';

/**
 * 上传时限定可选的文件类型。写成扩展名而不是 MIME：
 * 后端就是按扩展名判定支持与否（ai/rag/parser.py 的 SUPPORTED_TYPES），
 * 用 MIME 会两边规则不一致（同一类型在不同系统上报的 MIME 还不一样）。
 */
const ACCEPT = '.pdf,.docx,.txt,.md,.csv,.xlsx';

/** 成功提示的停留时长：只是"操作已生效"的确认，留太久会占位置 */
const HINT_DURATION = 3000;

/** 列表里还有非终态文档时的静默轮询间隔（与 workflow 详情页同量级节奏） */
const POLL_INTERVAL_MS = 2500;

/** 轮询连续失败几次就止损：worker/后端真挂时不该无限打它，交回手动刷新 */
const MAX_POLL_FAILURES = 3;

/**
 * 索引是否已定局。uploaded/parsing/chunking/embedding 都表示"还在 worker 路上"，
 * 只有 ready/failed 可以停表。
 */
function isSettled(status: DocumentStatus): boolean {
  return status === 'ready' || status === 'failed';
}

/**
 * 文件大小按 B / KB / MB 展示。原始字节数又长又没意义，
 * 但也不能只显示"1.2 MB"这种取整值而丢掉原始精度 —— 列表里够用即可。
 */
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

/** 上传时间转本地时间展示。知识库文档会长期留存，所以带上年份 */
function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/**
 * 状态 → 中文文案 + 徽标样式。用 switch 而不是内联嵌套三元：
 * 状态有 6 个取值，写在 JSX 里会变成一串看不懂的三元表达式。
 */
function statusView(status: DocumentStatus): { label: string; className: string } {
  switch (status) {
    case 'ready':
      return { label: '已就绪', className: styles.badgeReady };
    case 'failed':
      return { label: '索引失败', className: styles.badgeFailed };
    case 'uploaded':
      return { label: '待索引', className: styles.badgeNeutral };
    default:
      // parsing / chunking / embedding：索引跑在 worker 里，这三个中间态现在
      // 真的看得见（轮询会把它推到 已就绪 / 索引失败）
      return { label: '索引中', className: styles.badgeProcessing };
  }
}

/**
 * 知识库页（文档 §5.4）：上传文档 + 列表管理（重新索引 / 删除）。
 *
 * 状态全部放在页面本地：这一页没有跨页面共享的数据，也没有别处要读它，
 * 进全局 store 只会多一份需要同步的状态。数据每次都从接口重新拉，
 * 不做前端缓存 —— 上传/删除后直接 refresh，省掉"本地列表和库里对不上"的坑。
 */
export default function KnowledgePage() {
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [loading, setLoading] = useState(true);
  // 列表加载失败和"某次操作失败"要分开：前者整页没法用（给重试按钮），
  // 后者列表还在、只是这次操作没成（给一条提示条就够）
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);

  const [uploading, setUploading] = useState(false);
  // 按 id 记录正在忙的行：多行操作互不影响，也不会整页一起禁用
  const [reindexingId, setReindexingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  // 二次确认用"记录待确认的行 id"，而不是 window.confirm：
  // 原生弹窗会阻塞渲染、样式不可控，也打断用户对列表的上下文
  const [confirmId, setConfirmId] = useState<string | null>(null);

  const fileInputRef = useRef<HTMLInputElement>(null);

  const loadDocuments = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      setDocuments(await listDocuments());
    } catch (error) {
      // 错误文案统一交给 toReadableError：页面不拼错误信息，
      // 后端返回的中文提示（例如"已存在，无需重复上传"）也在这里原样透出
      setLoadError(toReadableError(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadDocuments();
  }, [loadDocuments]);

  // 索引后台化（排雷-F）：上传/重索引只拿到 202 + uploaded，终态由 worker 写。
  // 所以列表里只要还有非终态文档就静默轮询（不动 loading，避免整页闪一下），
  // 全部定局后 effect 重跑时自然停表。
  useEffect(() => {
    if (!documents.some((doc) => !isSettled(doc.status))) return;
    let failures = 0;
    let cancelled = false;

    const tick = async () => {
      try {
        const fresh = await listDocuments();
        if (cancelled) return;
        failures = 0;
        setDocuments(fresh);
      } catch (error) {
        if (cancelled) return;
        failures += 1;
        // 单次失败不吭声（下一次还会试）；连着三次才止损并说明原因。
        // 止损必须真停表：worker/后端挂了时无限打它没有意义，手动「刷新」是恢复入口
        if (failures >= MAX_POLL_FAILURES) {
          window.clearInterval(timer);
          setActionError(toReadableError(error));
        }
      }
    };

    const timer = window.setInterval(() => void tick(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [documents]);

  // 成功提示自动消失。放 useEffect 里而不是在操作成功处 setTimeout：
  // 这样组件卸载时定时器会被清理，不会在已卸载的组件上 setState
  useEffect(() => {
    if (!hint) return;
    const timer = window.setTimeout(() => setHint(null), HINT_DURATION);
    return () => window.clearTimeout(timer);
  }, [hint]);

  const handleFileChange = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    // 先清空 input 再处理：不清空的话连续选同一个文件不会再触发 change，
    // 用户会以为"点了没反应"
    event.target.value = '';
    if (!file) return;

    setUploading(true);
    setActionError(null);
    try {
      // 后端只做"落盘 + 建行 + 入队"就返回 202（排雷-F 后台化），
      // 所以这里的提示不能说"已索引完成"；刷新后列表显示「待索引」，
      // 由上面的轮询 effect 把它推到 已就绪 / 索引失败
      await uploadDocument(file);
      setHint(`「${file.name}」已收下，正在后台索引`);
      await loadDocuments();
    } catch (error) {
      setActionError(toReadableError(error));
    } finally {
      setUploading(false);
    }
  };

  const handleReindex = async (doc: KnowledgeDocument) => {
    setReindexingId(doc.id);
    setActionError(null);
    try {
      // 不传 file：用磁盘上的原文件重跑，正是"失败重试"需要的语义
      await reindexDocument(doc.id);
      setHint(`「${doc.filename}」已排队，正在后台重新索引`);
      await loadDocuments();
    } catch (error) {
      setActionError(toReadableError(error));
    } finally {
      setReindexingId(null);
    }
  };

  const handleDelete = async (doc: KnowledgeDocument) => {
    setDeletingId(doc.id);
    setActionError(null);
    try {
      await deleteDocument(doc.id);
      setConfirmId(null);
      setHint(`已删除「${doc.filename}」`);
      await loadDocuments();
    } catch (error) {
      setActionError(toReadableError(error));
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <div className={styles.page}>
      <div className={styles.container}>
        <section className={styles.uploadPanel}>
          <div className={styles.uploadText}>
            <h2 className={styles.uploadTitle}>上传文档</h2>
            <p className={styles.uploadHint}>
              支持 PDF、Word（docx）、TXT、Markdown、CSV、Excel（xlsx）。
              上传后会自动解析、切分并向量化，完成后即可在 AI Chat 中开启「知识库」检索。
            </p>
          </div>
          <button
            type="button"
            className={styles.uploadButton}
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
          >
            {uploading ? '正在上传并索引…' : '选择文件'}
          </button>
          {/* 真正的 file input 藏起来，用上面的按钮代它触发：
              原生 input 的样式在各浏览器差异很大，且没法做禁用态文案 */}
          <input
            ref={fileInputRef}
            type="file"
            className={styles.fileInput}
            accept={ACCEPT}
            onChange={(event) => void handleFileChange(event)}
            disabled={uploading}
          />
        </section>

        {hint && <p className={styles.successBanner}>{hint}</p>}
        {actionError && <p className={styles.errorBanner}>{actionError}</p>}

        {loading && (
          <div className={styles.center}>
            <p className={styles.hint}>正在加载文档列表…</p>
          </div>
        )}

        {!loading && loadError && (
          <div className={styles.center}>
            <div className={styles.errorCard}>
              <p className={styles.errorCardText}>{loadError}</p>
              <button
                type="button"
                className={styles.actionButton}
                onClick={() => void loadDocuments()}
              >
                重试
              </button>
            </div>
          </div>
        )}

        {!loading && !loadError && documents.length === 0 && (
          <div className={styles.center}>
            <div className={styles.emptyCard}>
              <h2 className={styles.emptyTitle}>知识库还是空的</h2>
              <p className={styles.emptyText}>
                先上传一份文档，AI 回答时才能引用它的内容。点上方「选择文件」开始。
              </p>
            </div>
          </div>
        )}

        {!loading && !loadError && documents.length > 0 && (
          <ul className={styles.list}>
            {documents.map((doc) => {
              const status = statusView(doc.status);
              const reindexing = reindexingId === doc.id;
              const deleting = deletingId === doc.id;

              return (
                <li key={doc.id} className={styles.row}>
                  <div className={styles.rowMain}>
                    <div className={styles.rowTitle}>
                      {/* 文件名可能很长，用 title 兜住被省略号截掉的部分 */}
                      <span className={styles.filename} title={doc.filename}>
                        {doc.filename}
                      </span>
                      <span className={`${styles.badge} ${status.className}`}>
                        {status.label}
                      </span>
                    </div>

                    <div className={styles.rowMeta}>
                      <span className={styles.fileType}>{doc.file_type.toUpperCase()}</span>
                      <span>{formatSize(doc.size_bytes)}</span>
                      <span>{doc.chunk_count} 个片段</span>
                      <span>{formatTime(doc.created_at)}</span>
                    </div>

                    {/* 失败原因单独占一行并高亮：它是用户下一步动作（重试 / 改文件）
                        的唯一线索，塞进元信息行里会被淹没 */}
                    {doc.status === 'failed' && doc.error_message && (
                      <p className={styles.rowError}>{doc.error_message}</p>
                    )}
                  </div>

                  <div className={styles.rowActions}>
                    <button
                      type="button"
                      className={styles.actionButton}
                      disabled={reindexing}
                      onClick={() => void handleReindex(doc)}
                      title={
                        doc.status === 'failed'
                          ? '用原文件重新索引一次'
                          : '重新解析、切分并向量化这份文档'
                      }
                    >
                      {reindexing ? '索引中…' : doc.status === 'failed' ? '重试' : '重新索引'}
                    </button>

                    {confirmId === doc.id ? (
                      <>
                        <button
                          type="button"
                          className={styles.dangerButton}
                          disabled={deleting}
                          onClick={() => void handleDelete(doc)}
                        >
                          {deleting ? '删除中…' : '确认删除'}
                        </button>
                        <button
                          type="button"
                          className={styles.ghostButton}
                          disabled={deleting}
                          onClick={() => setConfirmId(null)}
                        >
                          取消
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        className={styles.ghostButton}
                        onClick={() => setConfirmId(doc.id)}
                      >
                        删除
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
