"""Phase 8a Task 8：seed 口令脚本（入库版种子，先例 scripts/seed_business_data.py）。

干什么：
    ① dev 用户 …0002（dev@example.com）设置真 bcrypt 口令 + role='admin'
       （T6 处置轮里手工 UPDATE 过的动作，这里固化成可重放）；
    ② 建第二组织 …0003「演示组织B」+ 成员 …0004（member2@example.org,
       role='member'）——跨 org 隔离套件（test_p8_isolation.py）的两边身份。
       （8b T10：邮箱从 @example.test 迁出，旧行删除重造见 MEMBER2_EMAIL 处注。）

口令纪律（spec §2.7，一条都不许破）：
    - 口令只从**运行环境**进来：环境变量 SEED_DEV_PASSWORD / SEED_MEMBER2_PASSWORD，
      缺失则 getpass 交互输入（不回显、不进 shell 历史）。
    - 绝不写入 .env / 任何文件 / 任何日志；本脚本只打印 id 与布尔。
    - 取法（文档口径）：口令由操作者自持（例如
      `python -c "import secrets;print(secrets.token_urlsafe(18))"` 生成后
      存自己的密码管理器），跑脚本时经 env 传入。

幂等：可反复执行——用户/org 已存在则只重置口令（与 role 归位），不报错不重复插。

用法（在 backend/ 目录下）：
    SEED_DEV_PASSWORD=*** SEED_MEMBER2_PASSWORD=*** .venv\\Scripts\\python.exe scripts\\seed_dev_users.py
    （不带 env 直接跑则两次 getpass 交互）
"""
import asyncio
import getpass
import os
import sys
import uuid
from pathlib import Path

# 和 seed_business_data.py 同一套办法：直接 python xxx.py 就能跑
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.security import hash_password
from app.data.db import AsyncSessionLocal
from app.data.models import Organization, User

# 固定 id：…0001/…0002 是首个迁移种下的 dev org/user（测试侧常量 auth_helpers 引用），
# …0003/…0004 由本脚本种——跨进程、跨重跑都指同一行，隔离套件直接按 id 寻身份。
DEV_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
ORG2_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
MEMBER2_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
# 8b T10（R-T6 同款卫生账）：@example.test 是 RFC 6761 special-use 保留域，
# 演示数据不该占它。改 .org 后本脚本跑一次即完成「删除重造」（id 仍钉死 …0004，
# 幂等分支命中"已存在→重置"，故需先手工删旧行再跑——本轮 SEED_* env 未预设，
# 按口令纪律跳过执行，欠账留用户手执，Task 10 报告如实记）。
MEMBER2_EMAIL = "member2@example.org"


def _password_from_env_or_prompt(env_key: str, who: str) -> str:
    """env 优先；缺失才 getpass。两条路径的口令都只活在内存里。"""
    pw = os.environ.get(env_key)
    if pw:
        return pw
    return getpass.getpass(f"为 {who} 输入新口令（不回显）： ")


async def main() -> None:
    dev_pw = _password_from_env_or_prompt("SEED_DEV_PASSWORD", "dev@example.com(…0002)")
    member2_pw = _password_from_env_or_prompt("SEED_MEMBER2_PASSWORD", f"{MEMBER2_EMAIL}(…0004)")
    if not dev_pw or not member2_pw:
        print("口令为空：拒绝执行（宁可不种，也不落一个空口令账号）")
        sys.exit(1)

    async with AsyncSessionLocal() as s:
        # ① dev 用户：存在则重置口令 + role 归位 admin
        dev = await s.get(User, DEV_USER_ID)
        if dev is None:
            print(f"users …0002 不存在——迁移未跑到位，先检查 alembic 历史")
            sys.exit(1)
        dev.password_hash = hash_password(dev_pw)
        dev.role = "admin"

        # ② 第二组织 + 成员（幂等：有则只重置口令）
        org2 = await s.get(Organization, ORG2_ID)
        org2_created = org2 is None
        if org2_created:
            org2 = Organization(id=ORG2_ID, name="演示组织B")
            s.add(org2)
            await s.flush()
        member2 = await s.get(User, MEMBER2_ID)
        member2_created = member2 is None
        if member2_created:
            member2 = User(
                id=MEMBER2_ID, organization_id=ORG2_ID, email=MEMBER2_EMAIL,
                password_hash=hash_password(member2_pw), full_name="成员二",
                role="member", is_active=True,
            )
            s.add(member2)
        else:
            member2.password_hash = hash_password(member2_pw)
            member2.role = "member"
        await s.commit()

        # 回读自检在下面的新会话里做：只打印 id 与布尔，口令零露面
    async with AsyncSessionLocal() as s:
        dev_ok = await s.get(User, DEV_USER_ID)
        m2 = await s.get(User, MEMBER2_ID)
        o2 = await s.get(Organization, ORG2_ID)
        print(f"dev …0002: id={DEV_USER_ID} role={dev_ok.role if dev_ok else '-'} "
              f"hash_set={bool(dev_ok and dev_ok.password_hash)}")
        print(f"org …0003: id={o2.id if o2 else '-'} name={o2.name if o2 else '-'} "
              f"created={org2_created}")
        print(f"user …0004: id={m2.id if m2 else '-'} email={m2.email if m2 else '-'} "
              f"role={m2.role if m2 else '-'} hash_set={bool(m2 and m2.password_hash)} "
              f"created={member2_created}")


if __name__ == "__main__":
    asyncio.run(main())
