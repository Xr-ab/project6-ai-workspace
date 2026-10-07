/**
 * 知识库域 API（Phase 2）。页面不直接拼 URL，只调这里的方法。
 *
 * 上传走 multipart 而不是 JSON：文件是二进制，塞进 JSON 要 base64 编码，
 * 体积涨 33% 且后端还得再解一遍。multipart 是浏览器原生支持的文件传输格式。
 */
import { request } from './client';
import type { KnowledgeDocument } from '../types/document';

/** 文档列表（后端按创建时间倒序） */
export function listDocuments(limit = 100, offset = 0): Promise<KnowledgeDocument[]> {
  return request<KnowledgeDocument[]>('/documents', { params: { limit, offset } });
}

/**
 * 上传一个文件。后端 202 只表示"落盘 + 排队"，返回的 status 恒为 uploaded；
 * 索引在 worker 里跑，终态靠列表轮询看（排雷-F 后台化）
 */
export function uploadDocument(file: File): Promise<KnowledgeDocument> {
  const form = new FormData();
  // 字段名必须叫 file，和后端路由的形参名一致
  form.append('file', file);
  return request<KnowledgeDocument>('/documents', { method: 'POST', body: form });
}

/** 删除文档（分块与磁盘文件一并清理）。后端 204，无响应体 */
export function deleteDocument(id: string): Promise<void> {
  return request<void>(`/documents/${id}`, { method: 'DELETE' });
}

/**
 * 重新索引。两种用法：
 *   不传 file = 用磁盘上现有文件重跑（failed 重试 / 改了切分参数后重切）
 *   传 file   = 先替换文件内容再重跑（更新知识库里的文件）
 */
export function reindexDocument(id: string, file?: File): Promise<KnowledgeDocument> {
  if (!file) {
    // 不带 body 的 POST：后端 file 参数是可选的，空 body 会解析成 None。
    // 传一个空 FormData 也能work，但没必要多包一层 multipart
    return request<KnowledgeDocument>(`/documents/${id}/reindex`, { method: 'POST' });
  }
  const form = new FormData();
  form.append('file', file);
  return request<KnowledgeDocument>(`/documents/${id}/reindex`, { method: 'POST', body: form });
}
