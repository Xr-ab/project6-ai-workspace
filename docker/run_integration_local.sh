#!/usr/bin/env bash
# Phase 11b 集成层的本机入口（spec §5.3 R-ENV / §12 RK9）。
#
# 一句话形状：起一个**一次性 compose 项目 p6test**（只起 postgres + redis 两个服务），
# 把 DATABASE_URL / REDIS_URL 指到它的宿主端口（5433 / 6380），跑 pytest，
# 然后**只对这一个项目** down -v。默认项目（p6-postgres 与那四个真卷）全程不作为操作对象。
#
# 用法：bash docker/run_integration_local.sh [额外的 pytest 参数]
# 前置：Docker Desktop 在跑；5433 / 6380 空闲（默认项目在用的 5432/6379 不冲突，
#       conftest 的守卫恰恰要求它们**不**被本脚本使用）。
#
# 为什么不用 set -e：这条脚本对"清理"有分段的义务 ——
#   · up **之前**的失败（参数缺失 / 撞上真卷名 / 卷 project 标签不是 p6test）是 GUARD_ABORT：
#     直接退 8，**绝不** down -v（那正是守卫存在的意义：宁可不停也不删错东西）。此时 p6test 从没
#     被本脚本起过，端口也不归我们，无需端口证明。
#   · up **之后**的失败（compose up 失败、wait_healthy 超时、pytest 退出码非 0）一律经
#     post_up_abort / 第 5-6 段：先守卫式 down -v，再跑 port_check 证明 5433/6380 释放，
#     最后按优先级（pytest > 清理 > 端口）离开。绝不出现"p6test 还挂着就中途退、端口被占无痕迹"。
# 所以显式存 rc、显式判断，而不是 blanket `trap EXIT` 绕 die。
set -uo pipefail

cd "$(dirname "$0")/.."          # → project6-ai-workspace/（docker-compose.yml 所在）

ENV_FILE=docker/integration.env
PROJECT=p6test
VOL_KEYS=(P6_PGDATA P6_REDISDATA P6_MODELCACHE P6_UPLOADS)
# 默认项目那四个卷的**真名**（= compose 里 :- 的默认值）：撞上一个就 GUARD_ABORT
HOME_VOLUMES=(project6-ai-workspace_p6_pgdata project6-ai-workspace_p6_redisdata \
              project6-ai-workspace_p6_modelcache project6-ai-workspace_p6_uploads)
NEEDED=(P6_PG_CONTAINER P6_REDIS_CONTAINER P6_PG_HOST_PORT P6_REDIS_HOST_PORT "${VOL_KEYS[@]}")

die() { echo "GUARD_ABORT: $*" >&2; exit 8; }

# 端口释放证明（独立成函数：不只收尾段用，up 成功之后的失败路径也要跑它，
# 否则 p6test 还挂着时中途退出会留下 5433/6380 被占而无任何 PORT_STILL_HELD/RELEASED 痕迹）。
port_check() {
  "$PY" - "${P6_PG_HOST_PORT}" "${P6_REDIS_HOST_PORT}" <<'PYEOF'
import socket
import sys

for arg in sys.argv[1:]:
    port = int(arg)
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError as exc:
            print(f"PORT_STILL_HELD {port}: {exc}", file=sys.stderr)
            sys.exit(9)
        print(f"PORT_RELEASED {port}")
PYEOF
}

# up 成功之前，任何失败都是 GUARD_ABORT：直接退，**绝不** down -v（卷面标签守卫的意义所在）。
# up 成功之后（UP_DONE=1），失败必须先经 guarded_down + 端口证明再退。
# 刻意不做成 blanket `trap EXIT` 绕 die：那条卷面标签守卫触发的 GUARD_ABORT 不能被收尾段绕过。
UP_DONE=0
post_up_abort() {                 # $1=失败原因
  echo "GUARD_ABORT: $1（p6test 已 up：先守卫式清理，再证明端口，最后退 8）" >&2
  guarded_down || echo "清理段 guarded_down 亦非零退出（继续跑端口证明）" >&2
  port_check || true
  exit 8
}

# ---- 1. 八个参数成对齐全（缺一个就有一个回落默认值 = 家里的真名/真端口） ----
[ -f "$ENV_FILE" ] || die "$ENV_FILE 不存在（它被 .gitignore 的 *.env 命中，须 git add -f 才在库里）"
set -a; . "./$ENV_FILE"; set +a
for k in "${NEEDED[@]}"; do
  [ -n "${!k:-}" ] || die "$ENV_FILE 缺参数 $k"
done
for i in "${!VOL_KEYS[@]}"; do
  k=${VOL_KEYS[$i]}; v=${!k}
  [ "$v" != "${HOME_VOLUMES[$i]}" ] || die "$k=$v 撞上默认项目的真卷名"
done
[ "$P6_PG_HOST_PORT" != "5432" ] || die "P6_PG_HOST_PORT 不许是 5432"
[ "$P6_REDIS_HOST_PORT" != "6379" ] || die "P6_REDIS_HOST_PORT 不许是 6379"
echo "[1/6] 参数面齐全：pg:${P6_PG_HOST_PORT} redis:${P6_REDIS_HOST_PORT}"

# 解析 venv 解释器并转绝对（**提前到 up 之前**：up 成功之后的失败路径也要用 $PY 跑
# port_check 证明端口释放，不能等到第 4 段才定义）。下面要 cd 进 backend/，故转绝对。
PY=backend/.venv/Scripts/python.exe
[ -x "$PY" ] || PY=backend/.venv/bin/python
[ -x "$PY" ] || die "找不到 venv 解释器（backend/.venv）：先按 docs/01 建虚拟环境"
PY="$PWD/$PY"

# ---- 2. 卷面守卫式清理，然后 up（先清才有"全新空库"，test_migrations 才不是假话） ----
#
# 守卫形状沿用 11a 拆验收项目时的教训（docs/02 §10.7）：核验对象是**解析后的卷名**，
# 不是 `compose config --volumes` 给的 YAML 键名（那是键名，守卫会假红）。
guarded_down() {
  local v name label
  for v in "${VOL_KEYS[@]}"; do
    name=${!v}
    label=$(docker volume inspect --format \
      '{{ index .Labels "com.docker.compose.project" }}' "$name" 2>/dev/null)
    if [ $? -ne 0 ]; then
      continue                      # 卷不存在 = 没有东西可删，跳过（p6i_uploads 常不存在）
    fi
    [ "$label" = "$PROJECT" ] || die "卷 $name 的 project 标签是 '$label' 而非 $PROJECT —— 拒绝删除"
  done
  docker compose -p "$PROJECT" --env-file "$ENV_FILE" down -v
}
guarded_down || exit $?
docker compose -p "$PROJECT" --env-file "$ENV_FILE" up -d postgres redis \
  || post_up_abort "compose up 失败（默认项目不动；up 已尝试 ⇒ 守卫式清理 + 端口证明后再退）"
UP_DONE=1                          # 自此之后的失败都经 post_up_abort（guarded_down + 端口证明）收尾

wait_healthy() {                    # $1=容器名 $2=轮数（每轮 2s）
  local c=$1 n=$2 s=""
  for ((i = 0; i < n; i++)); do
    s=$(docker inspect --format '{{.State.Health.Status}}' "$c" 2>/dev/null || echo missing)
    [ "$s" = "healthy" ] && { echo "[2/6] $c healthy"; return 0; }
    sleep 2
  done
  post_up_abort "$c 在 $((n * 2)) 秒内没到 healthy（最后状态：$s）"
}
wait_healthy "$P6_PG_CONTAINER" 45
wait_healthy "$P6_REDIS_CONTAINER" 30

# ---- 3. 导出两条 URL ----
# 口令 app_dev_pwd 是 compose 里 inline 的开发值（也是 secret_scan 认得的占位形状），
# 不是任何真密钥；本脚本不读 backend/.env，因此也不把它带进子进程。
# PYTHONUTF8 不在这条启动脚本里导出：它属于 alembic 的**消费者**（conftest 的 migrated_schema
# 与 test_migrations 各在自己的子进程 env 上带 PYTHONUTF8=1）。放脚本里反而坑手工起 p6test
# 再裸跑默认命令的人（GBK 主机解析不了非 ASCII 的 alembic.ini ⇒ 假 RK1 的 pytest.exit(7)）。
export DATABASE_URL="postgresql+asyncpg://app:app_dev_pwd@localhost:${P6_PG_HOST_PORT}/ai_workspace"
export REDIS_URL="redis://localhost:${P6_REDIS_HOST_PORT}/0"
echo "[3/6] DATABASE_URL/REDIS_URL 已指向 $PROJECT（env 覆盖赢过 env_file，故 backend/.env 不干扰）"

# ---- 4. 跑测试（退出码先存住：清理段必须跑完） ----
# PY 已在第 1 段之后解析并转绝对（up 失败路径的 port_check 也要用）。
(cd backend && "$PY" -m pytest -m "not slow" "${@:1}")
rc=$?
echo "[4/6] pytest 退出码 = $rc"

# ---- 5. 守卫后清理（失败也要跑到端口证明再退，不再 `|| exit $?` 直接跳过第 6 段） ----
guarded_down
gd_rc=$?
if [ "$gd_rc" -ne 0 ]; then
  echo "清理段 guarded_down 退 $gd_rc（继续跑端口证明，最后按最高优先级码离开）" >&2
fi
echo "[5/6] $PROJECT 已 down -v（卷面守卫通过）"

# ---- 6. 端口释放证明（常驻纪律：不长期占端口，且要能证明） ----
port_check
irc=$?
echo "[6/6] 端口释放检查退出码 = $irc"
# 退出码优先级：pytest 失败 > 清理失败 > 端口未释放
final=$rc
[ "$final" -eq 0 ] && final=$gd_rc
[ "$final" -eq 0 ] && final=$irc
exit "$final"
