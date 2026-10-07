# project6-ai-workspace —— 企业级 AI 工作空间（FastAPI + LangGraph + RAG + Multi-Agent + Workflow）

一句话：上传企业文档、问它话、让它替你把活干完的 AI 工作空间。
后端 FastAPI + SQLAlchemy 2（async / asyncpg）+ LangGraph，前端 Vite + React + TypeScript（strict），依赖 PostgreSQL + pgvector、Redis、arq worker，另有一个 MCP 外部数据服务。全栈一条 compose 起得来。

## 能力清单

| 能力 | 一句话 |
|---|---|
| RAG 检索问答 | 向量 + 全文混合检索，RRF 融合，答案带引用来源 |
| Tool Calling | 工具进统一 registry，存在性/参数/权限三闸 + 全量计时落 `tool_calls` |
| Multi-Agent 与 Trace | Supervisor 动态调度，一次任务的调用树看得见谁调了模型 |
| Workflow 审批停等 | 图跑到审批节点真的停住等批准，状态字面量 `waiting_approval` |
| MCP 外部库存数据域 | 外部工具包成普通 `Tool` 进现有注册表，`tool_type="external"` |
| 记忆与评测、成本口径 | 终态快照注入 + 五类评测 + 单价表算 cost |
| 队列 + 限流 + 审计 | 长任务 202 入 arq 队列，Redis 滑窗限流，写请求全量审计 |

## 跑起来，两条路

### 形态 B：一条命令全栈（看效果用这条）

```bash
cd project6-ai-workspace
docker compose --env-file docker/acceptance.env -p p6accept up -d --build
```

打开 `http://localhost:8081`（nginx 同源反代 `/api/v1` 到 app；app 自己不发布宿主端口）。
停：

```bash
docker compose -p p6accept --env-file docker/acceptance.env down -v
```

**本批真跑过**（`backend/scratch/_p11c_t8_readme/up-build.log`、`ps-after-up.txt`、`down-v.log`）：七个服务里 `p6a-app / p6a-mcp / p6a-redis / p6a-postgres / p6a-worker / p6a-web` 全 `Up (healthy)`，`p6a-init-data` 是 one-shot、`Exited (0)`；`down -v` 之后 `p6a_*` 卷剩 0 枚、`5543/5637/8110/8081` 四门 FREE。探针读数（同目录 `probe-form-b.log`）：`/healthz` → 200 `{"status":"ok","service":"frontend"}`（nginx 静态地板）、`/api/v1/healthz` → 404（这一跳确实反代到了 app，app 没这个路径）、`/api/v1/agents/tasks` → 401 `{"code":"AUTH_401001",…}`（鉴权闸在反代后面是活的）。

⚠️ `-v` 只对 `p6accept` 这类一次性项目；**默认项目永不 `down -v`**。本批同时留了默认项目零改动证明：`home-census-before.txt` / `home-census-after.txt` 两份各 956 B，`home-census-diff.txt` **0 字节**。

### 形态 A：本机开发（改代码用这条）

前置：默认项目的 `p6-postgres` / `p6-redis` 已在跑；`backend/.venv` 已建；`backend/.env` 自备（见下面密钥纪律）。

```bash
cd project6-ai-workspace/backend
PYTHONPATH=. .venv/Scripts/python.exe run_dev.py      # 后端 8002
```

```bash
cd project6-ai-workspace/frontend
npm run dev                                           # 前端 5173
```

**本批真跑过**（`backend/scratch/_p11c_t8_readme/run-dev.log`、`npm-dev.log`、`probe-form-a.log`、`listeners-form-a.txt`）。两条实测要注意：

- `http://127.0.0.1:8002/healthz` → 200 `{"status":"ok","env":"development","postgres":true,"redis":true,"queue":true}`——`queue:true` 是 arq 入队通道可用，不是「worker 进程在跑」。
- **vite 只绑 IPv6 回环**：`npm run dev` 报 `ready in 838 ms` 在 `http://localhost:5173/`，但用 `127.0.0.1:5173` 探测是失败的，`localhost` 与 `[::1]` 才通。写脚本探活时按这条来。

### 登录账号从哪来

`backend/scripts/seed_dev_users.py` 种开发账号，口令走 `SEED_DEV_PASSWORD` 环境变量。**这条命令面本批只在同形状的一次性栈上证过**（`compose run -e SEED_DEV_PASSWORD init-data`，工件 `backend/scratch/_p11c_t7_demo1_retry2/identity-seed.txt`）；在家里默认库上重跑会覆盖已有用户口令，所以本批没重跑，家里跑请自备口令。

## 端口表（四套互不重叠是纪律，不是巧合）

| 用途 | postgres | redis | mcp-server | web / api |
|---|---|---|---|---|
| 本机 dev（`run_dev.py` + vite） | 用默认项目的 5432 | 用默认项目的 6379 | — | 后端 **8002** / 前端 **5173** |
| 本机默认项目（容器，`docker-compose.yml` 默认值） | 5432 | 6379 | `127.0.0.1:8100` | 前端 8080；app 不发布宿主端口 |
| 验收/演示一次性项目（`docker/acceptance.env`） | **5543** | **5637** | **`127.0.0.1:8110`** | **8081** |
| 集成测试一次性项目（`p6test`） | **5433** | **6380** | — | — |

出处：`docker/acceptance.env:18-21`（四个端口键）、`docker/run_integration_local.sh` 的 `PROJECT=p6test` 与端口段。mcp 那两枚刻意带 `127.0.0.1:` 前缀——`/mcp` 无鉴权，裸发布等于把跨组织库存读口对局域网打开。

## ⚠️ 双形态互斥

容器 worker 起来时**别再**在本机 `python -m app.workers`：两个进程连同一个 Redis 队列会抢消费，任务归因就说不清了（原文纪律在 `docker-compose.yml:169-170`）。

## 测试（两条命令 + 本批现值）

```bash
cd project6-ai-workspace/backend && .venv/Scripts/python.exe -m pytest -m "not db and not redis" -q
```

```bash
cd project6-ai-workspace && bash docker/run_integration_local.sh
```

本批实测（两条独立绿跑：`backend/scratch/_p11c_t8_readme/` 起文档时那次，与记账前重跑的 `backend/scratch/_p11c_t9_gates/`；**通过数一致，秒数是两次各自的**）：

- 离线层：**`185 passed, 47 deselected, 1 warning`**（7.20s / 7.16s，两份 `offline-gate.txt`）
- 三层门：**`232 passed, 7 warnings`**（= 185 + 那 47 条 `db`/`redis`；16.43s / 14.69s，两份 `integration-local.log`；跑完自动 `down -v`，`5433/6380` 已证 `PORT_RELEASED`、卷面守卫通过）
- 密钥门禁：`backend/.venv/Scripts/python.exe backend/scripts/secret_scan.py` → **`SCANNED 285 READ 285 SKIPPED 0 HITS 0`**（`_p11c_t9_gates/secret-scan.txt`）。**为什么是 285 不是起文档那次的 283**：扫描面就是 `git ls-files` 的清单（`backend/scripts/secret_scan.py:136`），未跟踪的文件在门外面；本批新入库的 `README.md` 与 `DELIVERY.md` 各贡献 1，所以顺序是**先 `git add` 再扫**，否则新文档根本没被扫过。
- 前端：`cd frontend && npm test -- --run` → `Test Files 2 passed (2)` / **`Tests 21 passed (21)`**；`npm run build` → `✓ built in 1.69s`（复跑 1.55s），TS strict 零错误

> **2026-10-03 修复波 + Phase 9b 现值**（**只更离线与前端的数**）：离线 **`227 passed, 47 deselected, 1 warning`**（185 +16 +26）、前端 **`Tests 37 passed（3 files）`**（21 +16）+ `npm run build` 与 `npx tsc --noEmit` 均零错误；密钥门禁 **`SCANNED 302 READ 302 SKIPPED 0 HITS 0`**（对着已提交的面重扫）。**三层那 `232` 未重测**（本机 Docker 未起、5432/6379 无监听，`db`/`redis` 那 47 条本轮跑不到）⇒ 不改它的数、也不假勾。**已入库两笔**：`ec9e1cf`（`?limit=-1` 500 改判 422 + R56 报告呈现区 + 文档漂移对账）、`07db367`（Phase 9b Reports 三件套）。新增针：`test_report_render_contract.py`、`test_list_pagination_bounds.py`、`test_report_layer.py`、`taskReport.test.ts`。（详细验证记录为内部资料，不随库发布。）

## 密钥纪律

- `backend/.env` 只在本地、**永不入库**（`.gitignore` 已挡，`secret_scan.py` 再盯一遍形状）。
- `.env.example` 是模板：加新键先加模板，别把真值抄进去。
- compose 里 `environment:` 的优先级**高于** `env_file:`——列一个空值就等于把真值顶成空（原文在 `docker-compose.yml:103-105`）。
- 验收/演示项目用 `docker/acceptance.env`（只有端口、卷名、容器名，无密钥）；一次性口令只在脚本进程环境里，命令行只出现变量名。
