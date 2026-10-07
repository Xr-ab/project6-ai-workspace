#!/usr/bin/env python
"""#25 泄密口径门禁（Phase 11b T6，spec §10）。

三条设计约束（spec §10）逐条落地在这儿：
1. 只打印 PATH:LINE:PATTERN_ID，绝不打印命中内容——CI 日志的可见面比 backend/.env 大得多，
   门禁自己不能成为泄密面。test_output_never_contains_the_matched_value 是这条的针。
2. 误伤防线用「占位符形状」判据，不用文件白名单（白名单会腐烂，下一个 *.env.example 一加就绕过）。
3. 附加硬检查：渲染产物不许在跟踪列表里（ARTIFACT_NAMES）——这是 11a 那次 compose config
   渲染落盘事故的结构性收口，守的是「看起来只读的命令会渲染出口令」这条没人机器在守的常识。

诚实边界（spec §10 末段，读文档时别把它读成「密钥问题已解决」）：只覆盖**已入库且当前跟踪**的内容。
扫不到未跟踪文件（backend/.env 与 55 个含字面量的 scratch 套件都在跟踪面外，这是刻意的），
扫不到已进历史的旧提交，也防不住未来有人把渲染输出重定向到磁盘——那仍然是纪律 + config -q 的
结构性免疫。历史里的东西只能靠轮转（LLM_API_KEY / BOCHA_API_KEY / JWT_SECRET，2026-10-01 已裁定不轮换，判据在 docs/09 F 表 #25）。

模式来源：spec §10 首版四条。落地成五条，且每条都收窄过——收窄的实测依据写在 docs/02 §11
（原样 spec 四条在今天 231 个跟踪文件上是 40 处命中、0 处真密钥，全为 alembic 样例注释、
类型标注、中文注释与历史计划里的 DSN 散文）。第 4 条「JWT_SECRET= 后跟非占位值」拆成
P4（引号形态）与 P5（裸值形态），因为合在一条里必然误伤 config.py 的类型标注。
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

# backend/scripts/ -> 项目根。CI 里（subtree split 之后）项目根就是仓库根，
# 本地里它是 git 根下的 project6-ai-workspace/ —— 两种形状下 git ls-files 的
# cwd 相对路径都成立，所以这个文件不需要为 CI 做任何特判。
ROOT = Path(__file__).resolve().parents[2]

SECRET_CHARS = r"[A-Za-z0-9_\-+/=.]"

PATTERNS = (
    # C1/I3 修复轮 1（评审 t6-review-findings.md）+ C1 残余 修复轮 2：名字轴零收窄。
    # 收窄只许发生在「值的形状」（{12,}/{8,} 与占位符判据）——brief 的先读②写死的规则。
    # 轮 1 删了 P3 的前导 \b，却给 P4/P5 换成 (?<![A-Za-z0-9]) lookbehind：那是同一缺陷类
    # 换一种写法（名字轴上的收窄），后果实测过——XJWT_SECRET=<20 位高熵串> 五条模式全零命中，
    # 而 MY_JWT_SECRET=<同值> 命中 P5。轮 2 把 lookbehind 也删了：P4/P5 现在**没有任何**
    # 前导锚点。后来人若想把 \b 或 lookbehind 加回去，先读这条注释，再看
    # test_p4_alnum_glued_name_hits / test_p5_alnum_glued_name_hits（两条针会红）。
    # 放宽的代价实测为零：263 个跟踪文件在无锚点形状下依旧 HITS 0（spec 首版那 40 处噪声
    # 全部是「值的形状」blamed——散文里的 pwd / a/b / 类型标注，与名字前缀无关）。
    # I3：P1 的判据组从隐式整串改为 body（sk- 前缀自带连字符，旧形状下「值自带分隔符」
    # 对 P1 恒成立 ⇒ 含 sample 子串的真密钥体被误抑制）。
    ("P1", re.compile(r"sk-(?P<value>[A-Za-z0-9]{12,})"), "value"),
    ("P2", re.compile(r"//[A-Za-z0-9_.%+-]+:(?P<value>" + SECRET_CHARS + r"{8,})@"), "value"),
    ("P3", re.compile(
        r"(?i)(api[_-]?key|secret|passwd|password|token|access[_-]?key)\b"
        r"\s*[:=]\s*[\"'](?P<value>" + SECRET_CHARS + r"{12,})[\"']"), "value"),
    ("P4", re.compile(
        r"(?i)JWT_SECRET\b\s*[:=]\s*[\"'](?P<value>[^\"']{8,})[\"']"), "value"),
    ("P5", re.compile(
        r"(?i)JWT_SECRET\s*=\s*(?P<value>" + SECRET_CHARS + r"{12,})"), "value"),
)

# spec §10 约束 3：渲染产物一旦落盘就必然长成这些名字。
ARTIFACT_NAMES = (
    "*rendered*config*", "*rendered*.yml", "*rendered*.yaml", "*.rendered",
    "acceptance-config.json", "compose-config*.json", "docker-compose.rendered*",
)

# 值里写着这些词 ⇒ 它是「字段名/自述占位」，不是凭据。
TRIGGER_WORDS = ("api_key", "apikey", "access_key", "secret", "passwd", "password", "token")
PLACEHOLDER_WORDS = (
    "change_me", "change-me", "changeme", "replace_me", "replace-me", "replace_",
    "test_only", "test-only", "not_a_real", "not-a-real", "dummy", "example",
    "sample", "placeholder", "scratch", "your_", "your-", "todo", "fixme",
)
# spec §10 点名的两个具体值 + 空值。app_dev_pwd 是本机开发库口令，11a 起就在 compose 默认值里，
# 它进 allowlist 是 spec 写的，不是我给的。
EXACT_PLACEHOLDERS = ("", "app_dev_pwd", "none", "null")


def is_placeholder(value: str) -> bool:
    v = value.strip().strip("\"'")
    low = v.lower()
    if low in EXACT_PLACEHOLDERS:
        return True
    if re.fullmatch(r"\$\{[^{}]*\}", v) or re.fullmatch(r"<[^<>]+>", v):
        return True
    if not v.strip():
        return True
    # 词形判据只在「值自带分隔符」时生效：真凭据是高熵串，scratch-local-secret /
    # p6_access_token / test-only-not-a-real-secret 这类自述名字都带 - _ .，
    # 而 base64 与 sk- 随机串不带。少了这道限定，一个恰好含 "sample" 子串的真密钥会被放过。
    if not re.search(r"[-_.]", v):
        return False
    if any(w in low for w in TRIGGER_WORDS):
        return True
    return any(w in low for w in PLACEHOLDER_WORDS)


def scan_line(line: str) -> list[str]:
    """一行能命中的模式编号（同一行多条同模式只报一次编号，行号已够定位）。

    修复轮 1 Minor：docstring 的允诺此前不成立——循环是每条匹配各 append 一次，
    一行两个 DSN 会报两条一模一样的 :P2。现在按 (行, 模式编号) 去重：命中判定
    （含占位符抑制）逐匹配照旧，只是同一行同一编号不再重复输出。
    """
    out: list[str] = []
    for pid, rx, group in PATTERNS:
        for m in rx.finditer(line):
            value = m.group(group) if group else m.group(0)
            if is_placeholder(value):
                continue
            if pid not in out:
                out.append(pid)
            break
    return out


def scan_text(rel: str, text: str) -> list[str]:
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for pid in scan_line(line):
            hits.append(f"{rel}:{lineno}:{pid}")
    return hits


def artifact_hits(names: list[str]) -> list[str]:
    out = []
    for rel in names:
        base = Path(rel).name
        for pat in ARTIFACT_NAMES:
            if fnmatch.fnmatch(base, pat) or fnmatch.fnmatch(rel, pat):
                out.append(f"{rel}:0:ARTIFACT({pat})")
                break
    return out


def tracked_files() -> list[str]:
    proc = subprocess.run(["git", "ls-files", "-z"], cwd=str(ROOT), capture_output=True)
    if proc.returncode != 0:
        # ASCII only: exit-2 面在 cp936 控制台不许乱码（修复轮 1 Minor）。
        # ROOT 本身可能含非 ASCII（本机就是 D:\工程学习），所以走 ascii() 转义而不是原样插值。
        sys.stderr.write(
            f"error: git ls-files failed (cwd={ascii(str(ROOT))}); the gate's scope is "
            "tracked content, so this must exit 2 and never report clean\n")
        raise SystemExit(2)
    return [n.decode("utf-8") for n in proc.stdout.split(b"\0") if n]


def read_text(rel: str) -> str | None:
    """读一个跟踪文件；二进制（NUL）与非 UTF-8 一律返回 None = 不在射程内。

    不做扩展名白名单：11a 的渲染产物是 .json/.yml，历史计划的泄密面是 .md，
    按扩展名筛等于把「散文里贴真密钥」这一整类放走。
    """
    path = Path(rel) if Path(rel).is_absolute() else ROOT / rel
    if not path.is_file():
        return None
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="跟踪面泄密门禁（spec §10）")
    ap.add_argument("--files", nargs="+", metavar="PATH",
                    help="只扫这些路径（红侧注入与单测用；默认面是 git ls-files）")
    # 与 --files 同义的裸位置参数形态：tests/unit/test_secret_scan.py 的探针按
    # `secret_scan.py PATH` 调用（run_scan(str(probe))），CI 侧只用默认面与 --artifacts-only。
    # 两个入口走同一组 names，语义没有分叉：给了路径就只扫这些路径。
    ap.add_argument("paths", nargs="*", metavar="PATH",
                    help="同 --files（裸路径形态，单测探针用）")
    ap.add_argument("--artifacts-only", action="store_true",
                    help="只做渲染产物入库检查（CI 的 compose job）")
    args = ap.parse_args()

    names = args.files if args.files else (args.paths if args.paths else tracked_files())
    hits = artifact_hits(names)
    reads = 0
    if not args.artifacts_only:
        for rel in names:
            text = read_text(rel)
            if text is None:
                continue
            reads += 1
            hits.extend(scan_text(rel, text))
    # I1 修复轮 1：SCANNED 只说明「在列表里」，不说明「读进去了」。汇总行把读取
    # 也记数（数字面，不含任何内容），有路径读不了就不再报告干净——退 2。
    # UTF-16 / 二进制 / 缺失的跟踪文件带着真泄漏静默出射程，此前门禁与
    # test_tracked_face_is_clean 会双双假绿。
    skipped = len(names) - reads if not args.artifacts_only else 0
    for h in hits:
        print(h)
    print(f"SCANNED {len(names)} READ {reads} SKIPPED {skipped} HITS {len(hits)}")
    if skipped > 0:
        # ASCII only（修复轮 1 Minor，同上）。
        sys.stderr.write(
            f"error: {skipped} path(s) on the scan face could not be read "
            "(missing / binary / non-UTF-8); refusing to report clean, exiting 2\n")
        return 2
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
