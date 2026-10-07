/**
 * SSE（Server-Sent Events）解析器。
 *
 * 为什么不用 EventSource：
 *   EventSource 只支持 GET，而聊天接口必须 POST —— 要把 conversation_id 和
 *   消息正文放进请求体。所以只能用 fetch + ReadableStream 手动解析。
 *
 * 为什么单独放一个文件、不放进 hook：
 *   解析器是纯逻辑（输入 ReadableStream，输出事件），跟 React 无关，
 *   放这里可以被任何地方复用、也好单独测。hook 只负责把事件接到 store 上。
 *
 * ── 后端帧格式（app/api/chat.py）────────────────────────────
 *   引用帧： data: {"citations": [{index, filename, page, score, ...}]}\n\n
 *            ← 一定排在所有文本帧之前（开了知识库且命中时才有）
 *   增量帧： data: {"text": "一小段增量"}\n\n
 *   工具帧： data: {"tool_call": {name, ok, rows, duration_ms, ...}}\n\n
 *            ← Phase 3：一次工具**已经跑完**才会推这一帧，
 *              可能夹在任意两个文本帧之间（模型边查边答）
 *   结束帧： data: [DONE]\n\n
 *   错误帧： data: {"error": "错误信息"}\n\n   ← 错误后不会再推 [DONE]
 * ─────────────────────────────────────────────────────────
 */
import type { Citation, ToolCallInfo } from '../types/chat';

/** 解析后的事件：把"帧格式"翻译成"业务语义"，上层不用再关心 SSE 长什么样 */
export type SSEEvent =
  | { type: 'citations'; citations: Citation[] }
  | { type: 'delta'; text: string }
  | { type: 'tool_call'; toolCall: ToolCallInfo }
  | { type: 'done' }
  | { type: 'error'; message: string };

/**
 * 把 fetch 的响应体流解析成事件流。
 *
 * 两个必须用缓冲区处理的原因（这是 SSE 解析最容易写错的地方）：
 *
 * 1. 一次 read() 不等于一帧。TCP 只保证字节顺序，不保证"按消息边界"送达：
 *    一个 chunk 里可能有 3 帧半，也可能半帧都不到。所以必须把已收到的字节
 *    攒进 buffer，每次只切出完整的帧（以 \n\n 结尾），剩下的留在 buffer 里
 *    等下一个 chunk 补齐。
 *
 * 2. 多字节字符可能被切在中间。中文在 UTF-8 里占 3 字节，如果这 3 个字节
 *    被拆到两个 chunk，直接 decode 会得到乱码。所以 decoder.decode 要传
 *    { stream: true }，让不完整的字节序列留在解码器内部，等下一块数据来补齐。
 */
export async function* parseSSEStream(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<SSEEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // 可能一次拿到多帧：循环切出所有完整帧，最后一段不完整的留在 buffer
      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        const rawFrame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const event = parseFrame(rawFrame);
        if (event) yield event;
        boundary = buffer.indexOf('\n\n');
      }
    }

    // 流结束：flush 掉解码器里残留的字节（正常情况下没有，
    // 除非最后一个多字节字符恰好被截断）
    buffer += decoder.decode();

    // 对端关闭连接但最后一段不是完整帧（缺结尾的 \n\n）：
    // 后端正常情况下会先发 [DONE] 再关闭，这里只在异常断流时才会命中
    const tail = parseFrame(buffer);
    if (tail) yield tail;
  } finally {
    // 无论是正常读完、被 abort、还是抛异常，都要释放 reader，
    // 否则连接不会被回收
    reader.releaseLock();
  }
}

/**
 * 解析单帧。SSE 规范里一帧可以有多行 `data:`，需要拼接；
 * 后端目前只发一行，但仍按规范收集，避免以后改多行时解析错。
 */
function parseFrame(rawFrame: string): SSEEvent | null {
  const dataLines: string[] = [];
  for (const line of rawFrame.split('\n')) {
    if (!line.startsWith('data:')) continue;
    // 规范允许 "data:xxx" 和 "data: xxx" 两种写法，去掉可选的一个空格
    dataLines.push(line.slice(5).replace(/^ /, ''));
  }
  if (dataLines.length === 0) return null;

  const data = dataLines.join('\n');
  if (data === '[DONE]') return { type: 'done' };

  try {
    const parsed: unknown = JSON.parse(data);
    if (typeof parsed !== 'object' || parsed === null) return null;

    const { text, error, citations, tool_call: toolCall } = parsed as {
      text?: unknown;
      error?: unknown;
      citations?: unknown;
      tool_call?: unknown;
    };
    if (typeof error === 'string') return { type: 'error', message: error };
    // citations 必须排在 text 前面判断：后端把引用帧放在最前面，
    // 这里只是让"先引用后文本"的顺序在代码上也一眼看得出来
    if (Array.isArray(citations)) {
      return { type: 'citations', citations: citations as Citation[] };
    }
    if (typeof toolCall === 'object' && toolCall !== null) {
      return { type: 'tool_call', toolCall: toolCall as ToolCallInfo };
    }
    if (typeof text === 'string') return { type: 'delta', text };
    // 未知字段（例如后端以后新增的 usage 帧）：静默忽略，
    // 不能因为不认识就把整个流打断
    return null;
  } catch {
    // 半帧 JSON / 非 JSON 内容：跳过这一帧，不影响后续帧
    return null;
  }
}
