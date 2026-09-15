"""免费登录次数限额：游客（未注册）与注册考生的免费次数控制。

口径：按登录会话计次，同一天重复登录不重复扣；限额在 .env 配置
（FREE_GUEST_LOGINS / FREE_USER_LOGINS，默认 5 / 10）。免费次数用完后
由入口锁定学习功能，提示注册或联系管理员；users.paid 预留为未来
付费开通字段（当前不做支付）。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from cpa_tutor.config import free_login_limits

GUEST_ROLE = "guest"


def limits() -> dict[str, int]:
    """返回 {guest: 游客免费登录次数, user: 注册考生免费登录次数}。"""
    return free_login_limits()


def consume_login(connection: sqlite3.Connection, user_id: int) -> int:
    """记录一次登录会话；同一天重复登录不重复扣。返回已用次数。"""
    today = date.today().isoformat()
    row = connection.execute(
        "SELECT free_logins_used, last_login_date FROM users WHERE id=?",
        (int(user_id),),
    ).fetchone()
    if row is None:
        return 0
    used = int(row["free_logins_used"] or 0)
    if row["last_login_date"] == today:
        return used
    connection.execute(
        "UPDATE users SET free_logins_used=?, last_login_date=? WHERE id=?",
        (used + 1, today, int(user_id)),
    )
    return used + 1


def quota_limit(user: dict[str, Any] | None) -> int:
    """该用户的免费登录上限；开发者或已付费用户返回 -1（不受限）。"""
    if not user:
        return limits()[GUEST_ROLE]
    if user.get("paid") or user.get("role") == "developer":
        return -1
    key = GUEST_ROLE if user.get("role") == GUEST_ROLE else "user"
    return limits()[key]


def quota_exhausted(user: dict[str, Any] | None) -> bool:
    """免费次数是否已用完（决定是否锁定学习功能）。"""
    limit = quota_limit(user)
    if limit < 0:
        return False
    return int((user or {}).get("free_logins_used") or 0) >= limit


def quota_status(user: dict[str, Any] | None) -> str:
    """“免费次数：已用 X / Y 次”或“不限次”文案。"""
    limit = quota_limit(user)
    if limit < 0:
        return "免费次数：不限次"
    used = int((user or {}).get("free_logins_used") or 0)
    return f"免费次数：已用 {used} / {limit} 次"
