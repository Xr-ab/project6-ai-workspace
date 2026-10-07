/**
 * 与后端 schemas/conversation.py 一一对应的类型。
 * 后端字段是 snake_case，这里保持原样不转驼峰 —— 多一层转换只会制造
 * "接口改了前端忘了改"的隐性 bug，直接对齐反而更安全。
 */

/** 会话摘要（对应 ConversationOut） */
export interface Conversation {
  id: string;
  title: string;
  model: string | null;
  last_message_at: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * 一条引用来源（对应后端 app/ai/rag/citation.py 的 Citation.to_payload）。
 *
 * 只在"开启知识库 + 检索命中"时才有。index 是模型回答里 [n] 的那个 n，
 * 两边共用同一个编号来源（后端拼上下文时的循环下标），所以不会对不上。
 */
export interface Citation {
  /** 资料编号，从 1 开始 */
  index: number;
  document_id: string;
  filename: string;
  /** 页码，txt / csv 这类没有页概念的格式为 null */
  page: number | null;
  chunk_index: number;
  /** 相似度，越大越相关（后端已把距离转成 0~1 的分数并保留 4 位） */
  score: number;
}

/** 一条消息（对应 MessageOut） */
export interface Message {
  id: string;
  seq: number;
  role: string;
  content: string;
  content_type: string;
  prompt_tokens: number;
  completion_tokens: number;
  error_message: string | null;
  /** 引用来源。普通对话（未开知识库）为 null */
  citations: Citation[] | null;
  created_at: string;
}

/** 会话详情 = 会话字段 + 历史消息（对应 ConversationDetailOut，messages 按 seq 正序） */
export interface ConversationDetail extends Conversation {
  messages: Message[];
  /**
   * 本会话的全部工具调用（按时间正序）。平铺而不是嵌在 messages 里：
   * 一条消息可能调多次工具，前端按 message_id 分组即可挂回对应消息
   */
  tool_calls: ToolCallInfo[];
}

/**
 * 一次工具调用的展示信息（对应后端 ToolCallOut）。
 *
 * 两个来源共用这一个类型，字段完全一致：
 *   - SSE 的 tool_call 帧（实时，调用完一条推一条）
 *   - 会话详情的 tool_calls 数组（刷新页面后重放）
 * 所以不需要转换函数，也不会出现"实时显示得出来、刷新后缺字段"。
 */
export interface ToolCallInfo {
  /**
   * 挂在哪条助手消息上。实时帧里是 null（推帧时助手消息还没落库、还没 id）；
   * 历史里是后端消息 id，用来把调用过程挂回对应的那条回复
   */
  message_id?: string | null;
  name: string;
  /** data / knowledge / research / business */
  tool_type: string;
  /** 模型这次到底查了什么（SQL 文本 / 查询词）；无参数时为 null */
  args: Record<string, unknown> | null;
  ok: boolean;
  error: string | null;
  /** 返回条数（SQL / 文件读取类工具才有） */
  rows: number | null;
  duration_ms: number | null;
  /** 结果是否被截断过，前端要提示"只显示了前 N 条" */
  truncated: boolean;
}

/**
 * 消息状态机（文档 07-frontend-design §6.3）：
 *
 *   sending ──► streaming ──► done
 *      │            │
 *      └──► error ◄─┘
 *
 * stopped 是文档 §6.4 的补充态：用户主动中止，已产出的内容保留，
 * 语义上等同 done（不再追加），只是 UI 上标一句"已停止"。
 */
export type MessageStatus = 'sending' | 'streaming' | 'done' | 'error' | 'stopped';

/**
 * 前端持有的消息。和历史消息的区别：
 * 流式过程中的消息还没有落库、没有后端 id，所以 id 由前端生成；
 * 同时多了一个 status 字段承载状态机。
 */
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  status: MessageStatus;
  error: string | null;
  /**
   * 引用来源。可选而不是必填，是为了不改动所有构造点：
   * 用户消息、普通对话的助手消息本来就没有引用。
   * 空数组和 undefined 在 UI 上是同一个意思（不渲染来源块）。
   */
  citations?: Citation[];
  /**
   * 工具调用过程（Phase 3）。和 citations 同理可选：
   * 用户消息、没开工具的助手消息都没有。
   * 流式过程中每收到一个 tool_call 帧追加一条，刷新后从会话详情恢复
   */
  toolCalls?: ToolCallInfo[];
}
