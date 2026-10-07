"""库存域 DSN 派生（app/core/enterprise_dsn.py:12 / 15）。

spec §7 针① 的反向保护：派生错了的表现是「应用连回自己的库」——跨源演示照样绿。
所以每一类不可静默放行的形状都必须炸。
"""
import re

import pytest

from app.core.enterprise_dsn import _PREFIX, enterprise_database_url

BASE = "postgresql+asyncpg://app:app_dev_pwd@localhost:5432/ai_workspace"


def test_happy_path_swaps_only_the_database_name():
    assert enterprise_database_url(BASE, "enterprise_data") == (
        "postgresql+asyncpg://app:app_dev_pwd@localhost:5432/enterprise_data"
    )


def test_password_containing_slash_is_not_mistaken_for_the_db_segment():
    dsn = "postgresql+asyncpg://app:a/b@localhost:5432/ai_workspace"
    assert enterprise_database_url(dsn, "enterprise_data").endswith("/enterprise_data")
    assert "a/b@" in enterprise_database_url(dsn, "enterprise_data")   # 凭据段原样


def test_password_containing_slash_without_db_segment_still_rejected():
    with pytest.raises(ValueError, match="末尾没有库名段"):
        enterprise_database_url("postgresql+asyncpg://app:a/b@localhost:5432", "x")


@pytest.mark.parametrize("dsn,db,why", [
    ("postgresql://app:pw@localhost:5432/ai_workspace", "x", None),
    (BASE, "", "db_name 不能为空"),
    (BASE, "a/b", "裸库名"),
    (BASE, "a?b", "裸库名"),
    (BASE, "a@b", "裸库名"),
    ("postgresql+asyncpg://app:pw@localhost:5432", "x", "末尾没有库名段"),
    ("postgresql+asyncpg://app:pw@localhost:5432/ai_workspace?sslmode=require", "x", "查询串"),
])
def test_every_silent_wrong_answer_raises_instead_of_returning_the_original(dsn, db, why):
    # match 走的是 re.search：_PREFIX 里的「+」当正则读是「一个或多个 l」，
    # 逐字匹配必须 re.escape，否则前缀那一条永远「不匹配 → 用例红」。
    with pytest.raises(ValueError, match=why if why else re.escape(_PREFIX)):
        enterprise_database_url(dsn, db)
