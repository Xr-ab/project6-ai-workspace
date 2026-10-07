"""「绝不碰默认项目」这条纪律的静态针。

为什么是静态扫文本而不是跑命令：被禁的动作（`down -v` 打到默认项目）一旦真跑就不可逆，
测试不能拿它做正反对照。所以扫「删除动作可达的参数面里有没有默认项目的名字」。
形状抄 tests/unit/test_secret_scan.py:16-18 的路径推导。
"""
import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ROOT = BACKEND_ROOT.parent
COMPOSE = ROOT / "docker-compose.yml"

# 默认项目的卷真名（compose 里 :- 的默认值）。demo 栈的删除面里出现任何一个 = 针红。
DEFAULT_VOLUME_NAMES = (
    "project6-ai-workspace_p6_pgdata",
    "project6-ai-workspace_p6_redisdata",
    "project6-ai-workspace_p6_modelcache",
    "project6-ai-workspace_p6_uploads",
)


# 顶层服务键：恰好两空格缩进 + 名字 + 冒号 + 行尾（注释行以 # 开头，被排除）。
SERVICE_KEY_RE = re.compile(r"^ {2}(?P<name>[^ #][^:]*):[ \t]*$")


def _compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def _service_block(name: str) -> str:
    """切出 docker-compose.yml 里 `services:` 下某个顶层服务块的原文。

    为什么要自己切：全文 count 只证明「这个键出现了几次」，证明不了它**住在哪个服务里**——
    把整行从 app: 挪到 worker: 底下，次数还是一次，全文扫的针全绿，压测却改不动档位。
    归属只由「恰好两空格缩进的服务键」和「第 0 列的顶层键」改变；空行与缩进注释不改归属。
    """
    inside_services = False
    collecting = False
    collected: list[str] = []
    for line in _compose_text().splitlines():
        if not line.strip():
            continue
        if not line.startswith(" "):  # 顶层键：services: / volumes: / networks: …
            inside_services = line.strip() == "services:"
            collecting = False
            continue
        if line.lstrip().startswith("#"):  # 注释跟着它上面的块走
            if collecting:
                collected.append(line)
            continue
        match = SERVICE_KEY_RE.match(line.rstrip())
        if inside_services and match is not None:
            collecting = match.group("name") == name
            continue
        if collecting:
            collected.append(line)
    return "\n".join(collected)


def test_app_service_has_rate_limit_knob_with_current_default():
    """限流旋钮必须在，且默认值 = 现值 20（不带 env 时行为零变化）。"""
    assert "RATE_LIMIT_TASK_PER_MIN: ${P6_RL_TASK_PER_MIN:-20}" in _compose_text()


def test_rate_limit_knob_is_not_added_to_worker():
    """worker 不做限流：旋钮只在 app 服务块里出现，且全文件恰好一次。

    钉住的失效模式：旋钮被挪进 worker = 压测档位改不动，读数会假（app 侧才是限流发生地）。
    挪动不改全文出现次数，所以这里按服务块切片分别断言在/不在；count == 1 的原文面保留，
    两层叠起来才是「一次、且只在对的服务里」。
    """
    text = _compose_text()
    knob = "RATE_LIMIT_TASK_PER_MIN"
    assert text.count(knob) == 1
    app_block = _service_block("app")
    worker_block = _service_block("worker")
    # 两块都必须切得到东西：服务改名/整块消失时不许让下面的 in/not in 变成空话。
    assert app_block and worker_block
    assert knob in app_block
    assert knob not in worker_block


def test_acceptance_env_pins_demo_stack_off_default_ports():
    """acceptance.env 是 demo 栈的复用参数面：端口必须与默认项目互斥。

    禁的一面按默认项目**实际发布的四个口**列全（compose:251 的 8100、:285 的 8080 也算）：
    只禁 5432/6379 的话，参数面里冒出一句「本机 mcp 用 8100」这种引用不会被抓到，
    而它正是「把默认项目的口当现值抄进 demo 面」的起点。
    """
    text = (ROOT / "docker" / "acceptance.env").read_text(encoding="utf-8")
    for port in ("5543", "5637", "8110", "8081"):
        assert port in text
    for port in ("5432", "6379", "8100", "8080"):
        assert port not in text


def test_compose_default_volume_names_are_documented_for_the_guard():
    """守卫自己的针：默认项目卷名清单不许被悄悄清空（清空 = 所有删除面扫描恒绿）。"""
    assert len(DEFAULT_VOLUME_NAMES) == 4
    for name in DEFAULT_VOLUME_NAMES:
        # 这四个真名必须真在 compose 里（说明清单没跟现实脱节）
        assert name in _compose_text()


def test_embedding_services_disable_the_xet_backend():
    """下 embedding 模型的三个服务，每个都要同时有 HF_ENDPOINT 与 HF_HUB_DISABLE_XET=1。

    一手证据（`backend/scratch/_p11c_t7_demo1_retry1/`）：hf-mirror 上 XET 那条腿是断的。
    worker 下模型到 4/5 个文件撞 `401 Unauthorized, domain:
    https://cas-server.xethub.hf.co/v2/reconstructions/...` ⇒ 文档落 status=failed
    ⇒ Demo 1 的「索引到 ready」在**任何一次补全之前**判红（app.log 与 worker.log 的
    `chat/completions` 都是 0）。关掉 XET 走镜像的经典 `/resolve/main/` 通道，
    同一份模型一次下全（`backend/scratch/_p11c_t7_xet_check/out-disable-xet.txt`：
    `DOWNLOAD_OK files=9 bytes=95333749`）。

    为什么按服务块而不是全文计数：删掉某个服务的这一行、或把它挪到一个没有 HF_ENDPOINT
    的服务里，全文次数都不变（形状抄 `test_rate_limit_knob_is_not_added_to_worker`）。
    """
    for name in ("init-data", "app", "worker"):
        block = _service_block(name)
        assert block, f"{name} 服务块切不出来：这条针会空转"
        assert "HF_ENDPOINT: https://hf-mirror.com" in block, f"{name} 没有镜像端点"
        assert "HF_HUB_DISABLE_XET" in block, f"{name} 会走 XET 后端：镜像上必 401"


def _script_text() -> str:
    return (ROOT / "docker" / "run_on_demo_stack.sh").read_text(encoding="utf-8")


def _code_lines(text: str) -> list[str]:
    """去掉空行与注释行，只留下会被 bash 真执行的代码。

    为什么要这个：脚本里有多条**说明性**的行提到 `down -v`（头注释、echo 文案），
    把它们也算进「删除动作可达参数面」会让针恒红，进而诱导读代码的人把针删掉。
    """
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith("echo"):
            continue
        out.append(s)
    return out


# 「down 掉卷」这条删除动作的两种等价写法：`down -v` 与 `down --volumes`。
# 字面串匹配只挡住作者想到的那一种；长写法同样会把家里的卷删掉，所以这里按 token 认。
DOWN_WORD_RE = re.compile(r"(?:^|[\s;&|`(])down(?:$|[\s;&|`)])")
VOLUME_OPT_RE = re.compile(r"(?:^|\s)(?:-v|--volumes)(?:\s|$)")


def _mentions_volume_delete(line: str) -> bool:
    """这一行是不是一条删卷调用：`down` 作独立词 + `-v`/`--volumes` 作独立选项 token。"""
    return DOWN_WORD_RE.search(line) is not None and VOLUME_OPT_RE.search(line) is not None


def _invocation_indices(text: str, name: str) -> list[int]:
    """函数**被调用**的行下标（按 _code_lines 序），定义行 `name() {` 用前瞻排除掉。

    为什么按调用而不是按 `in text`：「取证在拆栈之前」这类顺序纪律，只有拿调用点
    的行号比大小才钉得住；把定义行也算进来会先撞到自己（定义总在调用之前，恒真）。
    """
    pat = re.compile(rf"{name}(?![A-Za-z0-9_(])")
    return [i for i, ln in enumerate(_code_lines(text)) if pat.match(ln)]


def test_demo_stack_project_is_not_the_default_project():
    """入口脚本操作的项目名必须是 p6demo（不是默认项目名，也不是 p6accept 以免和 11a 的工件混）。"""
    text = _script_text()
    assert "PROJECT=p6demo" in text
    assert "docker compose -p p6demo" not in text  # 一律走 -p "$PROJECT"，不留硬编码的第二条路


def test_down_v_always_carries_the_project_flag():
    """最危险的一条：任何**被执行**的删卷调用都必须带 -p "$PROJECT"。

    裸 `down -v` 会吃 cwd + 默认项目名 ⇒ 把家里 Phase 1-10 的数据卷删掉，不可逆。

    修复轮 1 加的两层（评审给的变异：这两种写法从前全绿）：
    ① 认 token 不认字面串 —— `down --volumes` 与 `down -v` 是同一件事，只匹配 `down -v`
       等于把「换一种写法」的口子留着；
    ② 链式一行也要扫 —— `_code_lines()` 跳过 echo 行，是为了放过纯说明性的 echo 文案，
       但 `echo seed; docker compose down -v` 把真删除挂在 echo 后面就躲过了跳过。
       所以对**原始行**（含 echo 行，只放过整行注释）额外要求：带 `;`/`&&` 的删卷调用必须带 -p。
    """
    text = _script_text()
    deletes = [ln for ln in _code_lines(text) if _mentions_volume_delete(ln)]
    assert deletes, "脚本里应该至少有一条真执行的删卷调用"
    for ln in deletes:
        assert '-p "$PROJECT"' in ln, f"删卷调用少了项目限定：{ln}"
        assert "remove-orphans" not in ln
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue                                  # 只放过整行注释，不再放过 echo 前缀
        if _mentions_volume_delete(s) and (";" in s or "&&" in s):
            assert '-p "$PROJECT"' in s, f"链式行里的删卷调用少了项目限定：{raw}"


def test_no_delete_subcommand_names_the_default_project():
    """所有删除类子命令（down/rm/stop）的可达参数面里不许出现默认项目的名字。"""
    for ln in _code_lines(_script_text()):
        if any(word in ln for word in ("down", "docker rm", "docker stop", "volume rm")):
            for name in DEFAULT_VOLUME_NAMES:
                assert name not in ln, f"删除动作的可达参数面出现默认项目卷名：{ln}"


def test_volume_guard_checks_the_resolved_label_not_the_key():
    """卷面守卫核验的是解析后的卷名 + project 标签（11a 的教训：只看键名会假红）。

    修复轮 1 加的两层（评审给的变异：这两种改法从前全绿）：
    ③ 循环必须用**间接展开** `${!v}` 拿卷名 —— 写成 `name=$v` 拿到的是键名
       （`P6_PGDATA` 这种字面串），inspect 必然失败 → `continue` → 守卫变成一条不核验任何
       东西的空转，而针只验「标签在文本里出现」照样绿。这正是 11a 真踩过的同一个坑。
    ④ 核验必须**在删除之前** —— 把标签检查整段挪到 `down -v` 后面，文本面一切如旧，
       但删除已经发生，守卫只剩事后追认。所以按行下标比顺序。
    """
    text = _script_text()
    assert "com.docker.compose.project" in text
    assert "拒绝删除" in text
    # ③ guarded_down 函数体内取间接展开，且不出现「直接拿键名当卷名」的写法
    start = text.index("guarded_down() {")
    body = text[start:text.index("\n}", start) + 2]
    assert "${!" in body, "guarded_down 没做间接展开：核验的是键名而不是卷真名"
    assert re.search(r"\$\{!\s*[A-Za-z_][A-Za-z0-9_]*\s*\}", body), f"间接展开形状不对：{body[:80]}"
    assert re.search(r"name=\$v\b", body) is None, "卷名取了键名（name=$v）：守卫会空转"
    # ④ 标签核验（inspect + 标签键）都排在删卷调用之前
    lines = _code_lines(text)
    label_idx = next(i for i, ln in enumerate(lines) if "com.docker.compose.project" in ln)
    inspect_idx = next(i for i, ln in enumerate(lines) if "docker volume inspect" in ln)
    delete_idx = min(i for i, ln in enumerate(lines) if _mentions_volume_delete(ln))
    assert inspect_idx < delete_idx, "卷标签 inspect 被挪到了删卷之后：删除先发生，守卫成摆设"
    assert label_idx < delete_idx, "project 标签核验被挪到了删卷之后：删除先发生，守卫成摆设"
    # ④′（修复轮 2 补）只挪**比对**的变异：inspect 行与标签键行留在原地，把 `if [ "$label" !=
    # "$PROJECT" ]` 整段搬到 `down -v` 后面 —— 上面两条针照旧绿，可删除已经发生，守卫只剩事后追认。
    # 所以比对行自己按**行下标**钉：必须唯一、必须晚于 inspect（早于它就没有标签可比）、必须早于删除。
    compare_idx = [i for i, ln in enumerate(lines)
                   if "$label" in ln and "!=" in ln and "$PROJECT" in ln]
    assert len(compare_idx) == 1, f"标签比对行应只有一处，实得 {len(compare_idx)} 处：搬走后没人发现"
    assert compare_idx[0] > inspect_idx, "标签比对排在 inspect 之前：比对时标签还没取到"
    assert compare_idx[0] < delete_idx, "标签比对被挪到了删卷之后：删除先发生，守卫只剩事后追认"


def test_ports_are_proved_released_after_teardown():
    """收尾的顺序纪律，按**调用点**的行下标钉：取证 → 拆栈 → 端口证明，一个都不许换位。

    修复轮 1 加的两层：
    ⑤ 取证在拆栈之前（评审给的变异：把第 8 段整段挪到第 7 段前面从前全绿）——
       `down -v` 之后日志与普查就没了，post_up_abort 那条路径同理。
    ⑥ 端口证明在清理退出码之后（原文那条），外加 INT/TERM 也必须有 trap：
       没 trap 时 Ctrl-C 会把 p6demo 留在原地占着端口、证据没抓、也没有 PORT_STILL_HELD 痕迹，
       正好违背脚本头注释的承诺；信号路径必须走 post_up_abort 这一条同一路径。
    ⑦（修复轮 2，评审 Medium）post_up_abort 里「内层 rc 优先于 8」那条分支从前**不可达**：
       INNER_RC_KNOWN 只在开头置 0、脚本从没置 1 ⇒ 信号落在取证/清理段时 8 把内层真实的非零码
       整个盖掉。所以钉「置 1 真实存在、行号唯一、且排在 `rc=$?` 捕获与末次取证之间」。
    ⑧（修复轮 2，评审 Low 两条）pre-up 的信号路径从前只 exit 8 不留痕：Ctrl-C 之后 receipts.txt
       里连一行端口记录都没有 ⇒ 钉 handler 体内必须调 port_check（它只 bind 探测，不碰容器，
       「绝不删除」与「留下痕迹」两头都能要）；receipts.txt 的重开必须排在「-- 后面必须给内层
       命令」这道校验**之后** —— 一次敲错参数的运行不许把上一次的收据抹平。
    """
    text = _script_text()
    assert "PORT_RELEASED" in text
    assert "PORT_STILL_HELD" in text
    tail = text[text.index("gd_rc=$?"):]
    assert "port_check" in tail
    # ⑤ 最后一次 collect_evidence 调用要早于最后一次 guarded_down 调用
    evidence = _invocation_indices(text, "collect_evidence")
    teardown = _invocation_indices(text, "guarded_down")
    assert evidence and teardown, "取证或清理函数没被调用：顺序针成了空话"
    assert max(evidence) < max(teardown), "拆栈排在了取证之前：down -v 之后日志与普查就没了"
    # ⑥ 信号面：INT 与 TERM 都要 trap，处理函数要按 UP_DONE 分流到 post_up_abort，且幂等
    trap_lines = [ln for ln in _code_lines(text) if ln.startswith("trap ")]
    assert trap_lines, "没有 INT/TERM trap：Ctrl-C 会把一次性栈留在原地"
    joined = " ".join(trap_lines)
    assert "INT" in joined and "TERM" in joined, f"trap 没同时盖住 INT 与 TERM：{joined}"
    hstart = text.index("on_signal()")
    handler = text[hstart:text.index("\n}", hstart) + 2]
    assert "post_up_abort" in handler, "信号路径绕过了 post_up_abort：清理与取证会整个跳过"
    assert "UP_DONE" in handler, "trap 没按 UP_DONE 分流：up 之前不许尝试任何删除"
    assert "CLEANED" in handler and "CLEANED=1" in text, "缺幂等闸：信号会跑第二次 teardown"
    # ⑦（修复轮 2，评审 Medium）优先级分支不许是死码：post_up_abort 靠 INNER_RC_KNOWN 判断
    #    「内层已经有结论」，而脚本从前只在开头置 0、从没置 1 —— 信号落在取证/清理段时，
    #    8 会把内层真实的非零码整个盖掉。针的是赋值**真实存在且排在正确位置**（缺了它这条分支不可达）。
    lines = _code_lines(text)
    known = [i for i, ln in enumerate(lines) if ln == "INNER_RC_KNOWN=1"]
    assert len(known) == 1, "内层 rc 的闸从没置 1：post_up_abort 的「内层 > 清理 > 端口」是死分支"
    inner_rc = [i for i, ln in enumerate(lines) if ln == "rc=$?"]
    assert len(inner_rc) == 1, f"内层 rc 捕获行不唯一（{len(inner_rc)} 处）：闸的顺序无从判定"
    assert inner_rc[0] < known[0], "闸排在内层 rc 捕获之前：还没跑内层就声明「有结论」，优先级会拿旧值"
    assert known[0] < max(evidence), "闸排在建栈期取证之后：拆栈前那段取证仍在跑时信号仍会盖掉内层码"
    # ⑧（修复轮 2，评审 Low）up 之前那条信号路径不许「只退不证」：port_check 是只读探测
    #    （bind 一下就放行，不碰任何容器），所以「绝不删除」和「留下端口痕迹」两头都能要。
    #    必须按**代码行**找：handler 的原文切片里含我写的那条解释注释，光 `in handler` 会
    #    被注释喂饱 —— 把调用整行删掉针照旧绿（变异 M5 实测到这一点）。
    # 必须把范围**封顶在 handler 内**：只写下界时，起栈前守卫拒绝那条 `port_check`（:284）和
    # 收尾那条（:384）也会被算进来，摘掉 handler 里唯一那一行仍 `10 passed`（复审计 Minor 1 实测）。
    sig_start = lines.index("on_signal() {")
    sig_end = next(i for i in range(sig_start + 1, len(lines)) if lines[i] == "}")
    sig_calls = [i for i in range(sig_start + 1, sig_end) if lines[i].startswith("port_check")]
    assert sig_calls, "pre-up 信号路径没跑端口证明：Ctrl-C 之后 receipts 里零痕迹"
    assert "context=signal_before_up" in text, "信号路径的端口结果没写进 receipts：探测跑了但没人看得见"
    # ⑨（修复轮 2，评审 Low）receipts.txt 的重开必须排在「-- 后面必须给内层命令」这道校验**之后**：
    #    写在前头时，一次敲错参数的 die 就已经把上一次运行的收据抹平了 —— 一条没跑起来的命令
    #    不许有销毁证据的能力。
    trunc = [i for i, ln in enumerate(lines) if ln.startswith(": > ") and "receipts.txt" in ln]
    assert len(trunc) == 1, f"收据重开行不唯一（{len(trunc)} 处）：顺序针无法判定"
    argcheck = [i for i, ln in enumerate(lines) if ln.startswith("[ $# -gt 0 ]")]
    assert len(argcheck) == 1, "缺「必须给内层命令」这道校验：顺序针成了空话"
    assert argcheck[0] < trunc[0], "收据重开排在参数校验之前：一次敲错参数就抹掉上一次运行的收据"


def test_credentials_never_appear_on_the_command_line():
    """三条独立的形状要求，缺一条就有一个静默失败面：
    ① compose run 只带变量名（带 = 值 = 口令上命令行，会进 ps 与 shell 历史）；
    ② 变量名必须与 seed_dev_users.py 读的键一致，否则它去 getpass，无 tty 卡死；
    ③ 值必须是现场 mint，不许是写进 git 的字面量。"""
    text = _script_text()
    assert "-e SEED_DEV_PASSWORD -e SEED_MEMBER2_PASSWORD" in text
    assert 'SEED_DEV_PASSWORD="$P6_DEMO_PASSWORD"' in text
    assert 'SEED_MEMBER2_PASSWORD="$P6_MEMBER2_PASSWORD"' in text
    assert 'P6_DEMO_PASSWORD=$(' in text and 'P6_MEMBER2_PASSWORD=$(' in text
    # ③' 修复轮 1（评审变异：把 mint 换成 `$(cat somefile)` 从前全绿 —— 只查 `$(` 形状
    # 会让「从文件里读一个口令」冒充现场 mint）：mint 表达式本体必须是 secrets.token_urlsafe。
    for name in ("P6_DEMO_PASSWORD", "P6_MEMBER2_PASSWORD"):
        mints = [ln for ln in text.splitlines() if ln.strip().startswith(f"{name}=$(")]
        assert len(mints) == 1, f"{name} 的赋值行应当恰好一条，实得 {len(mints)}"
        assert "secrets.token_urlsafe" in mints[0], f"{name} 不是现场 mint 的随机口令：{mints[0]}"
    assert text.count("secrets.token_urlsafe") >= 2, "两把口令各自都要一次独立 mint（不许共用一个值）"
    for name in ("P6_DEMO_PASSWORD", "P6_MEMBER2_PASSWORD",
                 "SEED_DEV_PASSWORD", "SEED_MEMBER2_PASSWORD"):
        assert re.search(rf'{name}=["\']?[A-Za-z0-9]', text) is None, \
            f"{name} 出现了字面量值（凭据不许进 git 文件）"
    # 三条凭据面禁令：不 tee 渲染配置、不 inspect 容器环境、不读 backend/.env
    for forbidden in ("config |", ".Config.Env", "backend/.env"):
        assert forbidden not in text, f"脚本里不该出现凭据面动作：{forbidden}"


def test_worker_can_be_stopped_by_request_for_capacity_runs():
    """202 容量档的前提是「入队但不消费」：脚本必须有一条显式停 worker 的路，
    并且停的是**参数化容器名**（不是硬编码 p6-worker，那在 demo 栈上是错的）。"""
    text = _script_text()
    assert 'P6_STOP_WORKER' in text
    assert 'docker stop "$P6_WORKER_CONTAINER"' in text
    assert "docker stop p6-worker" not in text
