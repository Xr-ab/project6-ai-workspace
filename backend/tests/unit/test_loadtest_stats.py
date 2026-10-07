"""压测器的统计口径与解析层用例：零网络、零容器。

这一层为什么必须单独存在：spec §5.5 拒绝预设 SLA，那报告的全部价值就只剩「数字是真的」。
数字是不是真的，取决于下面这几个纯函数——它们要能在没有一台容器的前提下被证。
"""
import pytest


def test_percentile_uses_nearest_rank():
    from loadtest.loadtest import percentile

    v = [float(i) for i in range(1, 101)]  # 1..100
    assert percentile(v, 50) == 50.0
    assert percentile(v, 95) == 95.0
    assert percentile(v, 99) == 99.0
    assert percentile(v, 100) == 100.0


def test_percentile_empty_raises_instead_of_returning_zero():
    from loadtest.loadtest import percentile

    with pytest.raises(ValueError):
        percentile([], 95)


def test_summarise_counts_ok_as_2xx_only():
    from loadtest.loadtest import summarise

    s = summarise([10.0, 20.0, 30.0], [200, 202, 429, 500], 0, 1.0)
    assert s["n"] == 4
    assert s["ok"] == 2          # 200 与 202
    assert s["http_429"] == 1
    assert s["http_5xx"] == 1
    assert s["exc"] == 0
    assert s["qps"] == pytest.approx(4.0)
    assert s["p95"] == 30.0
    # R20：状态码直方图与「其他」桶。404（双前缀）/403（audit_page 非 admin）得看得见起因。
    assert s["http_other"] == 0
    assert s["codes"] == {"200": 1, "202": 1, "429": 1, "500": 1}
    # R15：measured 交代这一行的分位数到底基于几个样本（n 含异常，两者不等是常态）。
    assert s["measured"] == 3
    # R26：3xx 绝不能被当成 ok 吞掉。httpx 的 follow_redirects 默认虽是 False，但那是默认值
    # 不是口径——一旦被跟随，302→200 会把两跳的耗时塞进同一枚样本、还报成一次成功。summarise
    # 侧的分工是把 302 归进 http_other：这样哪怕将来有人改了客户端跟随重定向，这一行也不会
    # 悄悄把 3xx 记成 2xx。
    r3 = summarise([1.0], [200, 302, 404, 429, 503], 0, 1.0)
    assert r3["ok"] == 1          # 只有 200
    assert r3["http_429"] == 1
    assert r3["http_5xx"] == 1    # 503
    assert r3["http_other"] == 2  # 302 与 404


def test_summarise_keeps_every_column_the_markdown_needs():
    from loadtest.loadtest import summarise

    s = summarise([5.0], [200], 0, 0.5)
    for k in ("n", "ok", "http_429", "http_5xx", "exc", "qps", "p50", "p95", "p99", "max"):
        assert k in s
    # 只进 JSON、不进 markdown 的三列（12 列被 spec §5.4 冻住）也得在场。
    for k in ("measured", "codes", "http_other", "success_rate"):
        assert k in s
    assert s["codes"] == {"200": 1}
    assert s["http_other"] == 0


def test_render_markdown_header_matches_spec_verbatim():
    from loadtest.loadtest import render_markdown, summarise

    row = {"target": "tasks_list", "level": "c8", **summarise([1.0], [200], 0, 1.0)}
    text = render_markdown([row])
    assert "| target | level | n | ok | http_429 | http_5xx | exc | qps | p50 | p95 | p99 | max |" in text
    assert "tasks_list" in text
    assert text.count("\n") >= 3  # 表头 + 分隔 + 至少一行
    # R15：全超时的饱和档（n>0、一个样本都没量到）是最想看的一行。旧实现打印 0.0ms，
    # 等于给假表盖「已测」的章（spec §8）；现在百分位是 None，表里渲染成 ASCII '-'
    # （不是 em dash：控制台 GBK，非 ASCII 分隔符会被打成 '?' 或直接把 print 炸掉）。
    empty = summarise([], [], 7, 1.0)
    assert empty["measured"] == 0
    assert empty["n"] == 7
    assert empty["ok"] == 0
    for k in ("p50", "p95", "p99", "max"):
        assert empty[k] is None
    no_samples = render_markdown([{"target": "stats_overview", "level": "c64", **empty}])
    # R27：换成按列位断言，不再用 "0.0" not in 这种子串绊线（任何合法值都能把它误触发，
    # 也拦不住真回归），也不再用 "| - | - | - | - |" 这种位置未明的整串。spec §5.4 冻住的
    # 12 列里第 8..11 是 p50/p95/p99/max，第 7 是 qps。空样本行：四个百分位格必须恰为
    # '-'，而 qps 仍是数字（7 发请求 / 1.0s ⇒ '7.0'）——「没量到百分位」与「确实算得出
    # 吞吐」是两件事，列位断言把两者同时钉住。
    data_row = next(ln for ln in no_samples.splitlines() if "stats_overview" in ln)
    cells = [c.strip() for c in data_row.strip().strip("|").split("|")]
    assert len(cells) == 12
    assert cells[0] == "stats_overview"
    assert cells[7] == "7.0"
    assert cells[8:12] == ["-", "-", "-", "-"]


def test_summarise_probe_finds_first_429_and_checks_expectation():
    from loadtest.loadtest import SETTLE_S, WINDOW_S, summarise_probe

    # R24：滑窗前提写成数据后，「静置必须盖满一个窗口」是 SETTLE_S > WINDOW_S 这条不等式，
    # 不再靠一个不参与计算的 wait_s 形参（那个已从 summarise_probe 删掉）。WINDOW_S=60 抄自
    # app/core/rate_limit.py:46 的 DEFAULT_WINDOW_S；针断了就说明有人把静置调到睡不空上一段。
    assert WINDOW_S == 60
    assert SETTLE_S > WINDOW_S
    p = summarise_probe([202] * 20 + [429], limit_per_min=20,
                        retry_after="59", code="COMMON_429001")
    assert p["requests_sent"] == 21
    assert p["first_429_index"] == 21
    assert p["expected_first_429_at"] == 21
    assert p["match"] is True
    assert p["retry_after"] == "59"
    assert p["code"] == "COMMON_429001"
    # R16：错误码是「观测值 == 预期值」的比较结果，不是把常量抄进返回值——code 由 _one 从
    # 响应体读回来，code_match 才允许说「spec §5.3 的第三条证了」。
    assert p["code_match"] is True
    # R16：传输层失败不再伪装成伪状态码 0 混进 codes（那会占掉一个位置，把「限流器没响」
    # 和「客户端发不出去」读成同一张表）；这一发里没异常，所以独立计数是 0。
    assert p["exc"] == 0
    # R21：滑窗下的不变量是 limit+1，与静置秒数无关。旧的乘式（limit*wait/60+1）在
    # limit=600、wait=61 时会算出 610 —— 一枚限流器工作正常的 run 会被报成 miss。
    p600 = summarise_probe([202] * 600 + [429], limit_per_min=600, code="COMMON_429001")
    assert p600["expected_first_429_at"] == 601
    assert p600["match"] is True


def test_summarise_probe_reports_a_miss_without_hiding_it():
    from loadtest.loadtest import summarise_probe

    p = summarise_probe([202] * 25, limit_per_min=20)
    assert p["first_429_index"] is None
    assert p["match"] is False   # 没撞上 429 要在报告里明写，不许当「通过」
    # 期望值在场的同时还得说得通：25 发全绿时边界仍在 21，说明「探针该红而没红」是结论，
    # 不是因为期望值被推到了 25 之后（旧乘式在 limit=20/wait=61 下也是 21，换个 rl 就露馅）。
    assert p["expected_first_429_at"] == 21
    assert p["requests_sent"] == 25
    # code 没被观测到（这一发里压根没 429）⇒ code_match 必须是 False，不许默认成立。
    assert p["code_match"] is False


def test_parse_census_reads_counts_and_fingerprint():
    from loadtest.loadtest import parse_census

    c = parse_census("ai_workspace.agent_runs=231\nai_workspace.messages=309\nFINGERPRINT=deadbeef\n")
    assert c["counts"] == {"ai_workspace.agent_runs": 231, "ai_workspace.messages": 309}
    assert c["fingerprint"] == "deadbeef"
    # 逐表计数全可解析 ⇒ dropped 必须是空表。截断过的普查以前会静默变成更小的 counts，
    # 读数的人看不出来少了哪张表——那正是 spec §5.4 要 census 的理由。
    assert c["dropped"] == []
    # 值不是纯数字的行（截断/半行写入）必须留名，不许静默从 counts 里蒸发。
    noisy = parse_census("ai_workspace.agent_runs=231\nai_workspace.messages=oops\n")
    assert noisy["counts"] == {"ai_workspace.agent_runs": 231}
    assert noisy["dropped"] == ["ai_workspace.messages=oops"]


def test_parse_receipts_reads_the_three_lifecycle_keys():
    from loadtest.loadtest import parse_receipts

    r = parse_receipts(
        "== p6demo lifecycle receipts ==\n"
        "down_v_rc=0 project=p6demo\n"
        "port_check_rc=0\n"
        "home_census_diff_rc=0 (0 = 默认项目逐表行数零改动)\n"
    )
    assert r == {"down_v_rc": 0, "port_check_rc": 0, "home_census_diff_rc": 0}
    # R23：来路必须被「验过」而不是「假定」。parse_receipts 对空值键行（port_check_rc=）
    # 从前抛裸 IndexError，except ValueError 抓不住，损坏文件与「还没写」就分不开——
    # 现在必须抛带键名的 ValueError，好让 classify_receipts 把它归进「非空但缺三键」。
    with pytest.raises(ValueError):
        parse_receipts("down_v_rc=0\nport_check_rc=\nhome_census_diff_rc=0\n")
    # classify_receipts 是纯函数，五条分支互斥；main 用它替掉那句没验过的
    # "this_lifecycle_receipts_not_yet_written" 断言。
    from loadtest.loadtest import classify_receipts
    text, src = classify_receipts(path_given=False, path_text=None)     # 没给 --receipts-file
    assert src == "no_receipts_file_passed"
    assert "not_written_at_run_time" in text
    # 复审计 Minor 2：给了路径而文件不在，与压根没给路径也不能共用一句来路——同一类毛病
    # （出处被假设而不是被验出来），只是范围更小。
    _, src_absent = classify_receipts(path_given=True, path_text=None)
    assert src_absent == "path_given_but_file_absent"
    text, src = classify_receipts(path_given=True, path_text="\n   \n")  # 文件确认是空的：唯一能说还没拆栈的分支
    assert "not_yet_written" in src
    assert text != ""
    text, src = classify_receipts(                                       # 非空但缺三键（Task 4 的 P6_STOP_WORKER）
        path_given=True, path_text="stopping worker\npre-teardown junk\n")
    assert "2_non_empty_lines" in src               # 报出看到的非空行数，读者能分清杂行与形状变了
    assert "not_written_at_run_time" in text
    real = "down_v_rc=0\nport_check_rc=0\nhome_census_diff_rc=0\n"         # 齐三键：用原文
    text, src = classify_receipts(path_given=True, path_text=real)
    assert text == real
    assert src == "parsed_from_receipts_file"
    # 五条来路两两不同：只要有任何两条共用一句字符串，这一针就红——「互斥」从此不靠注释自证。
    assert len({src, src_absent, "no_receipts_file_passed",
                "receipts_file_empty_not_yet_written_this_lifecycle",
                "receipts_file_has_2_non_empty_lines_without_the_three_keys"}) == 5


def test_parse_receipts_keeps_a_non_numeric_token_visible_instead_of_raising():
    """探针阶段还没有本次拆栈收据，JSON 里带的是 'pending' 这种明文——不许静默变 0。"""
    from loadtest.loadtest import parse_receipts

    r = parse_receipts("down_v_rc=pending\nport_check_rc=pending\nhome_census_diff_rc=pending\n")
    assert r["down_v_rc"] == "pending"


def test_build_context_requires_the_evidence_keys():
    from loadtest.loadtest import build_context

    census = "ai_workspace.agent_runs=1\nFINGERPRINT=x\n"
    receipts = "down_v_rc=0\nport_check_rc=0\nhome_census_diff_rc=0\n"
    ok = build_context(stack="p6demo", census_text=census, receipts_text=receipts,
                       rl="600", pool="15", git_head="abc", worker_state="stopped",
                       worker_max_jobs="3", ports="5543,5637,8110,8081",
                       receipts_source="file")
    assert set(ok) >= {"stack", "census", "rate_limits", "worker_state", "receipts", "pool", "git_head"}
    # spec §5.4 的上下文块点名要的两件：limits 块里的 worker_max_jobs、stack 块里的四个宿主端口。
    # 名字沿用既有键（rate_limits / stack），这里是往里加字段，不是改名。
    assert ok["rate_limits"]["worker_max_jobs"] == "3"
    assert ok["rate_limits"]["task_per_min"] == "600"
    assert ok["host_ports"] == "5543,5637,8110,8081"
    # R17：收据的来路要在场——占位串不能被后来人读成观测到的 rc。
    assert ok["receipts_source"] == "file"
    with pytest.raises(ValueError):
        build_context(stack="p6demo", census_text="", receipts_text=receipts,
                      rl="600", pool="15", git_head="abc", worker_state="stopped")
    with pytest.raises(ValueError):
        build_context(stack="p6demo", census_text=census, receipts_text="",
                      rl="600", pool="15", git_head="abc", worker_state="stopped")
    # 缺任一件 = 这张表不许搬进 docs/12（spec §5.4「缺一个字段就不许搬」），所以空值必须响，
    # 而不是留个空字符串让 Task 4 手抄。
    with pytest.raises(ValueError):
        build_context(stack="p6demo", census_text=census, receipts_text=receipts,
                      rl="600", pool="15", git_head="abc", worker_state="stopped",
                      worker_max_jobs="", ports="5543,5637,8110,8081", receipts_source="file")
    with pytest.raises(ValueError):
        build_context(stack="p6demo", census_text=census, receipts_text=receipts,
                      rl="600", pool="15", git_head="abc", worker_state="stopped",
                      worker_max_jobs="3", ports="", receipts_source="file")
    with pytest.raises(ValueError):
        build_context(stack="p6demo", census_text=census, receipts_text=receipts,
                      rl="600", pool="15", git_head="abc", worker_state="stopped",
                      worker_max_jobs="3", ports="5543,5637,8110,8081", receipts_source="")


def test_read_targets_are_all_non_mutating():
    """「压测面零写入」是 spec §5.2 的口径，机械钉住：除 SUBMIT_TARGET 外全得是 GET 且非 money。"""
    from loadtest.loadtest import FLOOR_TARGET, READ_TARGETS, SUBMIT_TARGET

    for t in (*READ_TARGETS, FLOOR_TARGET):
        assert t.method == "GET"
        assert t.money is False
    # 路径口径：Target.path 一律从 origin 起算，main 不再补 /api/v1（补了就双前缀 404）。
    # floor 行挂在 origin 根上——nginx 的 location = /healthz 自己应答、根本不反代到 app。
    assert FLOOR_TARGET.path == "/healthz"
    assert all(t.path.startswith("/api/v1/") for t in (*READ_TARGETS, SUBMIT_TARGET))
    assert SUBMIT_TARGET.method == "POST"
    assert len(READ_TARGETS) == 6


def test_read_target_keys_are_the_six_faces_the_spec_names():
    from loadtest.loadtest import READ_TARGETS

    # spec §5.2 的表就是这六个名字（两条 stats 聚合 + 三条列表 + 一页审计）。
    # 之前这里钉的是「计划里那六条」，于是这根针对着偏离 spec 的注册表报绿——改名之后
    # 它才真的是「spec 点名的那六条」的守卫：少一条 stats、多一条 documents 都会红。
    assert {t.key for t in READ_TARGETS} == {
        "stats_overview", "stats_usage", "tasks_list",
        "audit_page", "workflows_list", "evals_runs_list",
    }
