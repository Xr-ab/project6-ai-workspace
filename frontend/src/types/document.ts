/**
 * 知识库文档类型（对应后端 schemas/document.py 的 DocumentOut）。
 * 字段保持 snake_case，理由同 types/chat.ts：不做驼峰转换，避免
 * "接口改了前端忘了改"的隐性 bug。
 */

/**
 * 索引状态机（后端 document_service 的流转）：
 *
 *   uploaded ─► parsing ─► chunking ─► embedding ─► ready
 *      └────────────── 任一步失败 ──────────────► failed
 *
 * 注意中间态（parsing / chunking / embedding）在当前实现里基本看不到 ——
 * 索引是同步跑在上传请求里的，前端拿到响应时已经是终态。
 * 保留这几个值是为了将来改成异步任务队列时不用动前端。
 */
export type DocumentStatus =
  | 'uploaded'
  | 'parsing'
  | 'chunking'
  | 'embedding'
  | 'ready'
  | 'failed';

/** 一个知识库文档 */
export interface KnowledgeDocument {
  id: string;
  filename: string;
  /** 扩展名，如 md / pdf / docx（后端按扩展名判定） */
  file_type: string;
  size_bytes: number;
  status: DocumentStatus;
  /** 索引失败原因。成功时为 null */
  error_message: string | null;
  /** 切分出的片段数。失败时为 0 */
  chunk_count: number;
  created_at: string;
}
