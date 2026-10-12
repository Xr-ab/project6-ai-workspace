# Project 6 · 企业级 AI 工作空间

**上传企业文档 → 向它提问 → 让它把活干完** 的 AI 工作空间：RAG 检索问答、工具调用、多 Agent 协作、人工审批工作流、异步任务与审计，全栈一条 Docker Compose 起得来。

> 定位：把「单个 Agent 能跑通」推进到「一套多租户可运营的 AI 平台」——有权限闸、有审批停等、有队列与限流、有成本口径、有评测与密钥门禁。

---

## 一、核心亮点

| 亮点 | 实现要点 | 为什么这么做 |
|---|---|---|
| **混合检索 + RRF 融合** | pgvector 向量检索与全文检索并行，RRF 融合排序，答案带引用来源 | 单一向量检索对精确术语/编号召回差，混合检索互补 |
| **工具调用三闸 + 全量落表** | 统一 registry（`register` / `execute` / `to_openai_schema`）；执行前过存在性、参数、权限三道闸；每次调用计时落 `tool_calls` | 工具是 Agent 最容易出事的地方：越权、错参、慢调用都需要可归因 |
| **多 Agent 协作与调用树** | Supervisor 动态调度子 Agent（`agent_team` ↔ `from_team` / `to_team` 节点），一次任务的调用树可见 | 复杂任务拆解需要「谁调了模型、花了多少」说得清 |
| **工作流审批停等** | 图执行到审批节点**真的停住**，状态字面量 `waiting_approval`，批准后从断点续跑 | 企业场景不能全自动：高风险动作必须留人工闸门 |
| **MCP 外部数据域** | 外部 MCP 工具包成普通 `Tool` 进既有注册表，`tool_type="external"`，权限与计时逻辑复用 | 新增数据源不改动 Agent 侧代码，扩展成本恒定 |
| **异步任务与稳定性** | 长任务 202 入 arq 队列、Redis 滑窗限流、写请求全量审计、超时任务有 sweeper 兜底 | 请求路径要快，重活丢队列；限流与审计是上线前提 |
| **成本与评测口径** | 五类评测 + 单价表折算 cost，按整轮 token 现算 | 「答得好不好、花了多少钱」要可量化，否则优化无从下手 |

---

## 二、系统架构

```
                    ┌──────────────────────────────┐
   Browser  ──────▶ │  nginx（前端静态站 + 同源反代） │  web :8080
                    └───────────────┬──────────────┘
                                    │ /api/v1/*
                    ┌───────────────▼──────────────┐
                    │  FastAPI（app 不发布宿主端口） │  app
                    │  auth / chat / documents      │
                    │  agents / workflows / reports │
                    │  evaluations / stats          │
                    └───┬───────────┬───────────┬───┘
                        │           │           │
        ┌───────────────▼──┐  ┌─────▼─────┐  ┌──▼─────────────┐
        │ LangGraph        │  │ PostgreSQL│  │ Redis + arq    │
        │  ├ RAG（混合检索）│  │ +pgvector │  │ 队列 / 限流     │
        │  ├ Tools(registry)│ │ 11 迁移   │  │ worker/sweeper │
        │  └ Workflow(审批) │ └───────────┘  └────────────────┘
        └────────┬─────────┘
                 │ MCP
        ┌────────▼─────────┐
        │ 外部库存数据服务  │  mcp-server（仅绑 127.0.0.1）
        └──────────────────┘
```

**分层**：`api`（路由） → `application`（用例） → `data/repositories`（持久化） / `ai`（RAG、工具、图） / `mcp`（外部域） / `workers`（队列）。

---

## 三、技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python · FastAPI · SQLAlchemy 2（async / asyncpg） |
| Agent | LangGraph（多 Agent、工作流、checkpointer 记忆） |
| 检索 | PostgreSQL + pgvector（向量）＋全文检索，RRF 融合 |
| 队列 / 缓存 | Redis · arq worker · 滑窗限流 |
| 外部数据 | MCP 服务（库存域），统一进工具注册表 |
| 前端 | Vite · React · TypeScript（strict）· react-router · zustand · react-markdown |
| 部署 | Docker Compose（7 服务）＋ nginx 同源反代 |

---

## 四、快速开始

### 方式 B：一条命令全栈（看效果用这条）

```bash
docker compose --env-file docker/acceptance.env -p p6accept up -d --build
```

打开 `http://localhost:8081`（nginx 同源反代 `/api/v1` 到 app；**app 自己不发布宿主端口**，只经反代暴露）。

```bash
# 停止并清理（仅限一次性项目）
docker compose -p p6accept --env-file docker/acceptance.env down -v
```

### 方式 A：本机开发（改代码用这条）

前置：默认项目的 PostgreSQL / Redis 已在跑；`backend/.env` 自备（见第七节密钥纪律）。

```bash
cd backend && PYTHONPATH=. .venv/Scripts/python.exe run_dev.py    # 后端 8002
cd frontend && npm install && npm run dev                         # 前端 5173
```

### 端口规划（四套环境互不重叠）

| 用途 | postgres | redis | mcp-server | web / api |
|---|---|---|---|---|
| 本机 dev | 复用默认项目 5432 | 复用默认项目 6379 | — | 后端 **8002** / 前端 **5173** |
| 本机默认项目（容器） | 5432 | 6379 | `127.0.0.1:8100` | 前端 8080（app 不发布端口） |
| 验收/演示（一次性） | **5543** | **5637** | **`127.0.0.1:8110`** | **8081** |
| 集成测试（一次性） | **5433** | **6380** | — | — |

> mcp-server 的端口**刻意加 `127.0.0.1:` 前缀**：`/mcp` 无鉴权，裸发布会把跨组织数据读口对局域网打开。

### ⚠️ 两种形态互斥

容器 worker 运行时，**不要**再在本机起 `python -m app.workers`——两个进程连同一个 Redis 队列会抢消费，任务归因就说不清了。

---

## 五、能力明细

### 内置工具（14 个，按域分组）

| 域 | 工具 |
|---|---|
| 知识库 | `rag_search`、`document_retriever` |
| 数据处理 | `csv_reader`、`excel_reader`、`data_statistics`、`python_calculator`、`sql_query` |
| 业务查询 | `query_customer`、`query_product`、`query_sales` |
| 外部（MCP） | `query_stock_levels`、`query_stock_movements`、`suggest_replenishment` |
| 研究 | `web_search` |

所有工具经同一 registry 注册，执行前过三闸（存在性 / 参数 / 权限），调用计时与结果落 `tool_calls` 表。

### 工作流图节点

`analyze` → `retrieve` → `ingest` → `summarize` → `draft` → `report` → `final_report` → `deliver`；多 Agent 分支 `agent_team` / `from_team` / `to_team`；审批不通过走 `reject_end`。

### 前端页面

`Dashboard`（总览）、`chat`（对话）、`Knowledge`（知识库）、`AgentTasks`、`Workflow`、`Evaluation`、`Reports`、`Settings`、`Login`。

### API 规模

**47 个端点，分 9 个路由模块**：`auth`(8)、`agents`(10)、`evaluations`(11)、`documents`(5)、`conversation`(4)、`reports`(3)、`workflows`(3)、`stats`(2)、`chat`(1)。

---

## 六、工程质量（可复现的量化数据）

最近一次全量验证 **2026-10-03**：

| 项目 | 结果 | 命令 |
|---|---|---|
| 后端离线单测 | **227 passed**（47 deselected 为 db/redis 依赖用例） | `pytest -m "not db and not redis" -q` |
| 后端三层集成 | **232 passed**（含 db/redis 全量） | `bash docker/run_integration_local.sh` |
| 前端单测 | **37 passed**（3 个测试文件） | `npm test -- --run` |
| 前端类型检查 | `tsc --noEmit` **零错误** | 同上 |
| 前端构建 | `npm run build` 通过（约 1.7s） | — |
| 密钥扫描门禁 | `SCANNED 302 · HITS 0` | `python backend/scripts/secret_scan.py` |

规模：后端 29 个测试文件、11 个数据库迁移、7 个容器服务、109 个应用层 Python 文件；前端 54 个 TS/TSX 文件、4 个测试文件。

---

## 七、密钥与安全纪律

- `backend/.env` **只在本地、永不入库**：`.gitignore` 先挡一刀，`secret_scan.py` 再按形状扫一遍（扫描面 = `git ls-files` 清单，所以**新文件要先 `git add` 再扫**，否则它在门外面）。
- `.env.example` 是模板：加新键先加模板，绝不把真值抄进去。
- compose 里 `environment:` 的优先级**高于** `env_file:`——列一个空值就等于把真值顶成空。
- 验收/演示项目用 `docker/acceptance.env`（只有端口、卷名、容器名，**无密钥**）；一次性口令只存在于脚本进程环境里，命令行只出现变量名。
- 无鉴权的 `/mcp` 只绑回环地址，不对外发布。

---

## 八、目录结构

```
backend/
├── app/
│   ├── api/            # 9 个路由模块，47 端点
│   ├── application/    # 用例编排
│   ├── ai/
│   │   ├── rag/        # parser / chunker / retriever / citation
│   │   ├── tools/      # registry + 权限闸 + 四域工具
│   │   └── graph/      # 多 Agent 图 / 工作流 / checkpointer / memory
│   ├── data/repositories/
│   ├── mcp/            # 外部库存数据域（server + inventory_data）
│   ├── schemas/  core/  workers/   # workers: jobs / sweeper
│   └── main.py
├── migrations/         # 11 个迁移
├── scripts/            # seed_dev_users.py / secret_scan.py 等
└── tests/              # 29 个测试文件
frontend/src/
├── pages/ components/ hooks/ stores/ router/ api/ lib/ types/
docker/                 # acceptance.env / integration.env / 两个 runner 脚本
docker-compose.yml      # 7 服务
```
