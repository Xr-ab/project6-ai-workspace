"""census_tables 的纯函数用例：DSN 构造、渲染、指纹。

连库的那条路（_dump）不在单测里跑——那是 db 标记的活。这里只测「给定行，输出什么」，
因为 11c 的守卫靠的就是这份输出的稳定性。
"""
from pathlib import Path

import pytest


def test_build_dsn_keeps_credentials_and_swaps_db_and_port():
    from scripts.census_tables import build_dsn

    dsn = build_dsn(
        "postgresql+asyncpg://app:app_dev_pwd@localhost:5432/ai_workspace",
        "enterprise_data",
        port=5543,
    )
    assert dsn == "postgresql://app:app_dev_pwd@localhost:5543/enterprise_data"


def test_build_dsn_rejects_dsn_without_scheme_or_host():
    from scripts.census_tables import build_dsn

    with pytest.raises(ValueError):
        build_dsn("ai_workspace", "enterprise_data")


def test_dsn_host_port_roundtrips_demo_stack_port():
    from scripts.census_tables import build_dsn, dsn_host_port

    dsn = build_dsn(
        "postgresql+asyncpg://app:app_dev_pwd@localhost:5432/ai_workspace",
        "ai_workspace",
        port=5543,
    )
    assert dsn_host_port(dsn) == ("localhost", 5543)


def test_render_counts_is_sorted_and_formats_full_names():
    from scripts.census_tables import render_counts

    assert render_counts([("ai_workspace.messages", 309), ("ai_workspace.agent_runs", 231)]) == [
        "ai_workspace.agent_runs=231",
        "ai_workspace.messages=309",
    ]


def test_build_dsn_requires_port_when_host_is_given():
    from scripts.census_tables import build_dsn

    with pytest.raises(ValueError):
        build_dsn(
            "postgresql+asyncpg://app:app_dev_pwd@localhost:5432/ai_workspace",
            "ai_workspace",
            host="localhost",
        )


def test_fingerprint_changes_when_a_count_changes():
    from scripts.census_tables import fingerprint

    a = fingerprint(["t=1"])
    assert a == fingerprint(["t=1"])
    assert a != fingerprint(["t=2"])


def test_script_is_tracked_and_reads_no_env_file():
    """census 脚本不许读 backend/.env（全局禁令）：它只从 argv 拿 DSN 形状。"""
    src = (Path(__file__).resolve().parents[2] / "scripts" / "census_tables.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (".env", "load_dotenv"):
        assert forbidden not in src
