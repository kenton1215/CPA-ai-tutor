"""学生账号体系：注册、登录、密码管理与 WeChat 绑定预留。

数据保存在本机 SQLite（data/cpa_user.db），未来部署到服务器后同一路径即成为
服务器数据。密码使用 PBKDF2-HMAC-SHA256 加盐哈希存储，绝不明文落库。

说明：微信网页授权（扫码/OAuth）需要已备案域名与微信开放平台凭据，本地开发
阶段无法完整闭环；本模块已为 wechat_openid 预留唯一索引与绑定接口，
部署后可增量接入：学生扫码 -> 授权回调 -> 按 openid 自动登录/绑定。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from cpa_tutor.config import get_env, get_user_db_path
from cpa_tutor.quota import consume_login

PBKDF2_ITERATIONS = 200_000
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_.@-]{4,40}$")
PHONE_RE = re.compile(r"^1\d{10}$")


class AccountError(RuntimeError):
    """账号操作失败（重复、密码错误等）。"""


def developer_accounts() -> set[str]:
    """读取 .env 中 APP_DEVELOPER_ACCOUNTS 配置的开发者账号白名单。"""
    raw = get_env("APP_DEVELOPER_ACCOUNTS", "")
    if not raw:
        return set()
    return {
        part.strip().lower()
        for part in re.split(r"[,，;；\s]+", raw)
        if part.strip()
    }


def _account_keys(account: str, phone: str = "") -> set[str]:
    keys = {account.lower()}
    if phone and phone.lower() != account.lower():
        keys.add(phone.lower())
    return keys


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def hash_password(password: str, salt_hex: str | None = None) -> dict[str, str]:
    """PBKDF2 加盐哈希。返回 (salt_hex, password_hash)。"""
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return {"salt": salt.hex(), "hash": digest.hex()}


def verify_password(password: str, salt_hex: str, expected_hash: str) -> bool:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        PBKDF2_ITERATIONS,
    )
    return hmac.compare_digest(digest.hex(), expected_hash)


class AccountStore:
    """用户表与账号操作。

    与 WeakPointStore 共用 data/cpa_user.db；建表使用 CREATE TABLE IF NOT EXISTS，
    幂等可安全重复调用。
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else get_user_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._sync_developer_roles()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL UNIQUE,
                    phone TEXT,
                    nickname TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    wechat_openid TEXT UNIQUE,
                    role TEXT NOT NULL DEFAULT 'student',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT,
                    free_logins_used INTEGER NOT NULL DEFAULT 0,
                    last_login_date TEXT NOT NULL DEFAULT '',
                    paid INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            self._migrate_users_columns(connection)

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
        try:
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.OperationalError:
            return set()
        return {row[1] for row in rows}

    @classmethod
    def _migrate_users_columns(cls, connection: sqlite3.Connection) -> None:
        """幂等加列：老库自动补上免费登录次数相关字段。"""
        columns = cls._table_columns(connection, "users")
        if "free_logins_used" not in columns:
            connection.execute(
                "ALTER TABLE users ADD COLUMN free_logins_used INTEGER NOT NULL DEFAULT 0"
            )
        if "last_login_date" not in columns:
            connection.execute(
                "ALTER TABLE users ADD COLUMN last_login_date TEXT NOT NULL DEFAULT ''"
            )
        if "paid" not in columns:
            connection.execute(
                "ALTER TABLE users ADD COLUMN paid INTEGER NOT NULL DEFAULT 0"
            )

    def _sync_developer_roles(self) -> None:
        """以 .env 白名单为唯一事实来源同步角色。

        - 白名单中的账号/手机号 -> developer；
        - 不在白名单中的 developer 会被降回 student（防止权限残留）。
        """
        allowed = developer_accounts()
        with self._connect() as connection:
            if allowed:
                placeholders = ",".join("?" for _ in allowed)
                # role <> 'guest'：游客行绝不参与白名单同步（提升/降级均不适用）
                connection.execute(
                    f"""
                    UPDATE users SET role='developer', updated_at=?,
                    account=lower(account)
                    WHERE active=1 AND role <> 'guest' AND (
                        lower(account) IN ({placeholders})
                        OR lower(phone) IN ({placeholders})
                    )
                    """,
                    (_now(), *allowed, *allowed),
                )
                connection.execute(
                    f"""
                    UPDATE users SET role='student', updated_at=?
                    WHERE role='developer' AND active=1
                      AND lower(account) NOT IN ({placeholders})
                      AND lower(phone) NOT IN ({placeholders})
                    """,
                    (_now(), *allowed, *allowed),
                )
            else:
                connection.execute(
                    "UPDATE users SET role='student', updated_at=? WHERE role='developer'",
                    (_now(),),
                )

    @staticmethod
    def _row_to_user(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        user = dict(row)
        user.pop("password_hash", None)
        user.pop("salt", None)
        return user

    def _require_active(self, user: dict[str, Any] | None) -> dict[str, Any] | None:
        if user and not user.get("active"):
            return None
        return user

    def register(
        self,
        account: str,
        password: str,
        nickname: str = "",
        *,
        phone: str = "",
    ) -> dict[str, Any]:
        """注册学生账号。

        account 可以是手机号或自定义登录名；手机号会同时写入 phone 字段。
        """
        account = (account or "").strip()
        password = password or ""
        nickname = (nickname or account).strip()[:30]
        phone = (phone or "").strip()

        if not account:
            raise AccountError("请输入登录账号或手机号。")
        if not (ACCOUNT_RE.match(account) or PHONE_RE.match(account)):
            raise AccountError("账号需为 4-40 位字母/数字，或中国大陆 11 位手机号。")
        if len(password) < 6:
            raise AccountError("密码至少需要 6 位。")
        if phone and not PHONE_RE.match(phone):
            raise AccountError("手机号格式不正确。")

        secret = hash_password(password)
        now = _now()
        allowed = developer_accounts()
        role = (
            "developer"
            if _account_keys(account, phone) & allowed
            else "student"
        )
        stored_phone = (
            phone or account if PHONE_RE.match(account) else phone or None
        )
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO users (
                        account, phone, nickname, password_hash, salt, role,
                        active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        account.lower() if ACCOUNT_RE.match(account) else account,
                        stored_phone,
                        nickname,
                        secret["hash"],
                        secret["salt"],
                        role,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AccountError("该账号或手机号已注册，请直接登录。") from exc
            # 注册成功即视为一次登录会话（同一天重复登录不重复扣）
            consume_login(connection, cursor.lastrowid)
            row = connection.execute(
                "SELECT * FROM users WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            return self._row_to_user(row) or {}

    def authenticate(self, account: str, password: str) -> dict[str, Any] | None:
        account = (account or "").strip()
        if not account or not password:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE account=?", (account,)
            ).fetchone()
            if row is None:
                # 兼容直接用手机号注册后以手机号登录的情况
                row = connection.execute(
                    "SELECT * FROM users WHERE phone=?", (account,)
                ).fetchone()
            if row is None:
                return None
            if not row["active"]:
                return None
            if row["role"] == "guest":
                return None  # 游客无密码，只能通过浏览器 Cookie 识别进入
            if not verify_password(password, row["salt"], row["password_hash"]):
                return None
            if row["role"] not in ("developer", "guest") and (
                _account_keys(row["account"], row["phone"]) & developer_accounts()
            ):
                connection.execute(
                    "UPDATE users SET role='developer', updated_at=? WHERE id=?",
                    (_now(), row["id"]),
                )
            consume_login(connection, row["id"])
            connection.execute(
                "UPDATE users SET last_login_at=? WHERE id=?",
                (_now(), row["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM users WHERE id=?", (row["id"],)
            ).fetchone()
            return self._row_to_user(updated)

    @staticmethod
    def _guest_token(guest_id: str) -> str:
        """把 Cookie 中的游客标识清洗为可安全用作账号的 token。"""
        return "".join(ch for ch in (guest_id or "") if ch.isalnum())[:16]

    def create_guest(self, guest_id: str, nickname: str = "游客") -> dict[str, Any]:
        """创建游客身份行（role='guest'，无密码，无法用密码登录）。

        guest_id 来自浏览器 Cookie（cpa_guest_id），保证同一浏览器下次
        访问仍能找回该游客及其学习记录。创建即计入一次登录会话。
        """
        token = self._guest_token(guest_id) or os.urandom(8).hex()
        now = _now()
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO users (
                        account, nickname, password_hash, salt, role,
                        active, created_at, updated_at
                    ) VALUES (?, ?, '', '', 'guest', 1, ?, ?)
                    """,
                    (f"guest_{token}", (nickname or "游客").strip()[:30], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise AccountError("创建游客身份失败，请刷新页面重试。") from exc
            consume_login(connection, cursor.lastrowid)
            row = connection.execute(
                "SELECT * FROM users WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            return self._row_to_user(row) or {}

    def find_guest(self, guest_id: str) -> dict[str, Any] | None:
        """按 Cookie 中的游客标识查找游客账号。"""
        token = self._guest_token(guest_id)
        if not token:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE account=? AND role='guest'",
                (f"guest_{token}",),
            ).fetchone()
            return self._row_to_user(row)

    def inherit_guest_data(self, guest_id: int, new_user_id: int) -> int:
        """把游客的学习记录迁移给新注册账号，并删除游客行。返回迁移条数。"""
        moved = 0
        with self._connect() as connection:
            for table in ("weak_points", "exam_attempts", "exam_drafts"):
                exists = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    continue
                cursor = connection.execute(
                    f"UPDATE {table} SET user_id=? WHERE user_id=?",
                    (int(new_user_id), int(guest_id)),
                )
                moved += cursor.rowcount
            connection.execute(
                "DELETE FROM users WHERE id=? AND role='guest'", (int(guest_id),)
            )
        return moved

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()
            return self._row_to_user(row)

    def update_profile(self, user_id: int, nickname: str) -> dict[str, Any] | None:
        nickname = (nickname or "").strip()[:30]
        if not nickname:
            raise AccountError("昵称不能为空。")
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET nickname=?, updated_at=? WHERE id=?",
                (nickname, _now(), user_id),
            )
            row = connection.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()
            return self._row_to_user(row)

    def change_password(
        self, user_id: int, old_password: str, new_password: str
    ) -> None:
        if len(new_password or "") < 6:
            raise AccountError("新密码至少需要 6 位。")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if row is None:
                raise AccountError("用户不存在。")
            if not verify_password(old_password or "", row["salt"], row["password_hash"]):
                raise AccountError("原密码不正确。")
            secret = hash_password(new_password)
            connection.execute(
                "UPDATE users SET password_hash=?, salt=?, updated_at=? WHERE id=?",
                (secret["hash"], secret["salt"], _now(), user_id),
            )

    def bind_wechat(self, user_id: int, openid: str) -> None:
        openid = (openid or "").strip()
        if not openid:
            raise AccountError("WeChat openid 不能为空。")
        with self._connect() as connection:
            try:
                connection.execute(
                    "UPDATE users SET wechat_openid=?, updated_at=? WHERE id=?",
                    (openid, _now(), user_id),
                )
            except sqlite3.IntegrityError as exc:
                raise AccountError("该微信已绑定其他账号。") from exc

    def find_by_wechat(self, openid: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE wechat_openid=?", (openid,)
            ).fetchone()
            return self._row_to_user(row)

    def list_active_users(self) -> list[dict[str, Any]]:
        # 游客是临时身份行，不出现在正式用户列表（开发者管理等界面）
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, account, phone, nickname, wechat_openid, role, "
                "created_at, last_login_at FROM users "
                "WHERE active=1 AND role <> 'guest' "
                "ORDER BY created_at DESC"
            ).fetchall()
            return [self._row_to_user(row) or {} for row in rows]
