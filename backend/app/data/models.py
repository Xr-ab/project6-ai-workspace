"""SQLAlchemy ORM 模型（Phase 1：organizations / users / conversations / messages）。

表结构依据 docs/05-database-design.md §2，字段名与 DDL 保持一致，便于对照阅读。

为什么 Phase 1 就建 organizations / users：
    conversations 的 organization_id / user_id 是 NOT NULL 外键，而 Phase 1 还没有登录
    （JWT 在 Phase 8）。所以先建这两张表，并在首个迁移里种一条固定的"开发身份"
    （UUID …0001/…0002，现由 scratch/auth_helpers 等测试侧常量引用）。
    Phase 8a 已接入真实鉴权（身份来源 = CurrentUser），表结构没动。
"""
import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from pgvector.sqlalchemy import Vector

from app.core.config import settings
from app.data.db import Base

# 主键统一写法：客户端生成 uuid4（插入后立刻能拿到 id，不用回库查），
# 同时保留 server_default 兜底 —— 手写 SQL 插入时不传 id 也能成功。
_pk = dict(
    default=uuid.uuid4,
    server_default=text("gen_random_uuid()"),
)
# 时间戳统一写法：交给数据库 now() 生成，避免应用服务器时区不一致
_created_at = dict(server_default=func.now())
_updated_at = dict(server_default=func.now(), onupdate=func.now())


class Organization(Base):
    """组织（多租户根）。Phase 1 只有一条开发用记录。"""

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    name: Mapped[str] = mapped_column(String(200))
    plan: Mapped[str] = mapped_column(String(50), server_default=text("'free'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_updated_at)


class User(Base):
    """用户。Phase 1 只有一条开发用记录，password_hash 暂时是占位值。"""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    email: Mapped[str] = mapped_column(String(255), unique=True)
    # Phase 1 不做登录，占位即可；Phase 8 换成 argon2/bcrypt 哈希
    password_hash: Mapped[str] = mapped_column(String(255), server_default=text("''"))
    full_name: Mapped[str | None] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), server_default=text("'member'"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_updated_at)


class AuditLog(Base):
    """审计日志（Phase 8a，spec §2.6）：auth 事件 + 审批决策 + 越权拒绝的留痕面。

    organization_id 可空是语义而不是偷懒：登录失败发生在"还不知道你是谁"的时刻——
    邮箱查不到用户就没有组织。宁可空着，也不往别人的组织名下塞假事件。
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        # 查询面固定是"本 org 按时间倒序翻页"（GET /auth/audit-log）
        Index("ix_audit_logs_org_time", "organization_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    # 事件名（register/login/login_failed/refresh_reuse/logout/approval_decide/
    # trigger_rejected/tool_denied）——开放字符串，新增事件不必修表
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)


class Conversation(Base):
    """会话（一次连续对话）。"""

    __tablename__ = "conversations"
    # 会话列表按 user 过滤、按 updated_at 倒序。
    # 索引只写升序即可：Postgres 的 btree 可以反向扫描，一样能服务 DESC 排序。
    __table_args__ = (Index("ix_conversations_user_updated", "user_id", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200), server_default=text("'新会话'"))
    # 会话使用的模型；NULL 表示用 settings.llm_model 的默认值
    model: Mapped[str | None] = mapped_column(String(50))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_updated_at)


class Message(Base):
    """会话内的一条消息（用户提问或模型回复）。"""

    __tablename__ = "messages"
    # seq 是会话内序号，前端按它渲染顺序；唯一约束顺带建了 (conversation_id, seq) 索引
    __table_args__ = (UniqueConstraint("conversation_id", "seq"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    # 删会话时消息一起删（外键 CASCADE），不留孤儿数据
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    # 冗余 org / user：按组织统计、数据隔离过滤时不用 JOIN conversations
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))

    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))  # user / assistant / system
    content: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(
        String(30), server_default=text("'text'")
    )  # text / markdown / structured
    structured: Mapped[dict | None] = mapped_column(JSONB)  # Structured Output 原样保留
    citations: Mapped[list | None] = mapped_column(JSONB)  # Phase 2 RAG 引用来源

    model: Mapped[str | None] = mapped_column(String(50))
    prompt_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost: Mapped[float] = mapped_column(Numeric(12, 6), server_default=text("0"))
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)


# ---------------- 任务 / 执行 / Agent 留痕（Phase 4：LangGraph Workflow） ----------------
#
# 三张表的关系（见 docs/05-database-design.md §2 / §5）：
#     tasks（任务实体，一件"要事"）1─N task_runs（每次执行一条，run_no 递增）1─N agent_runs（每个节点一条）
#
# 为什么 tasks 与 task_runs 拆两张：
#     同一件事会被重跑（Reviewer 打回、用户手动重试、worker 侧 sweeper 重入队），每次执行的状态 /
#     耗时 / 失败原因都不同，必须独立留痕才能对比"这次比上次好在哪"。
#     tasks.status 只是"最新一次执行的状态"的冗余，列表页一次查询就能显示，不用 JOIN 取 max。


class Task(Base):
    """任务实体（Phase 4）：用户发起的一次分析 / 一条 workflow / 一次文档总结。"""

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_org_created", "organization_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))

    title: Mapped[str | None] = mapped_column(String(200))
    # 原始问题必须落库：重跑时不用让用户重新问一遍，也是"这次执行到底在答什么"的锚点
    question: Mapped[str] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(String(30))  # agent_analysis / workflow / document_summary

    # 关联 workflow 定义（Phase 7 已建 workflows 表，补上外键）与产出报告（Phase 9b 建 reports 表）。
    # workflow_id 外键**不带 cascade**：删编目不牵连历史任务；可空性不变、不收紧。
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id")
    )
    # report_id（Phase 9b 起接上 reports 表的主键）：指向该任务**最新一份**报告，
    # 历史多轮报告按 task_run_id 查 reports 表。此前它是裸 uuid 占位（目标表不存在），
    # 本波的写入方是 report_service.write_for_terminal_run（终态落库同事务）。
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="SET NULL")
    )

    # Phase 7 心跳列（迁移 5ea259c3bdac）：长任务节点定期写入"我还活着"的时刻，
    # 供重启后判定哪些 run 真的断了（语义由后续任务给出，本层只管存取）
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 冗余"最新一次执行"的状态，列表页直接读；权威状态在 task_runs.status
    status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_updated_at)


class TaskRun(Base):
    """任务的一次执行（Phase 4）：一次图执行对外的留痕。

    与 Checkpointer 的分工（别混淆，两者都在"存状态"但不是一回事）：
        Checkpointer 存**图内部的状态快照**（state 里有什么就存什么），用途是"中断后接着跑"，
        属于机制层，可以换 saver、可以清；
        task_runs 存**这次执行对外的结果**（状态 / 进度 / 失败分类 / 耗时 / 终态快照），属于产品数据，
        供列表页、Trace 页、Phase 6 记忆与 Evaluation 长期读取。
        state 列就是那条「快照」：它是对外结果里唯一"完整白板"性质的一项，
        但仍然是这次执行的**产物**（跑完才写、只读不续跑），不是图内部的机制状态。
        checkpoint 扔了图还能从头发跑，task_runs 没了统计就断了 —— 所以不合并。

    状态机见 docs/05-database-design.md §5.1，失败分类取值见 §5.2。
    """

    __tablename__ = "task_runs"
    __table_args__ = (
        # 同一任务的每次执行必须可区分：既防并发起两次、也是"第几次跑"的权威编号
        UniqueConstraint("task_id", "run_no"),
        Index("ix_task_runs_status", "status"),
        # Phase 6 Evaluation 标记列的索引（与迁移 ef987cd499bb 一一对应）：
        # 产品任务列表按 run_type 过滤、评测聚合按 evaluation_run_id 定位
        Index("ix_task_runs_run_type", "run_type"),
        Index("ix_task_runs_eval_run", "evaluation_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE")
    )
    run_no: Mapped[int] = mapped_column(Integer, server_default=text("1"))

    status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"))
    # 失败分类（成功为 NULL）：model_timeout / tool_failure / output_validation / ...
    failure_category: Mapped[str | None] = mapped_column(String(50))
    progress: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # 0-100，供 SSE 推
    # 一次执行 = 一条 Trace，agent_runs / tool_calls 都带同一个 trace_id
    trace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    # {reviewer_verdict, retry_count, latency_ms, report, question, memory}
    # —— 跑完才知道的零散结论，不单独建列（question / memory 见 Phase 6：docs/11 §6）
    # workflow 家族另有两个键（Phase 7）：workflow_input（trigger 入参，建 run 时先落）
    # 与 result（_settle_terminal 合并写追加，reject 支路为 None；合并故 workflow_input 保留）
    meta: Mapped[dict | None] = mapped_column(JSONB)
    # 这次 run 跑完时的终态快照（Phase 6 短期记忆，docs/11 §3）：记忆的写入侧。
    # 与 meta 的分工：meta 是「给人看的零散结论」（report / verdict / latency），
    # state 是「给记忆读的完整白板」（plan / data_results / analysis / report…）。
    # 可空 = 迁移前的老任务本来就没有记忆，读到 NULL 时降级为无记忆开局（§7）。
    #
    # 为什么不用 LangGraph 的 checkpoint 表（§3 路线 A/B 的取舍，别重新论证一遍）：
    # 单池 / alembic 单主人 / 继承哪些键变成显式代码 / 快照是可 diff 的产品数据。
    # Checkpointer 继续只管「单次 run 内的中断恢复」，thread_id 仍是 run 级（§2 红线）。
    state: Mapped[dict | None] = mapped_column(JSONB)

    # ---- Phase 6 Evaluation 标记列（docs/08 §5.2：评测复用生产 Trace，不另建监控体系）----
    # product = 用户/接口发起的正常执行；evaluation = Runner 发起的评测执行。
    # 为什么要这个标记：评测跑出来的 task 会淹掉产品任务列表，而指标聚合又必须
    # 能在同一批表里定位到评测行 —— 用 run_type 过滤比另建一套 trace 表便宜得多。
    run_type: Mapped[str] = mapped_column(String(20), server_default=text("'product'"))
    # 归属哪次评测 Run（无 FK：task_runs 与 evaluation_runs 是"引用"而非"从属"，
    # 且删 Run 不该把执行痕迹删掉 —— 痕迹是 Trace 页与 cost 统计的原始数据）。
    evaluation_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)


class AgentRun(Base):
    """图中一个 Agent 节点的一次执行（Phase 4 建表，Phase 5 起逐节点写入）。

    Trace 树的一层：`task_runs(trace_id) 1─N agent_runs 1─N tool_calls`。
    span_id 与 tool_calls.span_id 共用同一个 id 空间 ——
    tool_calls.parent_span_id 指向所属 Agent 的 span_id，前端 Trace 页据此拼树。
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_task_run", "task_run_id"),
        Index("ix_agent_runs_trace", "trace_id"),
        Index("ix_agent_runs_parent_span", "parent_span_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    span_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, **_pk)

    trace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    parent_span_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # NULL = 根 span
    # 这里必须 CASCADE：删 task 的级联链是 tasks → task_runs → agent_runs，
    # 若这条 task_id 外键是默认的 NO ACTION，Postgres 会在删 task 时被它挡住：
    # "update or delete on table tasks violates foreign key constraint"（实测踩到）
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE")
    )
    task_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task_runs.id", ondelete="CASCADE")
    )

    agent_name: Mapped[str] = mapped_column(String(50))  # supervisor / data_analyst / ...
    status: Mapped[str] = mapped_column(String(20))  # ok / error

    # 存摘要不存全文：Trace 页要快，也避免把敏感原文长期留着
    input_summary: Mapped[str | None] = mapped_column(Text)
    output_summary: Mapped[str | None] = mapped_column(Text)

    duration_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    total_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost: Mapped[float] = mapped_column(Numeric(12, 6), server_default=text("0"))
    model: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolCall(Base):
    """工具调用记录（Phase 3）：一次工具调用的完整留痕。

    双重身份：Phase 3 是"这轮问答用了什么工具"的展示数据源；
    Phase 6 起是 Trace / Evaluation 的数据源（Tool Calling Success Rate 直接统计 status）。

    与 docs/05-database-design.md §2 的 DDL 有一处**刻意的差异**（Phase 4 复核后保留）：
        DDL 里 trace_id / parent_span_id / task_run_id 都是 NOT NULL，实际全部**可空且不再收紧** ——
        Chat 侧的一次问答没有 task / task_run，这三列对它是恒 NULL；
        强行 NOT NULL 就得给"非任务型调用"编假值，反而污染数据。
        span_id 在 Phase 3 只是个"唯一 id"，Phase 4 起才是 Trace 树的节点编号。

    为什么 Phase 3 建表时就带上这几列（而不是 Phase 4 再加）：
        加列要动一张已经有数据的表；带上可空列，Phase 4 只需补外键，列本身不用改。
    """

    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_conversation_created", "conversation_id", "created_at"),
        # Trace 组装用（某个 task_run 下有哪些工具调用）；顺带让 task_runs 的级联删除
        # 不用全表扫 tool_calls —— Postgres 不会给外键的"引用侧"自动建索引
        Index("ix_tool_calls_task_run", "task_run_id"),
        Index("ix_tool_calls_parent_span", "parent_span_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    span_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, **_pk)

    # Phase 3 的父级上下文：工具调用发生在某轮问答（某条会话、某条助手消息）里。
    # 都带 CASCADE：删会话/删消息时调用记录一起走，不留孤儿。
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE")
    )
    # 冗余 org / user：按组织统计、数据隔离过滤时不用 JOIN 两张表
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))

    # ---- Trace / 任务归属列（Phase 3 建表时带上，Phase 4 起开始写入）----
    # 全部可空：Chat 侧的调用没有 task_run，这几列对它恒为 NULL
    trace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    parent_span_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Phase 4 补外键（宿主表 tasks / task_runs 此时已存在）：挡住"写一个不存在的 task_run"
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE")
    )
    task_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task_runs.id", ondelete="CASCADE")
    )

    tool_name: Mapped[str] = mapped_column(String(100))
    tool_type: Mapped[str] = mapped_column(String(30))  # data / knowledge / research / business

    # 摘要给人和模型看（截断），json 原样落库供 Trace / 报告取用
    input_summary: Mapped[str | None] = mapped_column(Text)
    input_json: Mapped[dict | None] = mapped_column(JSONB)
    output_summary: Mapped[str | None] = mapped_column(Text)
    # str 也算在内：有的工具直接返回一段文本（data 是字符串），JSONB 存裸字符串是合法的
    output_json: Mapped[dict | list | str | None] = mapped_column(JSONB)

    rows_returned: Mapped[int | None] = mapped_column(Integer)
    truncated: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(20))  # ok / error
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[float] = mapped_column(Numeric(12, 6), server_default=text("0"))

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)


class Document(Base):
    """知识库文档（Phase 2 RAG）。存元数据，分块向量在 document_chunks。"""

    __tablename__ = "documents"
    # 文档列表页按组织过滤 + 状态筛选
    __table_args__ = (Index("ix_documents_org_status", "organization_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )

    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(500))
    file_type: Mapped[str] = mapped_column(String(20))  # pdf / docx / txt / md / csv / xlsx
    size_bytes: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    checksum: Mapped[str | None] = mapped_column(String(64))  # sha256，增量索引去重
    # status 流转：uploaded → parsing → chunking → embedding → ready | failed
    status: Mapped[str] = mapped_column(
        String(20), server_default=text("'uploaded'")
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_updated_at)


class DocumentChunk(Base):
    """文档分块 + 其 embedding 向量（Phase 2 RAG 检索的最小单元）。"""

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index"),
        # HNSW 向量索引：检索用余弦距离（`<=>` 操作符）。
        # v1 数据量小，直接在迁移里建；将来批量灌数据时可改为"灌完再建"
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # 检索时先按组织过滤（多租户隔离），再走向量索引
        Index("ix_document_chunks_org_id", "organization_id"),
        Index("ix_document_chunks_document_id", "document_id"),
        # Hybrid 全文侧的召回索引（docs/05 §8.2）。没有它 content_tsv 检索走全表扫描，
        # "索引建了没人用"和"列建了没人索引"是同一类假完成。
        Index(
            "ix_document_chunks_content_tsv_gin",
            "content_tsv",
            postgresql_using="gin",
        ),
    )

    # 用数据库自增 IDENTITY（BIGINT），不是客户端 uuid：
    # chunk 数量大、只在本文档内有序，不需要全局 uuid 的随机性
    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    # 冗余 org：检索时按组织过滤不用 JOIN documents
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # Hybrid 全文检索列：由 content 生成，随 content 自动更新，应用侧不用写它
    content_tsv = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', content)", persisted=True),
    )
    # 维度与 settings.embedding_dim 强绑定（config 里是 512），不要手写 1536
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.embedding_dim)
    )
    # 属性名必须叫 chunk_metadata：Declarative API 已占用 Base.metadata，
    # 直接命名 metadata 会抛 InvalidRequestError。列名仍保持 DDL 里的 metadata。
    chunk_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), **_created_at
    )


# ---------------- 企业模拟业务数据（Phase 3，只读） ----------------
#
# 用途：给 Data / Business Tools 提供"真实查库"的靶子（见 docs/05-database-design.md §3）。
# 全部**只读** —— 工具只查不写，写入只有种子脚本做。
#
# 与 DDL 草稿的三处**刻意修正**（草稿是 Phase 0 写的，没考虑多租户与查询形态）：
#   ① customers.code / products.sku 原为裸 UNIQUE —— 多租户下 A 组织和 B 组织
#      都会有自己的 "C001"，裸唯一约束会让第二个组织插不进去。改组合唯一。
#   ② organization_id 原为裸 UUID —— 其余所有表都 REFERENCES organizations(id)，
#      这里补齐 FK，否则可以写入一个不存在的组织 id。
#   ③ 补 §2 索引表里列出的分析索引（orders / sales 的日期维度），
#      否则"按区域看某月销售"这类查询会全表扫描。


class Region(Base):
    """区域（省/市），自引用形成层级树。"""

    __tablename__ = "regions"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )
    code: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(100))
    level: Mapped[str] = mapped_column(String(20), server_default=text("'province'"))
    # 自引用外键（省的 parent_id 为 NULL，市指向省）。
    # 因为 v1 不建 relationship，只写 FK 就够，不用 remote_side。
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("regions.id")
    )


class Customer(Base):
    """客户档案。tier / risk_level 是 Business Tool 按维度筛选的依据。"""

    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("organization_id", "code"),
        Index("ix_customers_org_region", "organization_id", "region_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )
    code: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(100))
    industry: Mapped[str | None] = mapped_column(String(100))
    region_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("regions.id")
    )
    tier: Mapped[str] = mapped_column(String(20), server_default=text("'standard'"))
    risk_level: Mapped[str] = mapped_column(String(20), server_default=text("'low'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), **_created_at
    )


class Product(Base):
    """产品。unit_cost 与 unit_price 分开存，才能让 SQL 算毛利。"""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("organization_id", "sku"),
        Index("ix_products_org_category", "organization_id", "category"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )
    sku: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(100))
    unit_price: Mapped[float] = mapped_column(
        Numeric(12, 2), server_default=text("0")
    )
    unit_cost: Mapped[float] = mapped_column(Numeric(12, 2), server_default=text("0"))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'active'"))


class Order(Base):
    """订单明细（一行一条，不做订单头/行两级 —— 见 DDL 草稿的说明）。"""

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_customer_date", "customer_id", "order_date"),
        Index("ix_orders_region_date", "region_id", "order_date"),
        Index("ix_orders_org_date", "organization_id", "order_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )
    order_no: Mapped[str] = mapped_column(String(50))
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id")
    )
    region_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("regions.id")
    )
    # DATE 而不是 TIMESTAMPTZ：经营分析按"天"聚合，存时间戳会带来时区换算的坑
    order_date: Mapped[date] = mapped_column(Date)
    quantity: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2))
    # 冗余存 amount，不每次 SUM(quantity * unit_price)：
    # 历史价格会变（product.unit_price 是当前价），下单时的成交额必须固化
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'completed'"))


class Sale(Base):
    """销售聚合明细（按日/区域/产品预聚合，便于 SQL 分析练习）。"""

    __tablename__ = "sales"
    __table_args__ = (
        Index("ix_sales_region_date", "region_id", "sale_date"),
        Index("ix_sales_product_date", "product_id", "sale_date"),
        Index("ix_sales_org_date", "organization_id", "sale_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )
    region_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("regions.id")
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id")
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id")
    )
    sale_date: Mapped[date] = mapped_column(Date)
    quantity: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    amount: Mapped[float] = mapped_column(Numeric(12, 2), server_default=text("0"))
    cost: Mapped[float] = mapped_column(Numeric(12, 2), server_default=text("0"))
    # profit 也固化：改成生成列的话，改成本口径要动 DDL
    profit: Mapped[float] = mapped_column(Numeric(12, 2), server_default=text("0"))


# ---------------- 评测（Phase 6 Evaluation，docs/08 §3.2） ----------------
#
# 与 docs/05 那张单表 evaluations 的出入见实施计划 D1：docs/05 自己预留了拆表的
# 口子（"若用例集需要复用，按 docs/08 拆"），落地补记已写进 docs/05 §2。


class EvaluationDataset(Base):
    """评测数据集（Phase 6 Evaluation，docs/08 §3.2）。

    与 docs/05 那张单表 `evaluations` 的出入见实施计划 D1：`docs/05:267` 自己
    预留了拆表的口子（"若用例集需要复用，按 docs/08 拆"），而用例复用正是
    回归对比的前提 —— 两次 Run 必须跑同一批用例，指标 diff 才有意义。
    """

    __tablename__ = "evaluation_datasets"
    __table_args__ = (
        Index("ix_eval_datasets_org_created", "organization_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # 本数据集覆盖的评测类别（docs/08 §2 五类里的子集）。本期恒为
    # ["multi_agent","tool_calling"]，chat/rag 未覆盖 —— 所以 Retrieval Quality 无源，
    # 指标层要显式标 not_applicable 而不是静默省略（Global Constraint 3）。
    category_scope: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_updated_at)


class EvaluationCase(Base):
    """一条评测用例（docs/08 §3.1）。code 是幂等键：seed 脚本重跑要能 upsert。"""

    __tablename__ = "evaluation_cases"
    __table_args__ = (
        UniqueConstraint("dataset_id", "code"),
        Index("ix_eval_cases_dataset", "dataset_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_datasets.id", ondelete="CASCADE")
    )
    code: Mapped[str] = mapped_column(String(60))  # MA-01 / TC-03…
    # chat / rag / tool_calling / multi_agent / workflow（docs/08 §2）
    category: Mapped[str] = mapped_column(String(30))
    input: Mapped[str] = mapped_column(Text)
    # {key_points: [...]} —— judge 逐条核对的依据（§5.3「带期望要点逐条核对」）
    expected: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # {data_scope: "...", doc_ids: [...]} —— 允许引用的数据/文档范围
    references: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # {must_include: [...], must_not_include: [...], tool_expectation: "..."}
    judgement: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    tags: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)


class EvaluationRun(Base):
    """一次评测执行（docs/08 §3.2 / §5.1）。进度写库里，前端靠轮询看（§7）。"""

    __tablename__ = "evaluation_runs"
    __table_args__ = (
        Index("ix_eval_runs_org_created", "organization_id", "created_at"),
        Index("ix_eval_runs_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    # SET NULL 而非 CASCADE：删数据集不许把历史基线一起带走（实测断言在 Step 2 第 5 段）
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_datasets.id", ondelete="SET NULL")
    )
    target: Mapped[str] = mapped_column(String(50), server_default=text("'agent'"))
    # 被评测对象的版本锚（docs/08 §6）：prompt/图/工具的 commit 或说明性标识。
    # 「改 Prompt 后重评测可对比」这条出口判据全靠它，不能留空。
    target_version: Mapped[str] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)
    # pending / running / completed / failed
    status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"))
    progress: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    case_total: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    case_done: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # 汇总指标（docs/08 §4 全量口径）。回归 diff 就是 diff 这个 JSONB。
    metrics: Mapped[dict | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)


class EvaluationResult(Base):
    """单条用例的结果（docs/08 §3.2）。下钻走 task_run_id → 生产 Trace（§8）。"""

    __tablename__ = "evaluation_results"
    __table_args__ = (
        UniqueConstraint("run_id", "case_no"),
        Index("ix_eval_results_run", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_runs.id", ondelete="CASCADE")
    )
    # SET NULL + case_snapshot：用例后来被改被删，这条结果仍知道"当时测的是什么"
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_cases.id", ondelete="SET NULL")
    )
    case_no: Mapped[int] = mapped_column(Integer)  # Run 内序号，diff 时对齐同一用例
    case_snapshot: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # 关联的执行痕迹（docs/08 §5.2 / §8）：评测不复制数据，只引用
    task_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task_runs.id", ondelete="SET NULL")
    )
    trace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # ok（跑完）/ failed（跑完但判分不过）/ error（链路炸了）—— 三态，别把 error 混进 failed
    status: Mapped[str] = mapped_column(String(20))
    passed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    score: Mapped[int | None] = mapped_column(Integer)  # 0-5（§5.3 分档）
    # auto / manual（§5.3 人工可覆盖并留痕）
    judge_source: Mapped[str] = mapped_column(String(20), server_default=text("'auto'"))
    reasons: Mapped[list | None] = mapped_column(JSONB)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost: Mapped[float] = mapped_column(Numeric(12, 6), server_default=text("0"))
    failure_category: Mapped[str | None] = mapped_column(String(50))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)


# ---------------- Workflow 目录与审批（Phase 7，迁移 5ea259c3bdac） ----------------


class Workflow(Base):
    """workflow 目录（Phase 7）：一行 = 一个可触发的图（预置或将来自定义）。

    graph_key 是注册表的键（sales_analysis / business_qa / doc_summary），
    全局唯一 —— 迁移种子靠 ON CONFLICT (graph_key) 幂等回放。
    """

    __tablename__ = "workflows"
    __table_args__ = (Index("ix_workflows_org", "organization_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )
    name: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    graph_key: Mapped[str] = mapped_column(String(50), unique=True)
    # 触发表单的字段声明，如 {"question": "string"} —— 前端按它渲染输入项
    input_spec: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)


class WorkflowApproval(Base):
    """审批门的一条记录（Phase 7）：图在审批节点 interrupt 挂起时落 pending，
    人批准/驳回后回写 decided_* —— 重启续跑靠它找回"等到哪一步了"。"""

    __tablename__ = "workflow_approvals"
    __table_args__ = (Index("ix_workflow_approvals_task", "task_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    # CASCADE：审批记录依附于任务，删任务即删审批痕迹
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE")
    )
    # 图中审批节点的名字（interrupt 发生处的 node 标识）
    graph_node: Mapped[str] = mapped_column(String(50))
    # pending / approved / rejected
    status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)


# ---------------- 报告（Phase 9b，迁移 a7c41f0b9de2） ----------------


class Report(Base):
    """产出的报告（Phase 9b，docs/05 §2 / docs/06 §2.5）。

    为什么现在才建这张表：Phase 5 的出口只要求「出报告 + Trace 可查」，当时报告正文
    落 `task_runs.meta.report` 就够了（`task_runner.py::_run_meta` 的注释即那次的裁定：
    「reports 表是后面 Phase 的事，这里不提前建」）。代价是**报告不是一等资源**——
    没有列表、没有按 id 取、没有来源跳转，`/reports` 页因此只能挂占位符。
    本表就是那个"后面的 Phase"。

    **与 `task_runs.meta.report` 的关系（重要，别读成两份真值）**：
    - 真值仍在 `task_runs`（`meta.report` 结构化 + `state.report` 白板），**本表是它的投影**：
      终态落库时由 `report_service.write_for_terminal_run` 从同一次执行的结论里拷一份，
      不参与任何执行/记忆/评测链路。删本表一行不影响任何执行事实（这是刻意的：
      投影丢了可以重建，真值丢了统计就断了——与 TaskRun docstring 里那条分工同一口径）。
    - `content` 存结构化字典（七字段，形状即 `ai/graph/nodes/report.py::Report`），
      `markdown` 存渲染用全文（消费者不必各自实现一遍字典→Markdown）。
    - `tasks.report_id` 这一列至此**才第一次有写入方**（此前是裸 uuid 占位，
      见 `Task.report_id` 的注释）；它指向最新一份，历史多轮报告仍在 `task_run_id` 上可查。
    """

    __tablename__ = "reports"
    __table_args__ = (
        # 列表页的主查询形态：本 org 按时间倒序分页（docs/05 §4 索引表同源）。
        # DESC 写法与 AuditLog 的 `ix_audit_logs_org_time` 同一款（`text("created_at DESC")`），
        # 且必须与迁移里的写法逐字一致，否则 autogenerate 会来回改这一条。
        Index("ix_reports_org_created", "organization_id", text("created_at DESC")),
        # 来源钻取：从一条执行找它的报告（Task 详情页的「看报告」入口）
        Index("ix_reports_task_run", "task_run_id"),
        Index("ix_reports_task", "task_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, **_pk)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))

    # 来源（可空）：报告可以脱离任务手工建（那是 9b 之后的事），但今天所有行都带来源。
    # SET NULL 而非 CASCADE —— 与 EvaluationResult.task_run_id 同一条裁定：
    # 删执行痕迹不该把已经交付出去的报告一起带走。
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL")
    )
    task_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task_runs.id", ondelete="SET NULL")
    )

    title: Mapped[str] = mapped_column(String(200))
    # analysis / workflow / summary（由图类型映射，见 report_service.REPORT_TYPES）
    report_type: Mapped[str] = mapped_column(String(30))
    # 结构化正文：{executive_summary, key_findings, data_evidence, root_causes,
    #              risks, recommendations, sources}（+ 降级支路的 {executive_summary, content}）
    content: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # 渲染用全文；结构化失败（降级支路）时内容仍在 content 里，这一列可空
    markdown: Mapped[str | None] = mapped_column(Text)
    # final / draft —— 今天只有 final（草稿态没有写入方，登记为 9b 之后的形状）
    status: Mapped[str] = mapped_column(String(20), server_default=text("'final'"))
    reviewer_verdict: Mapped[str | None] = mapped_column(String(20))
    total_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # 与 agent_runs.cost 同一条口径：未配置单价写 0 而不是 NULL（这列 NOT NULL）
    cost: Mapped[float] = mapped_column(Numeric(12, 6), server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_created_at)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_updated_at)
