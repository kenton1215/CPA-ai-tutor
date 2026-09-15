"""学生个人学习记录：未掌握知识点、考试记录、错题本（SQLite 持久化）。

所有记录均通过 user_id 与学生账号关联，保证不同学生的模考成绩、错题和
未掌握知识点相互隔离。数据保存在 data/cpa_user.db，未来部署到服务器后
同一文件即成为服务器端数据库。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from cpa_tutor.config import get_user_db_path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class WeakPointStore:
    """轻量级本地 SQLite 存储。

兼容升级：若旧版本表缺少 user_id 列（单机版遗留数据），会自动重建表；
旧版无主记录会被清除，避免历史数据混入任何学生账号。
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else get_user_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS weak_points (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    course_key TEXT NOT NULL,
                    knowledge_point TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    suggestion TEXT DEFAULT '',
                    related_question TEXT DEFAULT '',
                    wrong_count INTEGER NOT NULL DEFAULT 1,
                    mastered INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_wrong_at TEXT NOT NULL,
                    mastered_at TEXT,
                    UNIQUE(user_id, course_key, knowledge_point)
                );
                CREATE TABLE IF NOT EXISTS exam_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    course_key TEXT NOT NULL,
                    exam_title TEXT NOT NULL,
                    total_questions INTEGER NOT NULL,
                    correct_count INTEGER NOT NULL,
                    score REAL NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS exam_drafts (
                    user_id INTEGER PRIMARY KEY,
                    course_key TEXT NOT NULL,
                    exam_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._migrate_legacy_tables(connection)
            # 连续答对计数器：达到阈值自动标记已掌握（老库幂等加列）
            self._ensure_column(
                connection, "weak_points", "correct_streak", "INTEGER NOT NULL DEFAULT 0"
            )
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_weak_user_course
                    ON weak_points(user_id, course_key, mastered);
                CREATE INDEX IF NOT EXISTS idx_attempt_user_course
                    ON exam_attempts(user_id, course_key, finished_at);
                """
            )

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
        try:
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.OperationalError:
            return set()
        return {row[1] for row in rows}

    @classmethod
    def _ensure_column(
        cls, connection: sqlite3.Connection, table: str, name: str, ddl: str
    ) -> None:
        """幂等加列：列不存在时执行 ALTER TABLE（SQLite 不支持 IF NOT EXISTS）。"""
        if name not in cls._table_columns(connection, table):
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    @classmethod
    def _migrate_legacy_tables(cls, connection: sqlite3.Connection) -> None:
        """把单机旧表升级为带 user_id 的多用户表。

        旧表缺少 user_id 列，SQLite 无法修改唯一约束，因此重建表。
        旧版单机记录没有归属账号，无法安全映射到具体学生，按产品要求
        直接清除，绝不写入任何学生账号（user_id=0 恒为空）。
        """
        weak_columns = cls._table_columns(connection, "weak_points")
        if "user_id" not in weak_columns:
            connection.executescript(
                """
                ALTER TABLE weak_points RENAME TO weak_points_legacy;
                CREATE TABLE weak_points (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    course_key TEXT NOT NULL,
                    knowledge_point TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    suggestion TEXT DEFAULT '',
                    related_question TEXT DEFAULT '',
                    wrong_count INTEGER NOT NULL DEFAULT 1,
                    mastered INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_wrong_at TEXT NOT NULL,
                    mastered_at TEXT,
                    UNIQUE(user_id, course_key, knowledge_point)
                );
                DROP TABLE weak_points_legacy;
                """
            )

        attempt_columns = cls._table_columns(connection, "exam_attempts")
        if "user_id" not in attempt_columns:
            connection.executescript(
                """
                ALTER TABLE exam_attempts RENAME TO exam_attempts_legacy;
                CREATE TABLE exam_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    course_key TEXT NOT NULL,
                    exam_title TEXT NOT NULL,
                    total_questions INTEGER NOT NULL,
                    correct_count INTEGER NOT NULL,
                    score REAL NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '[]'
                );
                DROP TABLE exam_attempts_legacy;
                """
            )

    # ------------------------------------------------------------- 弱项 CRUD

    def upsert_weak_point(
        self,
        user_id: int,
        course_key: str,
        knowledge_point: str,
        *,
        reason: str = "",
        suggestion: str = "",
        related_question: str = "",
    ) -> int:
        """把知识点记为未掌握；已存在则累计错误次数并重新激活。"""
        name = (knowledge_point or "").strip()
        if not name:
            return 0
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO weak_points (
                    user_id, course_key, knowledge_point, reason, suggestion,
                    related_question, wrong_count, mastered,
                    first_seen_at, last_wrong_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                ON CONFLICT(user_id, course_key, knowledge_point) DO UPDATE SET
                    wrong_count = wrong_count + 1,
                    reason = CASE WHEN excluded.reason <> '' THEN excluded.reason
                                  ELSE weak_points.reason END,
                    suggestion = CASE WHEN excluded.suggestion <> ''
                                  THEN excluded.suggestion
                                  ELSE weak_points.suggestion END,
                    related_question = CASE WHEN excluded.related_question <> ''
                                  THEN excluded.related_question
                                  ELSE weak_points.related_question END,
                    mastered = 0,
                    mastered_at = NULL,
                    last_wrong_at = excluded.last_wrong_at
                """,
                (
                    int(user_id),
                    course_key,
                    name,
                    reason,
                    suggestion,
                    related_question,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM weak_points WHERE user_id=? AND course_key=? "
                "AND knowledge_point=?",
                (int(user_id), course_key, name),
            ).fetchone()
            return int(row["id"]) if row else 0

    def add_weak_points(
        self,
        user_id: int,
        course_key: str,
        points: list[dict[str, Any]],
        *,
        related_question: str = "",
    ) -> int:
        saved = 0
        for point in points or []:
            name = point.get("name") or point.get("knowledge_point")
            if not name:
                continue
            saved += (
                self.upsert_weak_point(
                    user_id,
                    course_key,
                    str(name),
                    reason=point.get("reason", ""),
                    suggestion=point.get("suggestion", ""),
                    related_question=related_question or point.get("related_question", ""),
                )
                > 0
            )
        return saved

    def list_weak_points(
        self,
        user_id: int,
        course_key: str,
        *,
        mastered: int | None = None,
        search: str = "",
    ) -> list[dict[str, Any]]:
        where = ["user_id=?", "course_key=?"]
        params: list[Any] = [int(user_id), course_key]
        if mastered is not None:
            where.append("mastered=?")
            params.append(int(mastered))
        if search.strip():
            where.append("(knowledge_point LIKE ? OR reason LIKE ?)")
            keyword = f"%{search.strip()}%"
            params.extend([keyword, keyword])
        sql = (
            "SELECT * FROM weak_points WHERE "
            + " AND ".join(where)
            + " ORDER BY mastered ASC, wrong_count DESC, last_wrong_at DESC"
        )
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_weak_point(
        self, user_id: int, course_key: str, knowledge_point: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM weak_points WHERE user_id=? AND course_key=? "
                "AND knowledge_point=?",
                (int(user_id), course_key, knowledge_point),
            ).fetchone()
        return dict(row) if row else None

    def record_review_result(
        self,
        user_id: int,
        course_key: str,
        knowledge_point: str,
        correct: bool,
        *,
        reason: str = "",
        related_question: str = "",
    ) -> dict[str, Any] | None:
        """记录一次巩固练习结果，实现“答对自动移除”闭环。

        答对：correct_streak + 1；连续答对达到阈值（.env WEAK_POINT_MASTERY_STREAK，
        默认 2）时自动标记已掌握。答错：streak 清零、wrong_count + 1、重新激活
        （已掌握的点再次答错会自动回到待巩固清单）。
        答对且无对应记录时不新建记录（返回 None）。
        """
        from cpa_tutor.config import mastery_streak_threshold

        name = (knowledge_point or "").strip()
        if not name:
            return None
        threshold = mastery_streak_threshold()
        now = _now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM weak_points WHERE user_id=? AND course_key=? "
                "AND knowledge_point=?",
                (int(user_id), course_key, name),
            ).fetchone()
            if row is None:
                if correct:
                    return None
                connection.execute(
                    """
                    INSERT INTO weak_points (
                        user_id, course_key, knowledge_point, reason, suggestion,
                        related_question, wrong_count, mastered,
                        first_seen_at, last_wrong_at
                    ) VALUES (?, ?, ?, ?, '', ?, 1, 0, ?, ?)
                    """,
                    (
                        int(user_id),
                        course_key,
                        name,
                        reason or "巩固练习答错，说明该知识点仍需加强。",
                        related_question,
                        now,
                        now,
                    ),
                )
                inserted = connection.execute(
                    "SELECT * FROM weak_points WHERE user_id=? AND course_key=? "
                    "AND knowledge_point=?",
                    (int(user_id), course_key, name),
                ).fetchone()
                return dict(inserted) if inserted else None
            if correct:
                streak = int(row["correct_streak"] or 0) + 1
                if streak >= threshold:
                    connection.execute(
                        "UPDATE weak_points SET correct_streak=?, mastered=1, "
                        "mastered_at=? WHERE id=?",
                        (streak, now, row["id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE weak_points SET correct_streak=? WHERE id=?",
                        (streak, row["id"]),
                    )
            else:
                connection.execute(
                    """
                    UPDATE weak_points SET correct_streak=0, wrong_count=wrong_count+1,
                        mastered=0, mastered_at=NULL, last_wrong_at=?
                    WHERE id=?
                    """,
                    (now, row["id"]),
                )
            updated = connection.execute(
                "SELECT * FROM weak_points WHERE id=?", (row["id"],)
            ).fetchone()
            return dict(updated) if updated else None

    def mark_mastered(self, user_id: int, weak_point_id: int, mastered: bool = True) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE weak_points SET mastered=?, mastered_at=? "
                "WHERE id=? AND user_id=?",
                (1 if mastered else 0, _now() if mastered else None, weak_point_id, int(user_id)),
            )

    def update_note(self, user_id: int, weak_point_id: int, note: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE weak_points SET suggestion=? WHERE id=? AND user_id=?",
                (note, weak_point_id, int(user_id)),
            )

    def delete_weak_point(self, user_id: int, weak_point_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM weak_points WHERE id=? AND user_id=?",
                (weak_point_id, int(user_id)),
            )

    def stats(self, user_id: int, course_key: str | None = None) -> dict[str, int]:
        sql = "SELECT mastered, COUNT(*) AS count FROM weak_points WHERE user_id=?"
        params: list[Any] = [int(user_id)]
        if course_key:
            sql += " AND course_key=?"
            params.append(course_key)
        sql += " GROUP BY mastered"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        result = {"active": 0, "mastered": 0}
        for row in rows:
            key = "mastered" if row["mastered"] else "active"
            result[key] = int(row["count"])
        return result

    def focus_recommendations(
        self, user_id: int, course_key: str, limit: int = 8
    ) -> list[dict[str, Any]]:
        """按出错次数优先返回待巩固知识点，供个性化组卷与首页推荐。"""
        return self.list_weak_points(user_id, course_key, mastered=0)[: max(1, limit)]

    # ------------------------------------------------------------- 考试记录

    def recent_exams(
        self, user_id: int, course_key: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, course_key, exam_title, total_questions, correct_count,
                       score, started_at, finished_at
                FROM exam_attempts
                WHERE user_id=? AND course_key=?
                ORDER BY finished_at DESC
                LIMIT ?
                """,
                (int(user_id), course_key, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def exam_history(
        self, user_id: int, course_key: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """完整考试历史（含每题明细），供学习画像与错题本使用。"""
        where = ["user_id=?"]
        params: list[Any] = [int(user_id)]
        if course_key:
            where.append("course_key=?")
            params.append(course_key)
        sql = (
            "SELECT * FROM exam_attempts WHERE "
            + " AND ".join(where)
            + " ORDER BY finished_at DESC LIMIT ?"
        )
        params.append(int(limit))
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json") or "[]")
            except (TypeError, ValueError):
                item["details"] = []
            result.append(item)
        return result

    def record_exam_attempt(
        self,
        user_id: int,
        course_key: str,
        exam_title: str,
        total_questions: int,
        correct_count: int,
        score: float,
        details: list[dict[str, Any]],
        started_at: str,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO exam_attempts (
                    user_id, course_key, exam_title, total_questions, correct_count,
                    score, started_at, finished_at, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(user_id),
                    course_key,
                    exam_title,
                    total_questions,
                    correct_count,
                    round(float(score), 2),
                    started_at,
                    _now(),
                    json.dumps(details, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    # ------------------------------------------------------------- 续考草稿

    def save_exam_draft(self, user_id: int, course_key: str, exam: dict[str, Any]) -> None:
        """保存进行中的考试草稿（每个学生一份），中途退出后下次登录可继续。"""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO exam_drafts (user_id, course_key, exam_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    course_key = excluded.course_key,
                    exam_json = excluded.exam_json,
                    updated_at = excluded.updated_at
                """,
                (
                    int(user_id),
                    course_key,
                    json.dumps(exam, ensure_ascii=False),
                    _now(),
                ),
            )

    def load_exam_draft(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT exam_json FROM exam_drafts WHERE user_id=?", (int(user_id),)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["exam_json"])
        except (TypeError, ValueError):
            return None

    def delete_exam_draft(self, user_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM exam_drafts WHERE user_id=?", (int(user_id),)
            )

    # ------------------------------------------------------------- 错题本

    def wrong_questions(
        self,
        user_id: int,
        course_key: str | None = None,
        *,
        limit_attempts: int = 60,
    ) -> list[dict[str, Any]]:
        """从最近考试中抽取答错的题目明细（含题干、选项、解析与考试信息）。"""
        attempts = self.exam_history(
            user_id, course_key=course_key, limit=limit_attempts
        )
        wrong: list[dict[str, Any]] = []
        seen: set[str] = set()
        for attempt in attempts:
            for item in attempt.get("details") or []:
                if item.get("is_correct"):
                    continue
                question = item.get("question") or {}
                key = question.get("stem") or item.get("student_answer") or str(item)
                dedupe = str(key)[:160]
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                wrong.append(
                    {
                        "attempt_id": attempt.get("id"),
                        "finished_at": attempt.get("finished_at", ""),
                        "exam_title": attempt.get("exam_title", ""),
                        "score": attempt.get("score"),
                        "question": question,
                        "student_answer": item.get("student_answer"),
                        "student_answer_display": item.get("student_answer_display"),
                        "knowledge_points": question.get("knowledge_points", [])
                        or item.get("knowledge_points", []),
                    }
                )
        return wrong

    def recent_question_stems(
        self, user_id: int, course_key: str, limit: int = 150
    ) -> list[str]:
        """该学生最近做过的题干（前 160 字），用于组卷时避免重复出题。"""
        attempts = self.exam_history(user_id, course_key=course_key, limit=20)
        stems: list[str] = []
        for attempt in attempts:
            for item in attempt.get("details") or []:
                question = item.get("question") or {}
                stem = str(question.get("stem") or "").strip()
                if stem:
                    stems.append(stem[:160])
        return stems[-limit:]

    def course_progress(self, user_id: int) -> dict[str, dict[str, Any]]:
        """按科目汇总考试次数、均分、最近成绩与弱项数量，供首页画像。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT course_key, COUNT(*) AS exam_count, AVG(score) AS avg_score,
                       MAX(score) AS best_score, MAX(finished_at) AS last_finished_at
                FROM exam_attempts WHERE user_id=?
                GROUP BY course_key
                """,
                (int(user_id),),
            ).fetchall()
            weak_rows = connection.execute(
                """
                SELECT course_key,
                       SUM(CASE WHEN mastered=0 THEN 1 ELSE 0 END) AS active,
                       SUM(CASE WHEN mastered=1 THEN 1 ELSE 0 END) AS mastered
                FROM weak_points WHERE user_id=? GROUP BY course_key
                """,
                (int(user_id),),
            ).fetchall()
        weak = {row["course_key"]: dict(row) for row in weak_rows}
        progress: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["avg_score"] = round(float(item["avg_score"] or 0), 1)
            item["best_score"] = round(float(item["best_score"] or 0), 1)
            weak_item = weak.get(item["course_key"], {})
            item["active_weak"] = int(weak_item.get("active") or 0)
            item["mastered_weak"] = int(weak_item.get("mastered") or 0)
            progress[item["course_key"]] = item
        for key, item in weak.items():
            if key not in progress:
                progress[key] = {
                    "course_key": key,
                    "exam_count": 0,
                    "avg_score": 0.0,
                    "best_score": 0.0,
                    "last_finished_at": None,
                    "active_weak": int(item.get("active") or 0),
                    "mastered_weak": int(item.get("mastered") or 0),
                }
        return progress
