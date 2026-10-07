"""全局配置：所有配置从环境变量 / .env 加载，代码里不写死任何敏感值。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 运行环境：development / production
    app_env: str = "development"

    # 前端地址（逗号分隔），决定 CORS 放行范围
    cors_origins: str = "http://localhost:5173"

    # LLM Provider（OpenAI 兼容接口）
    # 默认值 = 本项目在用（智谱 GLM-4-Flash，免费档）；真实值一律从 .env 读
    llm_api_key: str = ""
    llm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    llm_model: str = "glm-4-flash"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 2048

    # 模型单价（Phase 6 Evaluation 的 Cost 指标）：每 1k token 多少「货币单位」。
    # 留空（None）= 未配置 → cost 一律算成 None，指标里 cost_available=false。
    # 为什么宁可空着也不在代码里写死默认值（实施计划 D5）：
    #   单价是外部事实，会随供应商调价变。代码里写死 = 半年后没人知道这数从哪来，
    #   而所有成本结论都建立在这个数上 —— 编一个数的代价是"看起来可信的假数据"。
    llm_input_price_per_1k: float | None = None
    llm_output_price_per_1k: float | None = None
    llm_price_currency: str = "CNY"

    # Embedding（Phase 2）：本地 ONNX 模型，免费无 key
    # 维度与 document_chunks.embedding 的 VECTOR(N) 强绑定：
    # 换模型若维度变了，必须同步改这里 + 新建迁移重建向量列，否则入库直接报错
    # 已实测：BAAI/bge-small-zh-v1.5 = 512（注意不是 384，384 是 bge-small-en / all-MiniLM-L6-v2）
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512

    # 外部检索（Phase 3 web_search）：博查 BoCha 的 API key（project5 同款，可复用）
    # 留空 = 未配置，web_search 会返回"未配置"的可读错误而不是崩掉（见 research_tools.py）
    bocha_api_key: str = ""

    # 上传文件落盘目录（Phase 2）
    # 相对 backend/ 运行目录；容器部署时用 UPLOAD_DIR 环境变量给绝对路径覆盖
    # （不要用 __file__ 往上数几层来算项目根：容器里的目录层级和本机不一样，会算错）
    upload_dir: str = "data/uploads"

    # 单个上传文件大小上限（MB）。解析和向量化都在请求内同步完成，
    # 不设上限的话一个超大文件会把整个 worker 占住
    max_upload_mb: int = 20

    # 数据库（Phase 1 起）：异步驱动 asyncpg
    # 注意：本机跑 uvicorn 用 localhost；容器内跑必须换成 compose 服务名 postgres
    # （容器里的 localhost 指向容器自己，连不上宿主机）
    database_url: str = (
        "postgresql+asyncpg://app:app_dev_pwd@localhost:5432/ai_workspace"
    )

    # ---- Phase 8a：身份与访问 ----
    # JWT 签名密钥：只从 .env 读，代码里不放任何默认密钥（带默认密钥上线 = 任何人都能铸 token）
    jwt_secret: str = ""
    # docs/06 §1.4：Access 短期（2h）、Refresh 7d 且 Redis 白名单可吊销
    access_token_ttl_minutes: int = 120
    refresh_token_ttl_days: int = 7
    # refresh 白名单存储；容器部署用 REDIS_URL 覆盖成 redis://redis:6379/0
    redis_url: str = "redis://localhost:6379/0"

    # ---- Phase 8b：异步底座与工程化 ----
    # worker 并发上限：零钱纪律的机制化——即便 glm 免费，批量评测也不该把在飞模型调用放飞
    worker_max_jobs: int = 4
    # sweeper 判僵尸阈值：与 task_repo.has_running_run 的 5min stale_before 同源常量
    # （spec §1.4——两处口径必须一把尺，否则判活与清扫互相打架）
    sweep_stale_minutes: int = 5
    # checkpoint 保留天数（Phase 7 欠账 #5）：终态 run 的 thread 快照超期即清
    checkpoint_retention_days: int = 30
    # 评测 run 无心跳列（models.py:710-745 实读），running 判定用started_at 硬上限，
    # 宽到不可能误杀真在跑的批量（10 用例 × 全链最坏实测远小于此；宁松勿紧——误杀 = 丢真结果）
    eval_run_max_minutes: int = 120
    # 日志级别：JSON Formatter 的 level 门槛，本地 INFO 起步
    log_level: str = "INFO"
    # 限流三档（spec §6 表；数值可调不写死在模块里）
    rate_limit_auth_per_min: int = 10
    rate_limit_task_per_min: int = 20
    rate_limit_upload_per_min: int = 10

    # ---- Phase 11b：#23 worker 中毒环 ----
    # 容器形态：worker 守护崩溃后直接退出（复活交给 restart: unless-stopped，
    # 全新进程 = 全新单例，最强保证）。默认 False = 本机 dev 形态保持进程内退避重启——
    # Windows 下没有 docker 监督者，退出就是永久下线（spec §8 修法 2 的双形态裁定）。
    # compose 的 worker 服务用 ${P6_WORKER_EXIT_ON_FATAL:-1} 置 1；docs/06 §? 由 T8 登记。
    worker_exit_on_fatal: bool = False

    # ---- Phase 10：MCP 外部数据域（企业库存与补货） ----
    # 库存域库名：与 database_url 同 host / 同 role / 同口令，只换最后一段库名。
    # 为什么存库名而不是整条 DSN：host 与口令只有一份来源，改 .env 不会出现「只改一处」的漂移。
    enterprise_db_name: str = "enterprise_data"
    # 单次 MCP call_tool 的硬上限。协议边界 + 独立进程 = 必须假设它会卡（spec §6：超时是硬需求不是加固）。
    mcp_tool_timeout_seconds: int = 10
    # MCP server 地址。compose 部署时用服务名覆盖成 http://mcp-server:8100/mcp；
    # 路径段 /mcp 与 server 侧 streamable_http_path 同源（spec §3.3）。
    mcp_enterprise_url: str = "http://127.0.0.1:8100/mcp"


settings = Settings()

# F3 生产密钥门（8b T10，裁定 R-8b-6）：production 且没配 JWT_SECRET = 部署面
# 根本不该起来 —— 空密钥下任何人都能铸 admin token，起得来反而是事故。
# development 保持现状：运行期首请求由 security._require_secret 炸（scratch
# 套件全数不设 APP_ENV，若在 import 面按「非测试即炸」的字面走，会把开发机与
# 全套件一起炸掉，而 spec 承诺的只是部署面起不来）。两针见 test_p8b_config_gate。
if settings.app_env == "production" and not settings.jwt_secret:
    raise RuntimeError(
        "production 环境启动被拒：JWT_SECRET 未配置。"
        "空密钥 = 任何人都能伪造 access token，请显式配置密钥后再部署。"
    )
