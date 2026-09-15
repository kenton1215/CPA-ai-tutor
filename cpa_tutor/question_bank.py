"""真题题库与 AI 生成题缓存（SQLite 持久化）。

question_bank：从十年真题 PDF 解析入库的结构化题目（开发者页面批量解析 +
人工校对）。组卷时优先从这里按题型/章节选题，实现"原考题库选择组合"。
question_cache：AI 补充生成的题目缓存，跨考试复用，减少重复调用模型。

两张表都按 course_key 隔离；题干前 160 字作为去重键。
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


def _dump_answer(answer: Any) -> str:
    """多选答案为字母数组，序列化为 JSON；其余存原文。"""
    if isinstance(answer, (list, tuple)):
        return json.dumps(list(answer), ensure_ascii=False)
    return str(answer)


def _load_answer(raw: Any) -> Any:
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _stem_key(stem: Any) -> str:
    return str(stem or "").strip()[:160]


class QuestionBank:
    """题库与生成缓存的存储层（与账号/学习记录共用 data/cpa_user.db）。"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else get_user_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS question_bank (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_key TEXT NOT NULL,
                    type TEXT NOT NULL,
                    stem TEXT NOT NULL,
                    options_json TEXT NOT NULL DEFAULT '{}',
                    answer TEXT NOT NULL,
                    analysis TEXT NOT NULL DEFAULT '',
                    knowledge_points_json TEXT NOT NULL DEFAULT '[]',
                    chapter TEXT NOT NULL DEFAULT '',
                    year INTEGER,
                    source TEXT NOT NULL DEFAULT '',
                    score REAL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_bank_course_type
                    ON question_bank(course_key, type, active);
                CREATE TABLE IF NOT EXISTS question_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_key TEXT NOT NULL,
                    type TEXT NOT NULL,
                    stem TEXT NOT NULL,
                    options_json TEXT NOT NULL DEFAULT '{}',
                    answer TEXT NOT NULL,
                    analysis TEXT NOT NULL DEFAULT '',
                    knowledge_points_json TEXT NOT NULL DEFAULT '[]',
                    chapter TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cache_course_type
                    ON question_cache(course_key, type);
                """
            )

    @staticmethod
    def _row_to_question(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "id": row["id"],
            "type": row["type"],
            "stem": row["stem"],
            "options": json.loads(row["options_json"] or "{}"),
            "answer": _load_answer(row["answer"]),
            "analysis": row["analysis"],
            "knowledge_points": json.loads(row["knowledge_points_json"] or "[]"),
            "chapter": row["chapter"],
            "year": row["year"] if "year" in keys else None,
            "source": row["source"] if "source" in keys else "",
            "score": row["score"] if "score" in keys else None,
            "active": row["active"] if "active" in keys else 1,
        }

    # ------------------------------------------------------------- 题库写入

    def insert_questions(
        self,
        course_key: str,
        questions: list[dict[str, Any]],
        *,
        source: str = "",
        year: int | None = None,
    ) -> tuple[int, int]:
        """批量入库（按题干前 160 字去重）。返回 (新增, 重复跳过)。"""
        inserted = skipped = 0
        with self._connect() as connection:
            for question in questions or []:
                stem = _stem_key(question.get("stem"))
                if not stem:
                    continue
                exists = connection.execute(
                    "SELECT 1 FROM question_bank WHERE course_key=? AND substr(stem, 1, 160)=?",
                    (course_key, stem),
                ).fetchone()
                if exists:
                    skipped += 1
                    continue
                connection.execute(
                    """
                    INSERT INTO question_bank (
                        course_key, type, stem, options_json, answer, analysis,
                        knowledge_points_json, chapter, year, source, score,
                        active, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        course_key,
                        str(question.get("type") or ""),
                        str(question.get("stem") or ""),
                        json.dumps(question.get("options") or {}, ensure_ascii=False),
                        _dump_answer(question.get("answer") or ""),
                        str(question.get("analysis") or ""),
                        json.dumps(
                            question.get("knowledge_points") or [], ensure_ascii=False
                        ),
                        str(question.get("chapter") or ""),
                        year,
                        source,
                        question.get("score"),
                        _now(),
                    ),
                )
                inserted += 1
        return inserted, skipped

    # ------------------------------------------------------------- 题库抽取

    def _sample_from(
        self,
        table: str,
        course_key: str,
        qtype: str,
        *,
        chapter: str = "",
        exclude_stems: tuple[str, ...] | list[str] = (),
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [course_key, qtype]
        sql = f"SELECT * FROM {table} WHERE course_key=? AND type=?"
        if table == "question_bank":
            sql += " AND active=1"
        if chapter:
            sql += " AND (chapter LIKE ? OR chapter='')"
            params.append(f"%{chapter}%")
        sql += " ORDER BY RANDOM() LIMIT 60"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        used = {_stem_key(stem) for stem in (exclude_stems or ())}
        result: list[dict[str, Any]] = []
        for row in rows:
            question = self._row_to_question(row)
            if _stem_key(question["stem"]) in used:
                continue
            result.append(question)
            if len(result) >= limit:
                break
        return result

    def sample_questions(
        self,
        course_key: str,
        qtype: str,
        *,
        chapter: str = "",
        exclude_stems: tuple[str, ...] | list[str] = (),
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        """从真题题库随机抽取：优先章节匹配，并排除近期做过的题干。"""
        found = self._sample_from(
            "question_bank", course_key, qtype,
            chapter=chapter, exclude_stems=exclude_stems, limit=limit,
        )
        if not found and chapter:
            found = self._sample_from(
                "question_bank", course_key, qtype,
                exclude_stems=exclude_stems, limit=limit,
            )
        return found

    def sample_one(
        self,
        course_key: str,
        qtype: str,
        *,
        chapter: str = "",
        exclude_stems: tuple[str, ...] | list[str] = (),
    ) -> dict[str, Any] | None:
        found = self.sample_questions(
            course_key, qtype, chapter=chapter, exclude_stems=exclude_stems, limit=1
        )
        return found[0] if found else None

    def sample_cache_questions(
        self,
        course_key: str,
        qtype: str,
        *,
        chapter: str = "",
        exclude_stems: tuple[str, ...] | list[str] = (),
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        """从 AI 生成题缓存抽取（先题库后缓存，缓存只是补充）。"""
        return self._sample_from(
            "question_cache", course_key, qtype,
            chapter=chapter, exclude_stems=exclude_stems, limit=limit,
        )

    # ------------------------------------------------------------- 生成缓存写入

    def insert_cache_questions(
        self, course_key: str, questions: list[dict[str, Any]]
    ) -> int:
        inserted = 0
        with self._connect() as connection:
            for question in questions or []:
                stem = _stem_key(question.get("stem"))
                if not stem:
                    continue
                exists = connection.execute(
                    "SELECT 1 FROM question_cache WHERE course_key=? AND substr(stem, 1, 160)=?",
                    (course_key, stem),
                ).fetchone()
                if exists:
                    continue
                connection.execute(
                    """
                    INSERT INTO question_cache (
                        course_key, type, stem, options_json, answer, analysis,
                        knowledge_points_json, chapter, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        course_key,
                        str(question.get("type") or ""),
                        str(question.get("stem") or ""),
                        json.dumps(question.get("options") or {}, ensure_ascii=False),
                        _dump_answer(question.get("answer") or ""),
                        str(question.get("analysis") or ""),
                        json.dumps(
                            question.get("knowledge_points") or [], ensure_ascii=False
                        ),
                        str(question.get("chapter") or ""),
                        _now(),
                    ),
                )
                inserted += 1
        return inserted

    # ------------------------------------------------------------- 统计与管理

    def bank_stats(self, course_key: str | None = None) -> dict[str, Any]:
        where = "WHERE active=1" if course_key is None else "WHERE active=1 AND course_key=?"
        params: tuple = () if course_key is None else (course_key,)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT type, COUNT(*) AS count FROM question_bank {where} "
                "GROUP BY type",
                params,
            ).fetchall()
        by_type: dict[str, int] = {}
        total = 0
        for row in rows:
            by_type[str(row["type"])] = int(row["count"])
            total += int(row["count"])
        return {"total": total, "by_type": by_type}

    def list_bank(
        self,
        course_key: str,
        *,
        qtype: str | None = None,
        active: int | None = None,
        search: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where = ["course_key=?"]
        params: list[Any] = [course_key]
        if qtype:
            where.append("type=?")
            params.append(qtype)
        if active is not None:
            where.append("active=?")
            params.append(int(active))
        if search.strip():
            where.append("(stem LIKE ? OR chapter LIKE ? OR knowledge_points_json LIKE ?)")
            keyword = f"%{search.strip()}%"
            params.extend([keyword, keyword, keyword])
        sql = (
            "SELECT * FROM question_bank WHERE "
            + " AND ".join(where)
            + " ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        params.extend([int(limit), int(offset)])
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_question(row) for row in rows]

    def get_question(self, question_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM question_bank WHERE id=?", (int(question_id),)
            ).fetchone()
        return self._row_to_question(row) if row else None

    def set_active(self, question_id: int, active: bool = True) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE question_bank SET active=? WHERE id=?",
                (1 if active else 0, int(question_id)),
            )

    def delete_question(self, question_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM question_bank WHERE id=?", (int(question_id),)
            )

    def update_question(self, question_id: int, fields: dict[str, Any]) -> None:
        """校对修正：仅允许更新内容类字段。"""
        allowed = {
            "type": str,
            "stem": str,
            "options": "json",
            "answer": "dump",
            "analysis": str,
            "knowledge_points": "json",
            "chapter": str,
            "year": int,
            "source": str,
        }
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=?")
            if allowed[key] == "json":
                params.append(json.dumps(value or [], ensure_ascii=False))
            elif allowed[key] == "dump":
                params.append(_dump_answer(value))
            elif allowed[key] == int:
                params.append(int(value) if value not in (None, "") else None)
            else:
                params.append(str(value or ""))
        if not sets:
            return
        params.append(int(question_id))
        with self._connect() as connection:
            connection.execute(
                f"UPDATE question_bank SET {', '.join(sets)} WHERE id=?", params
            )
