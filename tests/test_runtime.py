"""核心业务逻辑与页面冒烟测试（不需要真实模型/网络）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from cpa_tutor.config import llm_config
from cpa_tutor.accounts import AccountStore
from cpa_tutor.courses import COURSES, match_course_key
from cpa_tutor.exam_engine import (
    MULTIPLE,
    SINGLE,
    SUBJECTIVE,
    evaluate_choice_answer,
    normalize_question,
    sample_exam_plan,
)
from cpa_tutor.llm import extract_json
from cpa_tutor.quota import quota_exhausted, quota_status
from cpa_tutor.text_pipeline import looks_readable, split_pages_into_chunks
from cpa_tutor.weak_points import WeakPointStore


def test_course_registry_has_six_cpa_courses():
    assert set(COURSES) == {
        "accounting",
        "audit",
        "financial_management",
        "tax",
        "economic_law",
        "strategy",
    }


def test_course_filename_mapping():
    assert match_course_key("2026年CPA考试辅导教材-会计.pdf") == "accounting"
    assert match_course_key("《5年真题》-注会-公司战略与风险管理-题干.pdf") == "strategy"


def test_extract_year_from_filename():
    from cpa_tutor.courses import extract_year

    assert extract_year("2016年真题-会计.pdf") == 2016
    assert extract_year("《5年真题》-注会-会计-题干.pdf") is None
    assert extract_year("2026年CPA考试辅导教材-会计.pdf") == 2026
    assert extract_year("1999年真题.pdf") is None


def test_account_register_authenticate_isolation(tmp_path):
    store = AccountStore(tmp_path / "users.db")
    alice = store.register("13800000001", "secret123", "Alice", phone="13800000001")
    bob = store.register("13900000002", "secret456", "Bob", phone="13900000002")
    assert alice["id"] != bob["id"]
    assert alice["wechat_openid"] is None
    assert store.authenticate("13800000001", "secret123")["id"] == alice["id"]
    assert store.authenticate("13800000001", "bad") is None
    assert "password_hash" not in alice
    # 不同学生弱项互不可见
    ws = WeakPointStore(tmp_path / "users.db")
    ws.upsert_weak_point(alice["id"], "accounting", "长期股权投资", reason="A 的错因")
    ws.upsert_weak_point(bob["id"], "accounting", "长期股权投资", reason="B 的错因")
    rows_a = ws.list_weak_points(alice["id"], "accounting")
    rows_b = ws.list_weak_points(bob["id"], "accounting")
    assert len(rows_a) == len(rows_b) == 1
    assert rows_a[0]["reason"] == "A 的错因"
    assert rows_b[0]["reason"] == "B 的错因"


def test_developer_role_follows_env_whitelist(tmp_path, monkeypatch):
    db = tmp_path / "roles.db"
    dev_phone = "13800000001"
    monkeypatch.setenv("APP_DEVELOPER_ACCOUNTS", dev_phone)
    store = AccountStore(db)
    developer = store.register(dev_phone, "secret123", "开发者", phone=dev_phone)
    student = store.register("13900000002", "secret456", "学生", phone="13900000002")
    assert developer["role"] == "developer"
    assert student["role"] == "student"

    # 白名单移除后，历史 developer 会被自动降级，避免权限残留
    monkeypatch.delenv("APP_DEVELOPER_ACCOUNTS", raising=False)
    AccountStore(db)
    assert store.get_user(developer["id"])["role"] == "student"
    assert store.get_user(student["id"])["role"] == "student"


def test_llm_config_resolves_without_crash():
    cfg = llm_config({})
    assert isinstance(cfg, dict)
    assert "available" in cfg
    assert cfg.get("api_key_masked") is not None


def test_extract_json_with_code_fence():
    value = extract_json('```json\n{"name": "测试", "items": [1, 2]}\n```')
    assert value["name"] == "测试"


def test_extract_json_rejects_invalid():
    try:
        extract_json("这不是 JSON")
        raise AssertionError("应抛出解析异常")
    except Exception:
        pass


def test_text_readability_heuristic():
    assert looks_readable("第一章 总论，本章主要介绍企业会计准则的基本框架与会计基本假设。")
    assert not looks_readable("g\x00e°\x8dDe\x99\x00 |¾Q")


def test_chunking_keeps_page_and_chapter_metadata():
    pages = [
        {"page": 1, "text": "第一章 概述\n这是第一段可读内容。", "readable": True},
        {"page": 2, "text": "第二章 进阶\n这是第二段可读内容，用于验证跨页切分。", "readable": True},
    ]
    chunks = split_pages_into_chunks(pages, "教材-会计.pdf")
    assert chunks
    assert all(chunk["source"] == "教材-会计.pdf" for chunk in chunks)
    assert all("page_start" in chunk for chunk in chunks)
    # 年份元数据：从文件名识别
    year_chunks = split_pages_into_chunks(pages, "2016年真题-会计.pdf")
    assert all(chunk.get("year") == 2016 for chunk in year_chunks)


def test_weak_point_store_crud(tmp_path):
    account = AccountStore(tmp_path / "user.db").register("13800000001", "secret123", "A", phone="13800000001")
    user_id = account["id"]
    store = WeakPointStore(tmp_path / "user.db")
    store.upsert_weak_point(user_id, "accounting", "长期股权投资成本法", reason="计算错误")
    store.upsert_weak_point(user_id, "accounting", "长期股权投资成本法", reason="再次算错")
    store.upsert_weak_point(user_id, "tax", "增值税进项税额抵扣")
    rows = store.list_weak_points(user_id, "accounting", mastered=0)
    assert len(rows) == 1
    assert rows[0]["wrong_count"] == 2
    row_id = rows[0]["id"]
    store.mark_mastered(user_id, row_id, True)
    assert store.list_weak_points(user_id, "accounting", mastered=1)
    assert store.list_weak_points(user_id, "accounting", mastered=0) == []


def test_exam_attempt_record(tmp_path):
    account = AccountStore(tmp_path / "user.db").register("13800000001", "secret123", "A", phone="13800000001")
    user_id = account["id"]
    store = WeakPointStore(tmp_path / "user.db")
    store.record_exam_attempt(
        user_id,
        "accounting",
        "测试卷",
        10,
        7,
        70.0,
        [{"index": 1, "is_correct": True}],
        "2026-09-03T10:00:00",
    )
    attempts = store.recent_exams(user_id, "accounting")
    assert len(attempts) == 1
    assert attempts[0]["score"] == 70.0


def test_exam_plan_respects_limits():
    plan = sample_exam_plan(12, chapters=["第一章", "第二章"], seed=7)
    assert len(plan) == 12
    assert all(item["index"] == idx for idx, item in enumerate(plan, start=1))
    types = {item["type"] for item in plan}
    assert SINGLE in types and MULTIPLE in types


def test_official_structures_total_100_and_cover_all_courses():
    from cpa_tutor.exam_structure import CHOICE_TYPES, EXAM_STRUCTURES

    assert set(EXAM_STRUCTURES) == set(COURSES)
    for structure in EXAM_STRUCTURES.values():
        assert abs(sum(g["total_score"] for g in structure) - 100) < 1e-6
        assert all(g["count"] >= 1 for g in structure)
        # 每科都有客观题与主观题，且主观题在前两栏之后
        types = [g["type"] for g in structure]
        assert types[0] in CHOICE_TYPES and types[1] in CHOICE_TYPES
        assert len(types) >= 3


def test_build_exam_plan_full_half_mini_scales():
    from cpa_tutor.exam_structure import build_exam_plan, plan_total_score

    full = build_exam_plan("accounting", "full", seed=1)
    assert len(full) == 13 + 12 + 2 + 2
    assert [item["type"] for item in full] == [
        "single",
    ] * 13 + ["multiple"] * 12 + ["calc"] * 2 + ["comp"] * 2
    assert all(item["score"] and item["score"] > 0 for item in full)
    assert abs(plan_total_score(full) - 100) < 1.5

    half = build_exam_plan("accounting", "half", seed=1)
    assert len(half) < len(full)
    assert plan_total_score(half) < 60

    mini = build_exam_plan("accounting", "mini", seed=1)
    assert len(mini) < len(half)
    # 迷你卷同样覆盖官方全部题型分区
    assert {item["type"] for item in mini} == {"single", "multiple", "calc", "comp"}


def test_build_exam_plan_uses_chapters_and_weak_point_strategy():
    from cpa_tutor.exam_structure import build_exam_plan

    plan = build_exam_plan(
        "tax", "mini", chapters=["增值税进项税额", "企业所得税扣除"], seed=3
    )
    assert plan
    assert all(item["chapter"] in {"增值税进项税额", "企业所得税扣除"} for item in plan)
    # 章节轮转：两个考点都会被考到
    assert {item["chapter"] for item in plan} == {"增值税进项税额", "企业所得税扣除"}


def test_plan_summary_includes_scores():
    from cpa_tutor.exam_engine import plan_summary
    from cpa_tutor.exam_structure import build_exam_plan

    plan = build_exam_plan("accounting", "mini", seed=1)
    text = plan_summary(plan)
    assert "共 " in text and "满分" in text and "分" in text
    assert "单项选择题" in text and "计算分析题" in text and "综合题" in text


def test_new_question_types_normalize_and_label():
    from cpa_tutor.exam_engine import CALC, COMP, is_subjective_type, normalize_question

    raw = {
        "type": "calc",
        "stem": "计算甲公司 2026 年应确认的投资收益金额。",
        "answer": "投资收益 = 初始投资成本 × 适用税率，代入数据计算可得 120 万元。",
        "analysis": "按权益法核算的长期股权投资，应确认被投资单位实现的净损益份额。",
        "knowledge_points": ["长期股权投资权益法"],
    }
    question = normalize_question(raw, CALC, index=1)
    assert question["type"] == CALC
    assert is_subjective_type(CALC) and is_subjective_type(COMP)
    assert not is_subjective_type("single")


def test_question_normalization_and_evaluation():
    raw = {
        "type": "single",
        "chapter": "第三章 存货",
        "stem": "下列各项中，不属于存货的是？",
        "options": {"A": "原材料", "B": "库存商品", "C": "固定资产", "D": "在产品"},
        "answer": "C",
        "analysis": "固定资产属于长期资产，不属于存货。",
        "knowledge_points": ["存货的确认"],
    }
    question = normalize_question(raw, SINGLE, index=1, chapter="第三章 存货")
    correct, display = evaluate_choice_answer(question, "C")
    assert correct is True and "固定资产" in display
    wrong, _ = evaluate_choice_answer(question, "A")
    assert wrong is False


def test_keyword_retriever_matches_chinese_query():
    from cpa_tutor.knowledge import KeywordRetriever

    chunks = [
        {
            "text": "根据合同法律制度的规定，当事人可以约定一方向对方给付定金作为债权的担保。",
            "source": "教材",
            "kind": "教材",
            "chapter": "第四章 合同法律制度",
            "page_start": 1,
            "page_end": 1,
        }
    ]
    retriever = KeywordRetriever(chunks)
    hits = retriever.search("合同 定金 担保", k=1)
    assert hits and hits[0]["text"].startswith("根据合同")


def test_wrong_questions_scoped_by_user_and_snapshot(tmp_path):
    acct = AccountStore(tmp_path / "user.db")
    alice = acct.register("13800000001", "secret123", "Alice", phone="13800000001")
    bob = acct.register("13900000002", "secret456", "Bob", phone="13900000002")
    store = WeakPointStore(tmp_path / "user.db")
    question = {
        "index": 1,
        "type": "single",
        "stem": "下列关于存货计量的说法正确的是？",
        "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
        "answer": "C",
        "analysis": "解析内容",
        "knowledge_points": ["存货计量"],
    }
    store.record_exam_attempt(
        alice["id"], "accounting", "测试卷", 1, 0, 0.0,
        [{"question": question, "is_correct": False, "student_answer": "A"}],
        "2026-09-04T10:00:00",
    )
    store.record_exam_attempt(
        bob["id"], "accounting", "测试卷", 1, 1, 100.0,
        [{"question": question, "is_correct": True, "student_answer": "C"}],
        "2026-09-04T10:01:00",
    )
    alice_wrong = store.wrong_questions(alice["id"], "accounting")
    bob_wrong = store.wrong_questions(bob["id"], "accounting")
    assert len(alice_wrong) == 1
    assert alice_wrong[0]["question"]["stem"].startswith("下列关于存货")
    assert bob_wrong == []


def test_subjective_question_normalization():
    raw = {
        "type": "subjective",
        "stem": "计算甲公司应当确认的长期股权投资初始投资成本。",
        "answer": "初始投资成本 = 支付对价公允价值 + 相关税费。",
        "analysis": "按准则计量。",
        "knowledge_points": ["长期股权投资初始计量"],
    }
    question = normalize_question(raw, SUBJECTIVE, index=1)
    assert question["type"] == SUBJECTIVE


def test_database_schema_created(tmp_path):
    store = WeakPointStore(tmp_path / "schema.db")
    AccountStore(tmp_path / "schema.db")
    connection = sqlite3.connect(tmp_path / "schema.db")
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"users", "weak_points", "exam_attempts", "exam_drafts"} <= tables


def test_exam_draft_save_load_delete_roundtrip(tmp_path):
    store = WeakPointStore(tmp_path / "draft.db")
    exam = {
        "course_key": "accounting",
        "title": "测试卷",
        "plan": [{"index": 1, "type": "single", "chapter": "", "score": 2.0}],
        "questions": {"1": {"index": 1, "stem": "题目", "type": "single"}},
        "current_index": 1,
        "results": [],
        "started_at": "2026-09-15T10:00:00",
        "finished": False,
    }
    store.save_exam_draft(7, "accounting", exam)
    loaded = store.load_exam_draft(7)
    assert loaded == exam and loaded["finished"] is False
    # 同一学生再次保存覆盖旧草稿
    store.save_exam_draft(7, "accounting", dict(exam, current_index=2))
    assert store.load_exam_draft(7)["current_index"] == 2
    # 学生之间互相隔离
    assert store.load_exam_draft(8) is None
    store.delete_exam_draft(7)
    assert store.load_exam_draft(7) is None


def test_home_page_shows_resume_banner_with_draft(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("CPA_USER_DB_PATH", str(tmp_path / "home_draft.db"))
    acct = AccountStore()
    user = acct.register("13900000002", "secret123", "学生", phone="13900000002")
    WeakPointStore().save_exam_draft(
        user["id"],
        "accounting",
        {
            "course_key": "accounting",
            "title": "《会计》AI 模拟试卷",
            "plan": [{"index": 1, "type": "single", "score": 2.0}],
            "questions": {},
            "current_index": 1,
            "results": [],
            "started_at": "2026-09-15T10:00:00",
            "finished": False,
        },
    )
    # 通过 app.py 入口运行（home.py 的 st.page_link 需要导航注册上下文）
    at = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=20)
    at.session_state["auth_user"] = user
    at.session_state["selected_course_key"] = "accounting"
    at.run()
    assert not at.exception, [exc.value for exc in at.exception]
    assert any("未完成的模拟考试" in str(m.value) for m in at.markdown)


def test_legacy_schema_migration_clears_unowned_rows(tmp_path):
    """单机旧版数据库（无 user_id）首次打开时自动重建并清除旧无主记录。"""
    db = tmp_path / "legacy.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE weak_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            UNIQUE(course_key, knowledge_point)
        );
        INSERT INTO weak_points (
            course_key, knowledge_point, reason, wrong_count, mastered,
            first_seen_at, last_wrong_at
        ) VALUES ('accounting', '旧版弱项', '历史记录', 3, 0,
                  '2026-01-01T00:00:00', '2026-01-02T00:00:00');
        CREATE TABLE exam_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_key TEXT NOT NULL,
            exam_title TEXT NOT NULL,
            total_questions INTEGER NOT NULL,
            correct_count INTEGER NOT NULL,
            score REAL NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '[]'
        );
        INSERT INTO exam_attempts (
            course_key, exam_title, total_questions, correct_count, score,
            started_at, finished_at
        ) VALUES ('accounting', '旧版模考', 10, 4, 40.0,
                  '2026-01-01T00:00:00', '2026-01-02T00:00:00');
        """
    )
    connection.close()

    WeakPointStore(db)
    check = sqlite3.connect(db)
    weak_cols = {r[1] for r in check.execute("PRAGMA table_info(weak_points)")}
    attempt_cols = {r[1] for r in check.execute("PRAGMA table_info(exam_attempts)")}
    assert "user_id" in weak_cols and "user_id" in attempt_cols
    weak_rows = check.execute("SELECT COUNT(*) FROM weak_points").fetchone()[0]
    attempt_rows = check.execute("SELECT COUNT(*) FROM exam_attempts").fetchone()[0]
    assert weak_rows == 0 and attempt_rows == 0
    check.close()


def test_users_quota_columns_migrate_on_old_db(tmp_path):
    """旧版 users 表首次打开时自动补上免费次数相关列。"""
    db = tmp_path / "old_users.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE users (
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
            last_login_at TEXT
        );
        """
    )
    connection.close()

    store = AccountStore(db)
    user = store.register("13800000001", "secret123", "老库升级", phone="13800000001")
    # 注册即计 1 次登录会话（同一天重复登录不重复扣）
    from datetime import date

    assert user["free_logins_used"] == 1
    assert user["last_login_date"] == date.today().isoformat()
    assert user["paid"] == 0


def test_guest_role_not_touched_by_developer_whitelist(tmp_path, monkeypatch):
    """游客行绝不参与开发者白名单同步（提升/降级都不影响）。"""
    db = tmp_path / "guest_role.db"
    monkeypatch.setenv("APP_DEVELOPER_ACCOUNTS", "guest_demo")
    store = AccountStore(db)
    connection = sqlite3.connect(db)
    connection.execute(
        """
        INSERT INTO users (account, phone, nickname, password_hash, salt, role,
                           active, created_at, updated_at)
        VALUES ('guest_demo', NULL, '游客', '', '', 'guest', 1,
                '2026-09-15T10:00:00', '2026-09-15T10:00:00')
        """
    )
    connection.commit()
    connection.close()

    AccountStore(db)  # 白名单恰好含 guest 行账号：不得提升为 developer
    assert store.get_user(1)["role"] == "guest"
    assert store.list_active_users() == []  # 游客不出现在正式用户列表

    monkeypatch.delenv("APP_DEVELOPER_ACCOUNTS", raising=False)
    AccountStore(db)  # 白名单清空：也不得把 guest 降级为 student
    assert store.get_user(1)["role"] == "guest"


def test_ensure_column_idempotent(tmp_path):
    """_ensure_column 幂等：重复执行不报错、列只存在一次。"""
    store = WeakPointStore(tmp_path / "cols.db")
    with store._connect() as connection:
        store._ensure_column(
            connection, "weak_points", "correct_streak", "INTEGER NOT NULL DEFAULT 0"
        )
        store._ensure_column(
            connection, "weak_points", "correct_streak", "INTEGER NOT NULL DEFAULT 0"
        )
    check = sqlite3.connect(tmp_path / "cols.db")
    cols = {row[1] for row in check.execute("PRAGMA table_info(weak_points)")}
    assert "correct_streak" in cols
    check.close()


def test_quota_same_day_login_does_not_double_consume(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_USER_LOGINS", "10")
    store = AccountStore(tmp_path / "quota.db")
    store.register("13800000001", "secret123", "A", phone="13800000001")
    first = store.authenticate("13800000001", "secret123")
    second = store.authenticate("13800000001", "secret123")
    # 注册计 1 次；同一天两次登录不再重复扣
    assert first["free_logins_used"] == 1
    assert second["free_logins_used"] == 1
    assert not quota_exhausted(second)

    # 跨天登录再扣 1 次
    with sqlite3.connect(tmp_path / "quota.db") as connection:
        connection.execute(
            "UPDATE users SET last_login_date='2000-01-01' WHERE id=?",
            (first["id"],),
        )
    third = store.authenticate("13800000001", "secret123")
    assert third["free_logins_used"] == 2


def test_guest_creation_find_and_quota_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_GUEST_LOGINS", "5")
    store = AccountStore(tmp_path / "guest.db")
    guest = store.create_guest("abc123token", nickname="游客")
    assert guest["role"] == "guest"
    assert guest["free_logins_used"] == 1
    assert store.find_guest("abc123token")["id"] == guest["id"]
    # 游客无密码，不能用账号密码登录
    assert store.authenticate("guest_abc123token", "whatever") is None

    # 用满免费次数后锁定
    with sqlite3.connect(tmp_path / "guest.db") as connection:
        connection.execute(
            "UPDATE users SET free_logins_used=5 WHERE id=?", (guest["id"],)
        )
    fresh = store.get_user(guest["id"])
    assert quota_exhausted(fresh)
    assert "已用 5 / 5 次" in quota_status(fresh)


def test_developer_quota_unlimited(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DEVELOPER_ACCOUNTS", "13800000001")
    monkeypatch.setenv("FREE_USER_LOGINS", "10")
    store = AccountStore(tmp_path / "dev.db")
    developer = store.register("13800000001", "secret123", "Dev", phone="13800000001")
    assert developer["role"] == "developer"
    with sqlite3.connect(tmp_path / "dev.db") as connection:
        connection.execute(
            "UPDATE users SET free_logins_used=99 WHERE id=?", (developer["id"],)
        )
    fresh = store.get_user(developer["id"])
    assert not quota_exhausted(fresh)
    assert quota_status(fresh) == "免费次数：不限次"


def test_guest_data_inherited_on_register(tmp_path):
    store = AccountStore(tmp_path / "inherit.db")
    ws = WeakPointStore(tmp_path / "inherit.db")
    guest = store.create_guest("tok123", nickname="游客")
    ws.upsert_weak_point(guest["id"], "accounting", "长期股权投资", reason="游客错因")

    user = store.register("13800000001", "secret123", "新学员", phone="13800000001")
    moved = store.inherit_guest_data(guest["id"], user["id"])
    assert moved == 1
    assert ws.list_weak_points(guest["id"], "accounting") == []
    rows = ws.list_weak_points(user["id"], "accounting")
    assert len(rows) == 1 and rows[0]["knowledge_point"] == "长期股权投资"
    assert store.get_user(guest["id"]) is None


def test_account_page_unauthenticated_guest_entry(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("CPA_USER_DB_PATH", str(tmp_path / "guest_page.db"))
    at = AppTest.from_file(
        str(PROJECT_ROOT / "app_pages/account.py"), default_timeout=20
    )
    at.run()
    assert not at.exception, [exc.value for exc in at.exception]
    assert at.button(key="guest_entry")
    at.button(key="guest_entry").click().run()
    assert not at.exception, [exc.value for exc in at.exception]
    assert at.session_state["auth_user"]["role"] == "guest"


def test_app_locks_learning_pages_when_quota_exhausted(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("CPA_USER_DB_PATH", str(tmp_path / "lock.db"))
    monkeypatch.setenv("FREE_USER_LOGINS", "10")
    acct = AccountStore()
    user = acct.register("13900000002", "secret123", "学生", phone="13900000002")
    with sqlite3.connect(tmp_path / "lock.db") as connection:
        connection.execute(
            "UPDATE users SET free_logins_used=10 WHERE id=?", (user["id"],)
        )
    at = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=20)
    at.session_state["auth_user"] = acct.get_user(user["id"])
    at.run()
    assert not at.exception, [exc.value for exc in at.exception]
    # 锁定后账号页可见“免费次数已用完”提示
    assert any("次数已用完" in str(t.value) for t in at.error)


def test_question_bank_insert_dedup_and_sample(tmp_path):
    from cpa_tutor.question_bank import QuestionBank

    bank = QuestionBank(tmp_path / "bank.db")
    questions = [
        {
            "type": "single",
            "stem": "下列关于存货初始计量的说法，正确的是？",
            "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
            "answer": "C",
            "analysis": "解析一",
            "knowledge_points": ["存货初始计量"],
            "chapter": "第三章 存货",
        },
        {
            "type": "multiple",
            "stem": "下列各项中，属于收入确认条件的有？",
            "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
            "answer": ["A", "B"],
            "analysis": "解析二",
            "knowledge_points": ["收入确认"],
            "chapter": "第十六章 收入",
        },
    ]
    inserted, skipped = bank.insert_questions(
        "accounting", questions, source="十年真题", year=2025
    )
    assert (inserted, skipped) == (2, 0)
    # 重复入库被去重跳过
    inserted, skipped = bank.insert_questions(
        "accounting", questions, source="十年真题", year=2025
    )
    assert (inserted, skipped) == (0, 2)

    one = bank.sample_one("accounting", "single")
    assert one and one["type"] == "single"
    assert one["stem"].startswith("下列关于存货")
    assert one["year"] == 2025 and one["source"] == "十年真题"

    # 排除近期做过的题干后无题可用
    assert bank.sample_one("accounting", "single", exclude_stems=[questions[0]["stem"]]) is None

    # 多选答案反序列化为字母数组
    multi = bank.sample_one("accounting", "multiple")
    assert multi["answer"] == ["A", "B"]
    assert bank.bank_stats("accounting")["total"] == 2


def test_question_bank_toggle_update_delete(tmp_path):
    from cpa_tutor.question_bank import QuestionBank

    bank = QuestionBank(tmp_path / "bank2.db")
    bank.insert_questions(
        "tax",
        [
            {
                "type": "single",
                "stem": "增值税一般纳税人适用税率说法正确的是？",
                "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                "answer": "A",
                "analysis": "解析",
                "knowledge_points": ["增值税税率"],
            }
        ],
    )
    row = bank.list_bank("tax")[0]
    assert row["active"] == 1
    bank.set_active(row["id"], False)
    assert bank.sample_one("tax", "single") is None
    assert bank.bank_stats("tax")["total"] == 0
    bank.set_active(row["id"], True)
    assert bank.bank_stats("tax")["total"] == 1
    # 校对修正
    bank.update_question(row["id"], {"stem": "修正后的增值税税率题目", "answer": "B"})
    updated = bank.get_question(row["id"])
    assert updated["stem"].startswith("修正后")
    assert updated["answer"] == "B"
    bank.delete_question(row["id"])
    assert bank.list_bank("tax") == []


def test_question_cache_insert_and_sample(tmp_path):
    from cpa_tutor.question_bank import QuestionBank

    bank = QuestionBank(tmp_path / "cache.db")
    bank.insert_cache_questions(
        "accounting",
        [
            {
                "type": "calc",
                "stem": "AI 生成的合并报表抵销分录计算题",
                "answer": "参考答案",
                "analysis": "解析",
                "knowledge_points": ["合并报表抵销"],
                "chapter": "第二十七章 合并财务报表",
            }
        ],
    )
    cached = bank.sample_cache_questions("accounting", "calc", limit=1)
    assert cached and cached[0]["stem"].startswith("AI 生成")


def test_assemble_exam_prefers_bank_then_fills_gaps(tmp_path, monkeypatch):
    from cpa_tutor import exam_engine
    from cpa_tutor.exam_structure import build_exam_plan
    from cpa_tutor.question_bank import QuestionBank

    db = tmp_path / "assemble.db"
    monkeypatch.setenv("CPA_USER_DB_PATH", str(db))
    monkeypatch.setenv("EXAM_GEN_WORKERS", "2")
    bank = QuestionBank(db)
    bank.insert_questions(
        "economic_law",
        [
            {
                "type": "single",
                "stem": "真题单选题",
                "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                "answer": "A",
                "analysis": "解析",
                "knowledge_points": ["考点"],
            }
        ],
    )
    plan = build_exam_plan("economic_law", "mini", seed=1)
    monkeypatch.setattr(
        exam_engine,
        "search_knowledge",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        exam_engine,
        "chat_json",
        lambda *args, **kwargs: {
            "type": "single",
            "stem": "AI 补位题",
            "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
            "answer": "A",
            "analysis": "解析",
            "knowledge_points": ["考点"],
        },
    )
    questions = exam_engine.assemble_exam("economic_law", plan, excluded_stems=[])
    assert len(questions) == len(plan)
    assert any(q["stem"] == "真题单选题" for q in questions.values())  # 题库题优先
    assert any(q["stem"] == "AI 补位题" for q in questions.values())  # 缺口补齐
    assert all("score" in q and q["index"] for q in questions.values())
    # 补位生成的题写入缓存，供后续复用
    cache = QuestionBank(db).sample_cache_questions("economic_law", "single", limit=10)
    assert any(q["stem"] == "AI 补位题" for q in cache)


def test_parse_bank_batch_extracts_and_validates(tmp_path, monkeypatch):
    from cpa_tutor import exam_engine

    chunks = [
        {
            "text": "1. 下列关于会计要素的说法正确的是（ ）。 A. 甲 B. 乙 C. 丙 D. 丁"
            "【答案】C【解析】会计要素解析。"
        }
    ]
    monkeypatch.setattr(
        exam_engine,
        "chat_json",
        lambda *args, **kwargs: [
            {
                "type": "single",
                "stem": "下列关于会计要素的说法正确的是？",
                "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
                "answer": "C",
                "analysis": "会计要素解析。",
                "knowledge_points": ["会计要素"],
                "chapter": "",
            },
            {"type": "single", "stem": "残缺题", "options": {}, "answer": "", "analysis": ""},
        ],
    )
    questions, stats = exam_engine.parse_bank_batch("accounting", chunks, year=2024)
    assert stats["batches"] == 1 and stats["valid"] == 1 and stats["invalid"] == 1
    assert questions[0]["stem"].startswith("下列关于会计要素")


def test_normalize_judge_result_score_ratio():
    from cpa_tutor.exam_engine import normalize_judge_result

    assert (
        normalize_judge_result({"verdict": "correct", "is_correct": True})["score_ratio"]
        == 1.0
    )
    partial = normalize_judge_result(
        {"verdict": "partial", "is_correct": False, "score_ratio": 0.5}
    )
    assert partial["score_ratio"] == 0.5 and partial["is_correct"] is False
    assert normalize_judge_result({"verdict": "wrong", "is_correct": False})[
        "score_ratio"
    ] == 0.0
    # 垃圾 ratio 回退 verdict 映射；0.5 步进夹紧
    assert normalize_judge_result({"verdict": "partial", "score_ratio": "abc"})[
        "score_ratio"
    ] == 0.5
    assert normalize_judge_result({"verdict": "partial", "score_ratio": 0.7})[
        "score_ratio"
    ] == 0.5
    # is_correct 为真时强制 1.0
    assert (
        normalize_judge_result(
            {"verdict": "partial", "is_correct": True, "score_ratio": 0.5}
        )["score_ratio"]
        == 1.0
    )


def test_compute_exam_scores_partial_credit_and_by_type():
    from cpa_tutor.exam_engine import compute_exam_scores

    plan = [
        {"index": 1, "type": "single", "score": 2.0},
        {"index": 2, "type": "calc", "score": 9.0},
    ]
    results = [
        {
            "question": {"index": 1, "type": "single", "score": 2.0},
            "is_correct": True,
        },
        {
            "question": {"index": 2, "type": "calc", "score": 9.0},
            "is_correct": False,
            "judge": {"verdict": "partial", "score_ratio": 0.5},
        },
    ]
    summary = compute_exam_scores(results, plan)
    assert summary["score"] == round(100 * (2 + 4.5) / 11, 1)
    assert summary["correct_count"] == 1
    assert summary["by_type"]["calc"]["earned"] == 4.5
    assert summary["by_type"]["single"]["total"] == 2.0
    assert results[0]["earned"] == 2.0 and results[1]["earned"] == 4.5


def test_direct_weak_points_from_wrong_dedupes():
    from cpa_tutor.exam_engine import direct_weak_points_from_wrong

    results = [
        {
            "is_correct": False,
            "question": {"knowledge_points": ["长期股权投资", "合并报表"]},
        },
        {
            "is_correct": False,
            "question": {"knowledge_points": ["长期股权投资", "出题失败"]},
        },
        {"is_correct": True, "question": {"knowledge_points": ["收入确认"]}},
    ]
    points = direct_weak_points_from_wrong(results)
    assert [point["name"] for point in points] == ["长期股权投资", "合并报表"]


def test_given_up_result_shape_and_scoring():
    from cpa_tutor.exam_engine import compute_exam_scores, given_up_result

    question = {
        "index": 1,
        "type": "single",
        "stem": "题干",
        "score": 2.0,
        "knowledge_points": ["长期股权投资"],
    }
    result = given_up_result(question)
    assert result["is_correct"] is False
    assert result["given_up"] is True
    assert result["earned"] == 0
    assert result["judge"]["score_ratio"] == 0.0
    assert result["student_answer_display"] == "不会"

    summary = compute_exam_scores(
        [result], [{"index": 1, "type": "single", "score": 2.0}]
    )
    assert summary["correct_count"] == 0 and summary["score"] == 0.0


def test_choose_mindmap_format_fallback():
    from cpa_tutor.exam_engine import choose_mindmap_format

    assert (
        choose_mindmap_format({"format": "mermaid", "content": "mindmap\n  root((考点))"})
        == "mermaid"
    )
    # 字段声称 mermaid 但内容不含 mindmap → 降级 tree
    assert choose_mindmap_format({"format": "mermaid", "content": "随便内容"}) == "tree"
    assert choose_mindmap_format({"format": "tree", "content": "考点\n  分支"}) == "tree"
    assert choose_mindmap_format("垃圾") == "tree"
    assert choose_mindmap_format(None) == "tree"


def test_review_content_prompt_builders_include_topic(monkeypatch):
    from cpa_tutor import exam_engine

    monkeypatch.setattr(exam_engine, "search_knowledge", lambda *args, **kwargs: [])
    builders = (
        exam_engine.mindmap_messages,
        exam_engine.lesson_messages,
        exam_engine.examples_messages,
        exam_engine.card_messages,
    )
    for builder in builders:
        messages = builder("accounting", "长期股权投资", retriever=None)
        assert any("长期股权投资" in str(m["content"]) for m in messages)
    # 思维导图提示词包含 mermaid/tree 约定
    mindmap_user = exam_engine.mindmap_messages("accounting", "长期股权投资")[1][
        "content"
    ]
    assert "mermaid" in mindmap_user and "tree" in mindmap_user
    # 记忆卡片提示词要求可选 hint 字段
    card_user = exam_engine.card_messages("accounting", "长期股权投资")[1]["content"]
    assert "hint" in card_user


def test_record_review_result_streak_to_mastery_and_wrong_resets(tmp_path, monkeypatch):
    monkeypatch.setenv("WEAK_POINT_MASTERY_STREAK", "2")
    store = WeakPointStore(tmp_path / "review.db")
    store.upsert_weak_point(7, "accounting", "长期股权投资", reason="错因")

    # 第一次答对：streak=1，仍未掌握
    first = store.record_review_result(7, "accounting", "长期股权投资", True)
    assert first["correct_streak"] == 1 and first["mastered"] == 0
    # 连续第二次答对：达到阈值自动标记掌握
    second = store.record_review_result(7, "accounting", "长期股权投资", True)
    assert second["correct_streak"] == 2 and second["mastered"] == 1
    assert second["mastered_at"]
    # 答错：streak 清零、wrong_count+1、重新激活（已掌握也回退）
    wrong = store.record_review_result(7, "accounting", "长期股权投资", False)
    assert wrong["correct_streak"] == 0 and wrong["mastered"] == 0
    assert wrong["wrong_count"] == 2
    # 答对但无对应记录：不新建
    assert store.record_review_result(7, "accounting", "不存在的考点", True) is None
    # 答错且无对应记录：新建
    created = store.record_review_result(7, "accounting", "新考点", False, reason="练习答错")
    assert created and created["wrong_count"] == 1


def test_match_question_points_to_weak_exact_and_containment():
    from cpa_tutor.exam_engine import match_question_points_to_weak

    rows = [
        {"id": 1, "knowledge_point": "长期股权投资权益法"},
        {"id": 2, "knowledge_point": "增值税进项税额抵扣"},
    ]
    assert match_question_points_to_weak(["长期股权投资权益法"], rows) == [rows[0]]
    # 互相包含匹配（≥2 字）
    assert match_question_points_to_weak(["权益法"], rows)[0]["id"] == 1
    assert match_question_points_to_weak(["企业所得税"], rows) == []


def test_similar_question_messages_include_snapshot(monkeypatch):
    from cpa_tutor import exam_engine

    monkeypatch.setattr(exam_engine, "search_knowledge", lambda *args, **kwargs: [])
    original = {
        "type": "single",
        "stem": "原题题干内容",
        "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
        "answer": "C",
        "answer_display": "C. 丙",
        "analysis": "原题解析内容",
        "knowledge_points": ["存货计量"],
    }
    messages = exam_engine.similar_question_messages(
        "accounting", original, "存货计量"
    )
    user_text = messages[1]["content"]
    assert "原题题干内容" in user_text and "原题解析内容" in user_text
    assert "存货计量" in user_text


def test_home_page_today_review_push(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("CPA_USER_DB_PATH", str(tmp_path / "home_push.db"))
    acct = AccountStore()
    user = acct.register("13900000002", "secret123", "学生", phone="13900000002")
    WeakPointStore().upsert_weak_point(
        user["id"], "accounting", "长期股权投资", reason="错因"
    )
    at = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=20)
    at.session_state["auth_user"] = user
    at.session_state["selected_course_key"] = "accounting"
    at.run()
    assert not at.exception, [exc.value for exc in at.exception]
    assert any("今日待复习" in str(m.value) for m in at.subheader)
    assert any("长期股权投资" in str(m.value) for m in at.markdown)


def test_streamlit_app_and_pages_smoke(tmp_path, monkeypatch):
    import os

    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("APP_DEVELOPER_ACCOUNTS", "13800000001")
    acct = AccountStore(tmp_path / "smoke.db")
    demo = acct.register("13800000001", "secret123", "冒烟测试", phone="13800000001")
    # 让页面实例与账号使用同一个隔离数据库（页面默认读取环境变量路径）
    os.environ["CPA_USER_DB_PATH"] = str(tmp_path / "smoke.db")
    # 注：home.py 含 st.page_link，需要 app.py 的导航注册上下文，
    # 故不在本列表单独运行（其渲染通过 test_home_page_shows_resume_banner 覆盖）。
    paths = (
        "app.py",
        "app_pages/account.py",
        "app_pages/review.py",
        "app_pages/weak_points.py",
        "app_pages/wrong_book.py",
        "app_pages/settings.py",
    )
    for path in paths:
        at = AppTest.from_file(str(PROJECT_ROOT / path), default_timeout=20)
        at.session_state["auth_user"] = demo
        at.session_state["selected_course_key"] = "accounting"
        at.run()
        assert not at.exception, [exc.value for exc in at.exception]


def test_settings_page_rejects_regular_student(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("CPA_USER_DB_PATH", str(tmp_path / "guard.db"))
    student = AccountStore().register(
        "13900000002", "secret123", "普通学生", phone="13900000002"
    )
    at = AppTest.from_file(str(PROJECT_ROOT / "app_pages/settings.py"), default_timeout=20)
    at.session_state["auth_user"] = student
    at.session_state["selected_course_key"] = "accounting"
    at.run()
    assert not at.exception
    assert at.error, "普通学生应看到权限拦截提示"
    assert not at.title, "普通学生不应看到开发者页面内容"
