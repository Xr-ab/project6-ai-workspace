"""门禁自身的行为面（spec §10 三条设计约束的机器化；本文件是 §5.1 清单的**裁定式追加**，
追加理由与后果写在计划 Task 6「与 spec §5.1 清单的偏离」一节，docs/02 §11 成文）。

为什么门禁需要自己的测试：§10 的红绿成对要求「注入假密钥跑红」，而一次性工件守不住未来
——改了模式让 P5 不再命中，只有这条针会红。同理，「只打印 file:line:id」这条约束
是靠不住人记忆的，所以拿探针值当反向断言。

假密钥必须拼接构造：本文件是**被扫对象**（跟踪面）。真写出邻接的
JWT_SECRET=<12+ 连续字面量> 会让下一次 CI 因门禁自己的测试夹具而红。
"""
import re
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND_ROOT / "scripts" / "secret_scan.py"
WORKFLOW = BACKEND_ROOT.parent / ".github" / "workflows" / "ci.yml"

# 拼接：源码文本里永不出现在 JWT_SECRET= 之后紧跟 12+ 个字面字符的形状
JWT_PREFIX = "JWT_" "SECRET"
FAKE_KEY = "abcdefghij" + "0123456789"          # 20 位，含字母与数字，不带 - _ .
FAKE_QUOTED = "AbCdEf" + "GhIjKlMnOpQr"
FAKE_SHORT = "Ab1C" + "d2Ef3"                   # 9 位：过 P4 的 {8,}、不到 P3 的 {12,}


def run_scan(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(BACKEND_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    return proc.returncode, proc.stdout, proc.stderr


def write_probe(tmp_path: Path, body: str) -> Path:
    probe = tmp_path / "probe.txt"
    probe.write_text(body, encoding="utf-8")
    return probe


# 汇总行的**形状**（secret_scan.py 末尾那一行）：四列、整数、整行匹配。
# 按形状定位而不是「第一条以 SCANNED 开头的行」：将来脚本若长出第二行 SCANNED…
# （per-directory tally / 进度回显），取首行会静悄悄判错对象（终评复审计 Minor-2）。
SUMMARY_RE = re.compile(r"SCANNED (\d+) READ (\d+) SKIPPED (\d+) HITS (\d+)")


def summary_of(out: str) -> tuple[int, int, int, int]:
    """取汇总行的四个整数 `(SCANNED, READ, SKIPPED, HITS)`，并**要求这样的行只有一条**。

    两种歧义都判红，不给静悄悄读错对象的机会（终评复审计 Minor-2 的加固形状）：
    没有形状匹配的行 ⇒ `AssertionError`；出现两条以上（将来若长出 per-directory
    tally，它同样能 fullmatch 这个四列形状）⇒ 同样 `AssertionError`。
    「红而不是绿」是这里唯一可接受的失败方向。
    """
    matches = [m for m in (SUMMARY_RE.fullmatch(line) for line in out.splitlines()) if m]
    if not matches:
        raise AssertionError(f"输出里没有 `{SUMMARY_RE.pattern}` 形状的汇总行：\n{out}")
    if len(matches) > 1:
        raise AssertionError(f"汇总行出现 {len(matches)} 条，判据无法确定该判哪一条：\n{out}")
    return (int(matches[0].group(1)), int(matches[0].group(2)),
            int(matches[0].group(3)), int(matches[0].group(4)))


def hits_of(out: str) -> int:
    """从汇总行 `SCANNED n READ m SKIPPED k HITS h` 里取 **h 这一个整数**（终评 R-T6-5 的加固）。

    原来写的是 `"HITS 2" in out` 这种子串判据：`HITS 20`、`HITS 21` 都含 `HITS 2`，
    也就是说命中数从 2 涨到 20 也不会红——那是可松弛的断言。取整值比较把松弛面关掉。
    """
    return summary_of(out)[3]


def test_tracked_face_is_clean():
    """判据 5 的绿侧常驻版：整个跟踪面 0 命中，否则 CI 红在这里。

    面宽对齐（11b 审计的加固）：原来三行在**空面**上也绿 —— `git ls-files` 为空且
    成功时门禁退 0，"HITS 0" 就成了「一个文件都没读」的自证。所以这里用与门禁 ROOT
    同源的一条命令独立数一遍面宽，与它自报的 SCANNED 对齐，并要求面非空。

    cwd 必须是项目目录而不是 BACKEND_ROOT：`git ls-files` 以当前目录为隐式 pathspec
    （本机实测 backend/ 下 155、项目目录下 263），而 ROOT 是脚本的 parents[2] =
    项目目录。拿 155 去比对 263 会把这条针做成假红。
    """
    project_root = BACKEND_ROOT.parent
    face = subprocess.run(
        ["git", "ls-files", "-z"], cwd=str(project_root), capture_output=True,
    )
    assert face.returncode == 0, "git ls-files 非零：比对基准本身不可信"
    expected = len([n for n in face.stdout.split(b"\0") if n])
    assert expected > 0, f"{project_root} 的跟踪面为空：这条针的前提（有面可扫）不成立"

    rc, out, err = run_scan()
    assert rc == 0, f"跟踪面有命中：\n{out}\n{err}"
    assert f"SCANNED {expected} " in out, f"面宽与门禁自报不一致（应为 {expected}）：\n{out}"
    assert "SKIPPED 0" in out, out
    assert hits_of(out) == 0, out


def test_p5_bare_jwt_secret_assignment_hits(tmp_path):
    probe = write_probe(tmp_path, f"prefix {JWT_PREFIX}={FAKE_KEY} suffix\n")
    rc, out, _ = run_scan(str(probe))
    assert rc == 1
    assert ":P5" in out


def test_p1_openai_style_key_hits(tmp_path):
    probe = write_probe(tmp_path, "LLM_KEY = 'sk-" + "ABCDEFGHIJKLMNOP'\n")
    rc, out, _ = run_scan(str(probe))
    assert rc == 1
    assert ":P1" in out


def test_p2_high_entropy_dsn_password_hits_but_interpolation_does_not(tmp_path):
    bad = f"DATABASE_URL=postgresql+asyncpg://svc:{FAKE_QUOTED}@db.internal:5432/app"
    good_interpolated = "url: postgresql://app:${P6_PG_PASSWORD}@db:5432/app"
    good_known_dev = "DATABASE_URL=postgresql+asyncpg://app:app_dev_pwd@localhost:5432/ai_workspace"
    # 垫一行良性头，让高熵那行落在**第 2 行**：断言里的 ":2:" 守的就是「命中归到正确的行」。
    # （计划原文把 bad 写在夹具第 1 行却断言 ":2:"，实测输出是 :1: —— 扫描器的行号从 1 起算，
    #   按「文档与代码不一致时补实现」把夹具对齐断言，而不是把断言改成 :1:。）
    probe = write_probe(tmp_path, f"# benign header\n{bad}\n{good_interpolated}\n{good_known_dev}\n")
    rc, out, _ = run_scan(str(probe))
    assert rc == 1
    p2_lines = [line for line in out.splitlines() if ":P2" in line]
    assert len(p2_lines) == 1, f"P2 应只命中高熵那一行，实得 {len(p2_lines)}：{out}"
    assert p2_lines[0].startswith(str(probe)) and ":2:" in p2_lines[0]


def test_type_annotation_and_lazy_generated_secret_are_not_secrets(tmp_path):
    """config.py 的两种**正确**形状不许染红：类型标注空默认值、运行期生成。

    这两条是 P4/P5 分家的原因本身。写成一条针而不拆成两条，是因为它们必须同时成立
    才说明「合并在一条模式里」是错的——分开写会让后到的人只改一条。
    """
    body = (
        "    jwt_secret: str = \"\"\n"
        f"    settings.{JWT_PREFIX.lower()} = secrets.token_urlsafe(32)\n"
        "self._model = settings.jwt_secret\n"
    )
    probe = write_probe(tmp_path, body)
    rc, out, _ = run_scan(str(probe))
    assert rc == 0, f"不该命中：\n{out}"


def test_placeholder_shapes_never_hit(tmp_path):
    """spec §10 约束 2 的点名单：${}/<>/change-me/REPLACE_/test-only-/app_dev_pwd/空值。"""
    body = (
        f"{JWT_PREFIX}=${{JWT_SECRET}}\n"
        f"{JWT_PREFIX}=<从 .env 里读>\n"
        f"{JWT_PREFIX}=change-me-please-now\n"
        f"{JWT_PREFIX}=REPLACE_AT_DEPLOY\n"
        f"{JWT_PREFIX}=test-only-not-a-real-secret\n"
        f"{JWT_PREFIX}=app_dev_pwd\n"
        f"{JWT_PREFIX}=\n"
        "API_KEY = 'p6_access_token'\n"          # 值就是字段名：键名，不是凭据
    )
    probe = write_probe(tmp_path, body)
    rc, out, _ = run_scan(str(probe))
    assert rc == 0, f"占位符形状被误判成密钥：\n{out}"


def test_output_never_contains_the_matched_value(tmp_path):
    """spec §10 约束 1：门禁自己不能成为泄密面。反向断言 —— 命中了也不许把值打出来。"""
    probe = write_probe(tmp_path, f"{JWT_PREFIX}={FAKE_KEY}\n")
    rc, out, err = run_scan(str(probe))
    assert rc == 1
    assert FAKE_KEY not in out and FAKE_KEY not in err
    assert "sk-" not in out.replace("sk-" + "ABCDEFGHIJKLMNOP", "")  # 只允许命中位置的形式


def test_artifact_name_fails_even_with_benign_content(tmp_path):
    """spec §10 约束 3：11a 事故根因的结构收口。名字本身即证据，内容无关。"""
    artifact = tmp_path / "compose-config-rendered.json"
    artifact.write_text("{}\n", encoding="utf-8")
    rc, out, _ = run_scan("--artifacts-only", str(artifact))
    assert rc == 1
    assert "ARTIFACT(" in out


def test_workflow_face_forbids_secrets_injection_and_render_redirect():
    """spec §7 末段「三样东西不得出现」从文档承诺升成机器针（不解析 YAML ——
    PyYAML 只在 requirements.txt 的传递依赖里，没显式声明，CI 不保证有）。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text, "CI 不许注入任何真密钥面"
    assert "config >" not in text and "config 1>" not in text, "渲染输出不许落盘"
    assert "LLM_API_KEY" not in text, "workflow 里不许出现 LLM 密钥的名字"
    assert "--omit=dev" not in text, "npm audit 必须全量（判据 4）"
    for job in ("backend:", "frontend:", "compose:"):
        assert job in text, f"缺 job {job}"
    assert "pgvector/pgvector:pg16" in text and "redis:7-alpine" in text
    assert 'JWT_SECRET: test-only-not-a-real-secret' in text


# ---------------------------------------------------------------------------
# 修复轮 1（t6-review-findings.md：C1 / I1 / I2 / I3 + 四条 Minor + R-T6-2）。
# 每针的假密钥都继续走拼接构造——本文件自身在被扫面上。
# ---------------------------------------------------------------------------

# --- I2：P3 / P4 各补正向命中针（此前删掉 PATTERNS 任一条 9/9 照样绿） ---

def test_p3_quoted_api_key_assignment_hits(tmp_path):
    probe = write_probe(tmp_path, f"API_KEY = \"{FAKE_KEY}\"\n")
    rc, out, _ = run_scan(str(probe))
    assert rc == 1
    assert ":P3" in out


def test_p4_quoted_jwt_secret_hits_below_p3_threshold(tmp_path):
    """值 9 位：过 P4 的 {8,}、不到 P3 的 {12,} —— 命中的必须只有 P4，
    否则这条针删掉 P4 也不会红（P3 会替它挨打）。"""
    probe = write_probe(tmp_path, f"{JWT_PREFIX} = \"{FAKE_SHORT}\"\n")
    rc, out, _ = run_scan(str(probe))
    assert rc == 1
    assert ":P4" in out
    assert ":P3" not in out


# --- C1：带前缀名字的命中针（C1 的失明形状，只钉裸形会让 bug 原路返回） ---

def test_p3_prefixed_name_hits(tmp_path):
    """OPENAI_API_KEY= 这种前缀拼法（config.py 里 llm_api_key/bocha_api_key 的形态）
    必须命中——修复前 \b 在下划线内侧点不着，这一行是零命中假绿面本身。"""
    probe = write_probe(tmp_path, f"OPENAI_API_KEY = \"{FAKE_KEY}\"\n")
    rc, out, _ = run_scan(str(probe))
    assert rc == 1
    assert ":P3" in out


def test_p4_prefixed_name_hits(tmp_path):
    probe = write_probe(tmp_path, f"APP_{JWT_PREFIX} = \"{FAKE_SHORT}\"\n")
    rc, out, _ = run_scan(str(probe))
    assert rc == 1
    assert ":P4" in out
    assert ":P3" not in out


def test_p5_prefixed_name_hits(tmp_path):
    probe = write_probe(tmp_path, f"APP_{JWT_PREFIX}={FAKE_KEY}\n")
    rc, out, _ = run_scan(str(probe))
    assert rc == 1
    assert ":P5" in out


# --- I3：P1 的占位符判据现在判「体」而不是判整串命中 ---

def test_p1_placeholder_guard_judges_the_body_not_the_prefix(tmp_path):
    """修复前 value=m.group(0) 恒含 sk- 的连字符 ⇒ 「值自带分隔符」对 P1 恒成立，
    体内含 sample 子串的随机形密钥被误抑制（sk- 加一串随机体，零命中——见修复轮报告）。
    修复后 value 组 = sk- 之后的 [A-Za-z0-9]{12,} 体，永不含分隔符 ⇒ 词形判据对 P1
    不再触发：第 1 行（体内含 sample）现在命中；第 2 行体不足 12 位连续字母数字，
    P1 形状本身不成立，保持不命中。两向合起来钉住「被判的是体」。"""
    hit_line = "KEY = \"sk-" + "abcdefghijqrsamplemnop\"\n"
    shape_line = "EXAMPLE = \"sk-" + "change_me_please\"\n"
    probe = write_probe(tmp_path, hit_line + shape_line)
    rc, out, _ = run_scan(str(probe))
    assert rc == 1
    p1_lines = [line for line in out.splitlines() if ":P1" in line]
    assert len(p1_lines) == 1, f"P1 应只命中第 1 行，实得：{out}"
    assert p1_lines[0].startswith(str(probe)) and ":1:" in p1_lines[0]


# --- I1：汇总行清点读取；读不了的路径不许假绿 ---

def test_summary_line_tallies_reads(tmp_path):
    probe = write_probe(tmp_path, "nothing secret here\n")
    rc, out, _ = run_scan(str(probe))
    assert rc == 0
    assert "SCANNED 1 READ 1 SKIPPED 0 HITS 0" in out


def test_unreadable_path_exits_2_never_clean(tmp_path):
    """缺失路径混在扫描面上：修复前 SCANNED 1 HITS 0 退 0（假绿）。
    修复后 READ/SKIPPED 记数、退 2；输出面只有数字与路径名，无内容。"""
    probe = write_probe(tmp_path, "nothing secret here\n")
    missing = tmp_path / "not-on-disk.txt"
    rc, out, err = run_scan("--files", str(probe), str(missing))
    assert rc == 2
    # 四列全钉（终评复审计 Minor-1）：原来只写到 `SKIPPED 1` 为止，那个字面串是
    # `SKIPPED 12` 的前缀，等于把 R-T6-5 刚关掉的松弛面在另一列上又开了一次。
    assert summary_of(out) == (2, 1, 1, 0)
    assert err.isascii() and "exiting 2" in err


# --- Minor：docstring 允诺的「同一行同一编号只报一次」现在成立 ---

def test_same_pattern_twice_on_one_line_reports_once(tmp_path):
    dup = f"postgresql+asyncpg://svc:{FAKE_QUOTED}@a.internal:5432/app and " \
          f"postgresql+asyncpg://usr:{FAKE_QUOTED}@b.internal:5432/app\n"
    probe = write_probe(tmp_path, dup)
    rc, out, _ = run_scan(str(probe))
    assert rc == 1
    hit_lines = [line for line in out.splitlines() if ":P2" in line]
    assert hit_lines == [f"{probe}:1:P2"], f"同一行同编号应只报一次：{out}"
    assert hits_of(out) == 1, out


# --- R-T6-2：--files 与裸位置参数必须同义（Task 8 的文档要引用 --files 形状） ---

def test_files_flag_and_positional_paths_agree(tmp_path):
    probe = write_probe(tmp_path, f"prefix {JWT_PREFIX}={FAKE_KEY} suffix\n")
    rc_f, out_f, _ = run_scan("--files", str(probe))
    rc_p, out_p, _ = run_scan(str(probe))
    assert rc_f == rc_p == 1
    assert out_f == out_p
    assert ":P5" in out_f


# ---------------------------------------------------------------------------
# 修复轮 2（t6-rereview-findings C1 残余）：名字轴的收窄一个都不留。
# 轮 1 删了 P3 的前导 \b，却给 P4/P5 换成 (?<![A-Za-z0-9]) lookbehind——那是同一
# 缺陷类的另一种写法：关键词被字母数字直接粘住时依旧零命中（XJWT_SECRET=…），
# 而 brief 的收窄规则只许动「值的形状」。两针各带两行：第 1 行是轮 1 已钉的
# _ 分隔形（不许回退），第 2 行是粘连形（轮 1 代码下不可见）。
# 值继续拼接构造，且 P4 用 9 位值把 P3 的 {12,} 阈值隔开——否则删掉 P4 也不会红。
# ---------------------------------------------------------------------------

def test_p4_alnum_glued_name_hits(tmp_path):
    """P4 引号形：APP_（下划线）与 X（字母直接粘连）两种前缀必须同样可见，且归行正确。"""
    body = (
        f"APP_{JWT_PREFIX} = \"{FAKE_SHORT}\"\n"
        f"X{JWT_PREFIX} = \"{FAKE_SHORT}\"\n"
    )
    probe = write_probe(tmp_path, body)
    rc, out, _ = run_scan(str(probe))
    assert rc == 1
    p4_lines = [line for line in out.splitlines() if ":P4" in line]
    assert p4_lines == [f"{probe}:1:P4", f"{probe}:2:P4"], \
        f"P4 应命中两行（下划线形与粘连形）且归行正确：{out}"
    assert ":P3" not in out, "值仅 9 位，P3 的 {12,} 不该参与——否则这条针删不掉 P4"
    assert hits_of(out) == 2, out


def test_p5_alnum_glued_name_hits(tmp_path):
    """P5 裸值形：MY_JWT_SECRET=（轮 1 可见）与 XJWT_SECRET=（轮 1 不可见）同值同判。"""
    body = (
        f"MY_{JWT_PREFIX}={FAKE_KEY}\n"
        f"X{JWT_PREFIX}={FAKE_KEY}\n"
    )
    probe = write_probe(tmp_path, body)
    rc, out, _ = run_scan(str(probe))
    assert rc == 1
    p5_lines = [line for line in out.splitlines() if ":P5" in line]
    assert p5_lines == [f"{probe}:1:P5", f"{probe}:2:P5"], \
        f"P5 应命中两行（下划线形与粘连形）且归行正确：{out}"
    assert hits_of(out) == 2, out
