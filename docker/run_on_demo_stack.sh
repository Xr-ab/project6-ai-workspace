#!/usr/bin/env bash
# Phase 11c：一次性 demo 栈的全生命周期入口（spec §3.3 / §4）。
#
# 一句话形状：复用 acceptance.env 的参数面（容器名 p6a-* / 端口 5543,5637,8110,8081 / 卷名 p6a_*），
# 但项目名换成 p6demo，起栈 → 种一次性身份 → 跑内层命令 → **拆栈前**抓日志与数据普查 →
# 守卫式 down -v → 端口释放证明。默认项目（p6-postgres 与那四个 project6-ai-workspace_p6_* 真卷）
# 全程只被「读」，绝不作为删除动作的参数对象。
#
# 用法：bash docker/run_on_demo_stack.sh --out backend/scratch/_p11c_t4 [--rl 600] -- <内层命令...>
# 前置：Docker Desktop 在跑（docker info 退 0）；5543/5637/8110/8081 空闲。
#
# 为什么不用 set -e（与 run_integration_local.sh 同一条理由）：这条脚本对「清理」有分段义务——
#   · up 之前的失败是 GUARD_ABORT：直接退 8，**绝不** down -v（守卫存在的意义就是宁可不停也不删错东西）；
#   · up 之后的失败一律经 post_up_abort：先抓日志与普查，再守卫式 down -v，再证明端口，最后按优先级离开。
# 绝不出现「p6demo 还挂着就中途退、端口被占而无痕迹」。
# 信号面同一条义务（修复轮 1 R1）：INT/TERM 被 trap 住并走**同一个** post_up_abort 路径——
#   up 之前收到信号 = 什么都不删直接退 8；up 之后收到信号 = 取证 → 守卫式清理 → 端口证明 → 退 8。
#   CLEANED 是幂等闸：清理已经开跑后再来信号只忽略，绝不第二次 teardown。
set -uo pipefail

cd "$(dirname "$0")/.."          # → project6-ai-workspace/

ENV_FILE=docker/acceptance.env
PROJECT=p6demo                   # 刻意不叫 p6accept：与 11a 的验收项目区分开，日志/工件不说谎
SERVICES=(postgres redis init-data app worker mcp-server frontend)
NEEDED=(P6_PG_CONTAINER P6_REDIS_CONTAINER P6_APP_CONTAINER P6_WORKER_CONTAINER \
        P6_MCP_CONTAINER P6_WEB_CONTAINER P6_INIT_CONTAINER \
        P6_PG_HOST_PORT P6_REDIS_HOST_PORT P6_MCP_HOST_PORT P6_WEB_HOST_PORT \
        P6_PGDATA P6_REDISDATA P6_MODELCACHE P6_UPLOADS)
# 默认项目那四个卷的**真名**。demo 栈的删除面里出现任何一个 = GUARD_ABORT。
HOME_VOLUMES=(project6-ai-workspace_p6_pgdata project6-ai-workspace_p6_redisdata \
              project6-ai-workspace_p6_modelcache project6-ai-workspace_p6_uploads)
VOL_KEYS=(P6_PGDATA P6_REDISDATA P6_MODELCACHE P6_UPLOADS)
# 本脚本要删除的对象**只允许**是这两个项目名之一。出现第三个名字 = GUARD_ABORT。
ALLOWED_PROJECTS=(p6demo p6test)

# die 必须在使用它的参数解析**之前**定义（bash 的函数不 hoisting：顺序错了第一条
# 「未知参数」就会报 die: command not found，把守卫变成噪音）。
die() { echo "GUARD_ABORT: $*" >&2; exit 8; }

USAGE="用法：bash docker/run_on_demo_stack.sh --out DIR [--rl N] -- <内层命令...>（rc 面只有 0 / 内层码 / 8 / 9）"
OUT=""
RL="${P6_RL_TASK_PER_MIN:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    # 缺值的 --out/--rl 以前会踩到 shift 2 失败后的 set -u（$2 未绑定 → 退 1，一个文档里没写的码），
    # 现在一律先查 $# 再取值：坏用法走 die() = GUARD_ABORT 面 8，且打一行用法提示。
    --out)
      [ $# -ge 2 ] || die "--out 需要一个值（$USAGE）"
      OUT=$2; shift 2 ;;
    --rl)
      [ $# -ge 2 ] || die "--rl 需要一个值（$USAGE）"
      case "$2" in ''|*[!0-9]*) die "--rl 要的是非负整数，收到 '$2'（$USAGE）" ;; esac
      RL=$2; shift 2 ;;
    --)    shift; break ;;
    *)     die "未知参数 $1（$USAGE）" ;;
  esac
done

[ -n "$OUT" ] || die "--out 必填（工件目录；建议 backend/scratch/_p11c_*——未跟踪，但 .gitignore 里没有 scratch 规则，所以只能靠显式路径提交兜底）"
mkdir -p "$OUT" || die "--out $OUT 建不出来"
OUT_DIR_IN="$OUT"
OUT="$(cd "$OUT" && pwd -P)" || die "--out '$OUT_DIR_IN' 解析绝对路径失败（$USAGE）"
[ -n "$OUT" ] || die "--out '$OUT_DIR_IN' 解析后为空（子 shell 被吞了？$USAGE）"
case "$OUT" in
  /*) : ;;
  *)  die "--out '$OUT_DIR_IN' 解析出的不是绝对路径：'$OUT'" ;;
esac
# 一个 --out 目录 = 一次生命周期。receipts.txt 全程用 >> 追加，复用旧目录会把两次运行的行
# 叠在同一份收据里，后面的 Task 读到的就是混合收据（T2 修复轮 1 复跑同一目录时实测到）。
# 所以每次运行开头先把它清空重开；其余工件本来就是各自命令重写的，不受影响。
# 重开必须排在「有没有内层命令」这道检查**之后**：写在前面时，一次敲错参数的运行
# 就已经把上一次留下的收据抹掉了（修复轮 2 的 Low 项）。
[ $# -gt 0 ] || die "-- 后面必须给内层命令"
: > "$OUT/receipts.txt"

[ -f "$ENV_FILE" ] || die "$ENV_FILE 不存在"
set -a; . "./$ENV_FILE"; set +a
for k in "${NEEDED[@]}"; do
  [ -n "${!k:-}" ] || die "$ENV_FILE 缺参数 $k"
done

# ---- 守卫 1：撞名检查（卷名与项目名都不许指向默认项目） ----
for i in "${!VOL_KEYS[@]}"; do
  k=${VOL_KEYS[$i]}; v=${!k}
  [ "$v" != "${HOME_VOLUMES[$i]}" ] || die "$k=$v 撞上默认项目的真卷名"
done
for p in "${ALLOWED_PROJECTS[@]}"; do
  [ "$PROJECT" != "$p" ] || PROJECT_OK=1
done
[ "${PROJECT_OK:-}" = "1" ] || die "PROJECT=$PROJECT 不在允许名单 ${ALLOWED_PROJECTS[*]}"
[ "$P6_PG_HOST_PORT" != "5432" ] || die "P6_PG_HOST_PORT 不许是 5432（默认项目在用）"
[ "$P6_REDIS_HOST_PORT" != "6379" ] || die "P6_REDIS_HOST_PORT 不许是 6379"

# ---- 守卫 2：acceptance.env 里的端口必须是演示端口 ----
[ "$P6_WEB_HOST_PORT" = "8081" ] || die "P6_WEB_HOST_PORT=$P6_WEB_HOST_PORT，期望 8081（内层脚本按它硬写 P6_ORIGIN）"

PY="$PWD/backend/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$PWD/backend/.venv/bin/python"
[ -x "$PY" ] || die "找不到 venv 解释器（backend/.venv）"

echo "[1/8] 参数面齐全：project=$PROJECT pg:${P6_PG_HOST_PORT} web:${P6_WEB_HOST_PORT} rl=${RL:-默认20}"

# ---- 守卫 3：默认项目只读普查（before） + demo 端口此刻必须空闲（反向 port_check） ----
"$PY" backend/scripts/census_tables.py --db ai_workspace --db enterprise_data --host-port 5432 \
  --out "$OUT/home-census-before.txt" > "$OUT/home-census-before-stdout.txt" 2>&1 \
  || die "默认项目普查失败（不该是删除，但读都读不到就先别说零改动）"
"$PY" - "$P6_PG_HOST_PORT" "$P6_REDIS_HOST_PORT" "$P6_WEB_HOST_PORT" "$P6_MCP_HOST_PORT" <<'PYEOF'
import socket, sys
held = []
for arg in sys.argv[1:]:
    port = int(arg)
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError as exc:
            held.append(f"PORT_PREHELD {port}: {exc}")
if held:
    print("\n".join(held), file=sys.stderr)
    sys.exit(9)
print("PORTS_FREE_BEFORE_UP " + " ".join(sys.argv[1:]))
PYEOF
[ $? -eq 0 ] || die "demo 栈端口在 up 之前就被占（先弄清是谁占的，别把别人的栈拆了）"

port_check() {
  "$PY" - "${P6_PG_HOST_PORT}" "${P6_REDIS_HOST_PORT}" "${P6_WEB_HOST_PORT}" "${P6_MCP_HOST_PORT}" <<'PYEOF'
import socket, sys
bad = []
for arg in sys.argv[1:]:
    port = int(arg)
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError as exc:
            print(f"PORT_STILL_HELD {port}: {exc}", file=sys.stderr)
            bad.append(port)
        else:
            print(f"PORT_RELEASED {port}")
sys.exit(9 if bad else 0)
PYEOF
}

guarded_down() {
  local v name label
  for v in "${VOL_KEYS[@]}"; do
    name=${!v}
    label=$(docker volume inspect --format \
      '{{ index .Labels "com.docker.compose.project" }}' "$name" 2>/dev/null)
    if [ $? -ne 0 ]; then
      continue                      # 卷不存在 = 没东西可删（全新空卷生命周期的正常形状）
    fi
    if [ "$label" != "$PROJECT" ]; then
      # R2（修复轮 1）：这里从前是 die() —— 在本函数最后一个调用点（收尾拆栈）里它直接 exit 8，
      # 于是端口证明、收据、home-census-after 全被跳过，还把非零的内层 rc 盖掉（优先级倒置）。
      # 守卫**照旧拒绝删除**，但将拒绝变成返回值：调用点存住 rc、按既有优先级最后决定离开码。
      # GUARD_ABORT=8 仍然是「守卫拒绝删除」这个意思，且拒绝在 stderr 与 receipts.txt 两处都留痕。
      echo "GUARD_ABORT: 卷 $name 的 project 标签是 '$label' 而非 $PROJECT —— 拒绝删除（本函数不执行拆栈，把码交回调用点）" >&2
      echo "guard_refused_volume=$name label='$label' expected=$PROJECT rc=8" >> "$OUT/receipts.txt"
      return 8
    fi
  done
  docker compose -p "$PROJECT" --env-file "$ENV_FILE" down -v
}

UP_DONE=0
CLEANED=0
INNER_RC_KNOWN=0                   # 第 6 段跑完内层命令才置 1：post_up_abort 的优先级要用它判断
rc=0
# collect_evidence：拆栈前必做——日志与普查在 down -v 之后就没了（注释单独成行，
# 不挂在函数定义行上：内联注释会让「被执行行」混进扫描噪音，脚本半边的针只认纯代码行）。
collect_evidence() {
  # R4（修复轮 1）：这一段每一条都曾经是 `|| true` —— 工件缺席也能整体退 0，
  # 后面的 Task 读到「绿」却没有证据可查。现在每条子调用的 rc 都落进 receipts.txt，
  # 退出行为不变（取证面不许把生命周期带偏），但缺件在收据上留名。
  local ps_rc worker_rc app_rc census_rc hist_rc
  docker compose -p "$PROJECT" --env-file "$ENV_FILE" ps --all > "$OUT/ps.txt" 2>&1
  ps_rc=$?
  docker logs "$P6_WORKER_CONTAINER" > "$OUT/worker.log" 2>&1
  worker_rc=$?
  [ "$worker_rc" -eq 0 ] || echo "worker 日志抓取失败" > "$OUT/worker.log"
  docker logs "$P6_APP_CONTAINER" > "$OUT/app.log" 2>&1
  app_rc=$?
  [ "$app_rc" -eq 0 ] || echo "app 日志抓取失败" > "$OUT/app.log"
  (cd backend && "$PY" scripts/census_tables.py --db ai_workspace --db enterprise_data \
      --host-port "$P6_PG_HOST_PORT" --out "$OUT/census-after.txt") >> "$OUT/census-after-stdout.txt" 2>&1
  census_rc=$?
  # 任务状态直方图（spec §9 判据 2 第三条「任务停在 queued」的一手读数）：
  # census 只给逐表**行数**，答不了「这些行是什么状态」，所以这里按 status 分组再数一次。
  # 只读 SELECT、DSN 经 settings 构造（口令不经命令行）、输出全 ASCII ⇒ 不新增守卫针：
  # 本段没有删除动作，针只盯删除面与凭据面。
  (cd backend && "$PY" - "$P6_PG_HOST_PORT" "$OUT/task-status-histogram.txt" <<'PYEOF'
import asyncio
import sys

import asyncpg

from scripts.census_tables import build_dsn
from app.core.config import settings


async def go(port: int, out: str) -> None:
    dsn = build_dsn(settings.database_url, "ai_workspace", host="localhost", port=port)
    conn = await asyncpg.connect(dsn)
    try:
        lines: list[str] = []
        for table in ("tasks", "task_runs"):
            rows = await conn.fetch(
                f'SELECT status, count(*) AS n FROM "{table}" GROUP BY status ORDER BY status'
            )
            lines += [f'{table}.status.{row["status"]}={row["n"]}' for row in rows]
    finally:
        await conn.close()
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


asyncio.run(go(int(sys.argv[1]), sys.argv[2]))
PYEOF
  ) >> "$OUT/task-status-histogram-stdout.txt" 2>&1
  hist_rc=$?
  {
    echo "evidence_ps_rc=$ps_rc"
    echo "evidence_worker_log_rc=$worker_rc"
    echo "evidence_app_log_rc=$app_rc"
    echo "evidence_census_after_rc=$census_rc"
    echo "evidence_histogram_rc=$hist_rc"
  } >> "$OUT/receipts.txt"
}
post_up_abort() {
  CLEANED=1                        # 幂等闸：清理一开跑，后来的信号只忽略，不第二次 teardown
  echo "GUARD_ABORT: $1（p6demo 已 up：先取证，再守卫式清理，再证明端口，最后按优先级退）" >&2
  echo "abort_rc_path=post_up_abort" >> "$OUT/receipts.txt"
  collect_evidence
  guarded_down
  down_rc=$?
  [ "$down_rc" -eq 0 ] || echo "清理段 guarded_down 退 $down_rc（继续跑端口证明）" >&2
  echo "down_v_rc=$down_rc project=$PROJECT context=post_up_abort" >> "$OUT/receipts.txt"
  port_check
  port_rc=$?
  echo "port_check_rc=$port_rc context=post_up_abort" >> "$OUT/receipts.txt"
  # 离开码仍按文档面那条优先级：**内层 > 清理 > 端口**。内层已经跑完并且非零时（信号是在
  # 取证/清理阶段到达的），8 不许把内层的真实失败盖掉；内层没结论（up/wait/种子阶段就断了）
  # 才落回 GUARD_ABORT=8。守卫拒绝删除本身也走这条：8 = 「守卫不肯删」。
  if [ "$INNER_RC_KNOWN" = "1" ] && [ "$rc" -ne 0 ]; then
    echo "内层 rc=$rc 优先于本次 abort 的 8（清理与端口证据已留在 receipts）" >&2
    exit "$rc"
  fi
  exit 8
}

# R1（修复轮 1）：INT/TERM 必须走上面那条同一路径。从前脚本里没有 trap，Ctrl-C 会把
# p6demo 留在原地（端口被占、证据没抓、也没有 PORT_STILL_HELD 痕迹），正好违背头注释的承诺。
# st=$? 先于任何命令把被打断命令的退出码存住：handler 自己那条 echo 不许把 $? 洗成 0。
on_signal() {
  local sig=$1 st=$?
  if [ "$CLEANED" = "1" ]; then
    echo "SIG$sig：清理已在进行，重复信号忽略（teardown 只跑一次）" >&2
    return "$st"
  fi
  if [ "$UP_DONE" = "1" ]; then
    post_up_abort "caught SIG$sig：生命周期在 up 之后被打断"
  fi
  echo "GUARD_ABORT: caught SIG$sig —— 尚未 up 成功，按纪律**不做任何删除**（退 8）" >&2
  # 不删是对的，但「端口现在归谁」必须留痕：从前这里直接 exit 8，一次 Ctrl-C 之后
  # receipts.txt 里连一行端口记录都没有，读的人只能重新 up 才知道有没有残留。
  # port_check 只 bind 探测，不动任何容器，所以 pre-up 路径可以安全调它。
  # 下一行不许改写 $?：它是被打断命令的退出码，on_signal 顶部已经存进 st，返回时要用。
  port_check
  echo "port_check_rc=$? context=signal_before_up" >> "$OUT/receipts.txt"
  exit 8
}
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM

# ---- 2. 清旧 + up（全新空卷 ⇒ census 的「零改动」和 Demo 的「已知起点」才是真话） ----
# R2（修复轮 1）：这一处的 guarded_down 从前是 `|| exit $?`，而 guard 内部是 die()：
# 拒绝删除时脚本立刻退 8，一台遗留的 p6demo 就这样挂在端口上、没有任何痕迹。
# 现在：存住拒绝码 → 写收据 → 跑端口证明 → 退 8（清理优先于端口，与文档面一致）。
guarded_down
pre_guard_rc=$?
if [ "$pre_guard_rc" -ne 0 ]; then
  echo "GUARD_ABORT: 起栈前的 guarded_down 退 $pre_guard_rc（守卫拒绝删除 = 绝不继续 up；先证明端口归谁占着）" >&2
  echo "pre_down_guard_rc=$pre_guard_rc project=$PROJECT" >> "$OUT/receipts.txt"
  port_check
  pre_port_rc=$?
  echo "port_check_rc=$pre_port_rc context=pre_up_guard_refused" >> "$OUT/receipts.txt"
  exit 8
fi
if [ -n "$RL" ]; then
  export P6_RL_TASK_PER_MIN="$RL"
fi
docker compose -p "$PROJECT" --env-file "$ENV_FILE" up -d --build "${SERVICES[@]}" \
  || post_up_abort "compose up 失败"
UP_DONE=1
echo "[2/8] $PROJECT 已 up"

wait_healthy() {
  local c=$1 n=$2 s=""
  for ((i = 0; i < n; i++)); do
    s=$(docker inspect --format '{{.State.Health.Status}}' "$c" 2>/dev/null || echo missing)
    [ "$s" = "healthy" ] && { echo "  $c healthy"; return 0; }
    sleep 2
  done
  post_up_abort "$c 在 $((n * 2)) 秒内没到 healthy（最后状态：$s）"
}
wait_healthy "$P6_PG_CONTAINER" 45
wait_healthy "$P6_REDIS_CONTAINER" 30
wait_healthy "$P6_APP_CONTAINER" 60
wait_healthy "$P6_WORKER_CONTAINER" 30
wait_healthy "$P6_MCP_CONTAINER" 30
wait_healthy "$P6_WEB_CONTAINER" 20
docker compose -p "$PROJECT" --env-file "$ENV_FILE" ps --all > "$OUT/ps.txt" 2>&1
echo "[3/8] 六常驻全 healthy"

# ---- 4. 一次性身份：现场 mint 口令 → compose run 种子脚本 ----
# 口令只在**本进程环境**里（`backend/scripts/seed_dev_users.py:52` 的
# `_password_from_env_or_prompt` 先读 env）。名字必须与种子脚本读的键**逐字相同**：
# `:59` 读 SEED_DEV_PASSWORD、`:60` 读 SEED_MEMBER2_PASSWORD —— 名字不同它会去 getpass，
# `compose run` 没有 tty 就卡死在这里。内层命令读的是 P6_* 那对面（消费侧的名字），
# 所以 mint 一次、映射两份。命令行只出现**变量名**：`-e SEED_DEV_PASSWORD` 不带 = 值
# ⇒ compose 从当前环境取值。工件里只允许出现 hash_set / 计数这一类形状，绝不出现口令或 JWT。
P6_DEMO_EMAIL=dev@example.com
P6_DEMO_PASSWORD=$("$PY" -c "import secrets;print(secrets.token_urlsafe(24))")
P6_MEMBER2_PASSWORD=$("$PY" -c "import secrets;print(secrets.token_urlsafe(24))")
SEED_DEV_PASSWORD="$P6_DEMO_PASSWORD"
SEED_MEMBER2_PASSWORD="$P6_MEMBER2_PASSWORD"
export P6_DEMO_EMAIL P6_DEMO_PASSWORD P6_MEMBER2_PASSWORD
export SEED_DEV_PASSWORD SEED_MEMBER2_PASSWORD
docker compose -p "$PROJECT" --env-file "$ENV_FILE" run --rm \
  -e SEED_DEV_PASSWORD -e SEED_MEMBER2_PASSWORD \
  init-data python scripts/seed_dev_users.py > "$OUT/identity-seed.txt" 2>&1 \
  || post_up_abort "身份种子失败（见 $OUT/identity-seed.txt）"
grep -q 'hash_set=True' "$OUT/identity-seed.txt" \
  || post_up_abort "身份种子没报告 dev 口令已设置（登进去会 401，不如在这里红）"
echo "[4/8] 一次性身份就位（dev@example.com / org …0001，口令只在进程环境）"

# ---- 5. demo 栈普查（before）+ 内层环境 ----
(cd backend && "$PY" scripts/census_tables.py --db ai_workspace --db enterprise_data \
  --host-port "$P6_PG_HOST_PORT" --out "$OUT/census-before.txt") >> "$OUT/census-before-stdout.txt" 2>&1 \
  || post_up_abort "demo 栈普查失败"
export P6_ORIGIN="http://127.0.0.1:${P6_WEB_HOST_PORT}"
export P6_API_BASE="${P6_ORIGIN}/api/v1"
export P6_ART_DIR="$OUT"
export P6_DEMO_PG_PORT="$P6_PG_HOST_PORT"
echo "[5/8] 内层环境已导出：P6_API_BASE=$P6_API_BASE"

# ---- 5.5 显式停 worker（P6_STOP_WORKER=1 时）：202 容量档与限流边界档的前提 ----
# 这两档要的是「入队但不消费」：worker 全程停着 ⇒ 零 chat/completions、任务停在 queued。
# 必须停在**参数化容器名** $P6_WORKER_CONTAINER 上（硬编码 p6-worker 在 demo 栈上是错的名字）。
# 位置刻意在 before-census 成功之后、任何内层 traffic 之前：普查基线要包含 healthy worker 起栈后的
# 已知起点，而停 worker 必须早于第一条压测请求，否则前几秒会被真消费污染 queued 判据。
# docker stop 的输出与 worker_state 行都追加进 receipts.txt（内层命令读到的是「非空但缺三键」
# 的收据，这正是 loadtest.classify_receipts 设计要放行的运行中状态，绝不因它中断产出）。
if [ "${P6_STOP_WORKER:-0}" = "1" ]; then
  docker stop "$P6_WORKER_CONTAINER" >> "$OUT/receipts.txt" 2>&1 \
    || post_up_abort "停 worker 失败（这一档的前提就是 worker 不消费）"
  echo "worker_state=stopped_by_request container=$P6_WORKER_CONTAINER" >> "$OUT/receipts.txt"
fi

# ---- 6. 跑内层命令（rc 先存住：清理与取证必须跑完） ----
"$@"
rc=$?
# 内层已经有结论了 ⇒ post_up_abort 的「内层 > 清理 > 端口」优先级此刻才可执行。
# 不置这个闸，信号若在取证/清理段到达，8 会把内层真实的非零码盖掉（修复轮 2 的 Medium 项）。
INNER_RC_KNOWN=1
echo "$rc" > "$OUT/inner-rc.txt"
echo "[6/8] 内层命令退出码 = $rc"

# ---- 7. 拆栈前取证 ----
collect_evidence
probe_rc=0
( cd backend && "$PY" -c "
import httpx, os, sys
r = httpx.get(os.environ['P6_API_BASE'] + '/auth/login', timeout=10)
print('login_endpoint_probe', r.status_code)
" ) >> "$OUT/app.log" 2>&1 || probe_rc=$?
echo "evidence_login_probe_rc=$probe_rc" >> "$OUT/receipts.txt"
echo "[7/8] 证据已抓（worker.log / app.log / census-after.txt / task-status-histogram.txt / ps.txt；逐条 rc 见 receipts.txt）"

# ---- 8. 守卫式拆栈 + 收据 + 默认项目零改动证明 + 端口释放 ----
collect_receipts() {
  {
    echo "== $PROJECT lifecycle receipts =="
    echo "project=$PROJECT env_file=$ENV_FILE web_port=$P6_WEB_HOST_PORT pg_port=$P6_PG_HOST_PORT"
    echo "rl_task_per_min=${RL:-default-20}"
    echo "--- pre-up: demo 端口当时空闲（见 PORTS_FREE_BEFORE_UP）---"
    echo "--- post-down: 端口释放证明 ---"
  } >> "$OUT/receipts.txt"
}
collect_receipts
# R1：拆栈一开跑就把幂等闸落下 —— 此后的信号只忽略，不会第二次 teardown。
CLEANED=1
guarded_down
gd_rc=$?
[ "$gd_rc" -eq 0 ] || echo "GUARD_ABORT 面：清理段 guarded_down 退 $gd_rc（继续端口证明，最后按优先级离开）" >&2
echo "down_v_rc=$gd_rc project=$PROJECT" >> "$OUT/receipts.txt"
port_check
irc=$?
echo "port_check_rc=$irc" >> "$OUT/receipts.txt"
"$PY" backend/scripts/census_tables.py --db ai_workspace --db enterprise_data --host-port 5432 \
  --out "$OUT/home-census-after.txt" > "$OUT/home-census-after-stdout.txt" 2>&1
home_after_rc=$?
echo "home_census_after_rc=$home_after_rc" >> "$OUT/receipts.txt"
diff -u "$OUT/home-census-before.txt" "$OUT/home-census-after.txt" > "$OUT/home-census-diff.txt"
diff_rc=$?
echo "home_census_diff_rc=$diff_rc (0 = 默认项目逐表行数零改动)" >> "$OUT/receipts.txt"
echo "[8/8] $PROJECT 已 down -v；默认项目 diff 退出码 = $diff_rc"

final=$rc
[ "$final" -eq 0 ] && final=$gd_rc
[ "$final" -eq 0 ] && final=$irc
[ "$final" -eq 0 ] && final=$diff_rc
exit "$final"
