"""学习首页：学生个人学习画像、科目进度与今日建议。

所有数据均按当前登录学生隔离；切换科目即切换该生在该科下的记录。
"""

from __future__ import annotations

import streamlit as st

from app_pages._helpers import current_user, current_user_id, page_guide, require_user
from cpa_tutor.config import describe_llm, llm_config
from cpa_tutor.courses import COURSES, COURSE_KEYS, course_by_key
from cpa_tutor.knowledge import course_status, readable_ratio_label
from cpa_tutor.weak_points import WeakPointStore

require_user()
user = current_user()
user_id = current_user_id()

course_key = st.session_state.get("selected_course_key") or "accounting"
course = course_by_key(course_key)
store = WeakPointStore()

st.title(f"你好，{user.get('nickname', '同学')}！", icon=":material/dashboard:")
st.markdown(
    f"当前正在学习 **《{course.name}》**。这是**你的专属**学习画像：模考成绩、"
    "错题与未掌握知识点都只属于你的账号，AI 会据此安排复习与组卷。"
)

# ---------------------------------------------------------------- 续考横幅

draft = store.load_exam_draft(user_id)
if draft and not draft.get("finished"):
    draft_course = course_by_key(draft.get("course_key") or "")
    answered = len(draft.get("results") or [])
    total = len(draft.get("plan") or [])
    with st.container(border=True):
        banner_cols = st.columns([3, 1], vertical_alignment="center")
        banner_text = (
            f":material/replay: **你有一次未完成的模拟考试**　"
            f"《{draft.get('title', '模拟试卷')}》　已完成 {answered}/{total} 题"
        )
        if draft_course:
            banner_text += f"　科目：{draft_course.name}"
        banner_cols[0].markdown(banner_text)
        banner_cols[0].caption("考试进度每题自动保存，随时退出、下次登录从这里继续。")
        if banner_cols[1].button(
            "继续作答",
            icon=":material/replay:",
            type="primary",
            width="stretch",
            key="resume_exam_btn",
        ):
            st.session_state["exam"] = draft
            if draft_course and draft_course.key != course_key:
                st.session_state["selected_course_key"] = draft_course.key
                st.session_state["selected_course_name"] = draft_course.name
            st.switch_page("app_pages/mock_exam.py")

# ---------------------------------------------------------------- 当前科目画像

progress = store.course_progress(user_id)
current = progress.get(course_key, {})
status = course_status(course_key)
recent = store.recent_exams(user_id, course_key, limit=6)
focus = store.focus_recommendations(user_id, course_key, limit=5)

is_new_student = not current.get("exam_count") and not focus
page_guide(
    "新手三步走：开始你的 CPA 学习",
    [
        f"在左侧把备考科目切到你要学的科目（当前：《{course.name}》）；",
        "去「教材研读」先看教材/真题，不理解的地方直接问 AI 教练；",
        "去「模拟考试」选“针对我的薄弱知识点”组一套卷，交卷后系统自动记录成绩、错题和弱项；",
        "回到本页查看进度，再到「未掌握知识点」「错题与记录」完成复习闭环。",
    ],
    expanded=is_new_student,
)

m1, m2, m3, m4 = st.columns(4)
with m1.container(border=True):
    st.metric("已完成模考", current.get("exam_count", 0))
with m2.container(border=True):
    st.metric("平均得分率", f"{current.get('avg_score', 0):.1f}%")
with m3.container(border=True):
    st.metric("待巩固知识点", current.get("active_weak", 0))
with m4.container(border=True):
    st.metric("已掌握知识点", current.get("mastered_weak", 0))

st.caption(
    f"《{course.name}》知识库：{status['chunk_count']} 个文本分块 · "
    f"{readable_ratio_label(status)}"
)

# ---------------------------------------------------------------- 今日待复习（自动推送）

st.subheader("今日待复习", icon=":material/alarm:")
if focus:
    for point in focus[:5]:
        push_cols = st.columns([3, 1], vertical_alignment="center")
        push_cols[0].markdown(
            f"**:material/error_circle: {point['knowledge_point']}**"
            f"（错 {point['wrong_count']} 次 · 最近 {str(point['last_wrong_at'])[:10]}）"
        )
        if push_cols[1].button(
            "一键复习",
            icon=":material/bolt:",
            key=f"home_review_{user_id}_{point['id']}",
        ):
            # 跳到未掌握知识点页并自动选中该点、自动生成摘要（点击即到复习卡片）
            st.session_state[f"wp_select_{user_id}_{course_key}"] = (
                f"{point['knowledge_point']}（错 {point['wrong_count']} 次）"
            )
            st.session_state["wp_auto_gen"] = {
                "user_id": user_id,
                "course_key": course_key,
                "point_id": point["id"],
                "kind": "summary",
            }
            st.switch_page("app_pages/weak_points.py")
else:
    st.caption("今日没有待复习知识点。答错的题会自动沉淀到这里，形成专属错题闭环。")

col_trend, col_focus = st.columns([1.25, 1], vertical_alignment="top")

with col_trend:
    with st.container(border=True):
        st.subheader("模考成绩走势", icon=":material/show_chart:")
        if recent:
            scores = [float(item["score"]) for item in reversed(recent)]
            st.line_chart(
                scores,
                height=180,
                color="#1565A8",
            )
            labels = {
                item["id"]: f"{item['score']:.0f}% · {str(item['finished_at'])[5:16]}"
                for item in reversed(recent)
            }
            st.caption("最近成绩：" + " → ".join(labels.values()))
        else:
            st.caption("还没有模考记录。去“模拟考试”完成第一套卷，系统会记录你的进步曲线。")
        st.page_link(
            "app_pages/mock_exam.py",
            label="开始一套新的模拟考试",
            icon=":material/play_circle:",
        )

with col_focus:
    with st.container(border=True):
        st.subheader("优先巩固建议", icon=":material/psychology:")
        if focus:
            for point in focus[:5]:
                st.markdown(
                    f":material/error_circle: **{point['knowledge_point']}**"
                    f"（错 {point['wrong_count']} 次）"
                )
            st.page_link(
                "app_pages/weak_points.py",
                label="进入未掌握知识点复习",
                icon=":material/arrow_forward:",
            )
        else:
            st.caption("当前科目暂无待巩固知识点。答错的题会自动沉淀到这里，形成专属错题闭环。")
            st.page_link(
                "app_pages/review.py",
                label="先去教材研读 / AI 答疑",
                icon=":material/menu_book:",
            )

# ---------------------------------------------------------------- 六科总览

has_activity = any(
    item.get("exam_count") or item.get("active_weak")
    for item in progress.values()
)
if has_activity:
    st.subheader("六科学习进度总览", icon=":material/table_chart:")
    summary_rows = []
    for key in COURSE_KEYS:
        item = progress.get(key, {})
        summary_rows.append(
            {
                "科目": COURSES[key].name,
                "模考次数": item.get("exam_count", 0),
                "平均得分率": f"{item.get('avg_score', 0):.1f}%",
                "最佳得分率": f"{item.get('best_score', 0):.1f}%",
                "待巩固": item.get("active_weak", 0),
                "已掌握": item.get("mastered_weak", 0),
                "最近模考": (item.get("last_finished_at") or "")[:10] or "—",
            }
        )
    st.dataframe(summary_rows, hide_index=True, width="stretch")

# ---------------------------------------------------------------- 快捷入口

st.subheader("开始今天的学习", icon=":material/bolt:")
card_cols = st.columns(3)
with card_cols[0]:
    with st.container(border=True, height=210):
        st.markdown("### :material/menu_book: 教材研读")
        st.caption("按章节阅读教材与真题，随时向 AI 教练提问。")
        st.page_link(
            "app_pages/review.py",
            label="进入教材研读",
            icon=":material/arrow_forward:",
        )
with card_cols[1]:
    with st.container(border=True, height=210):
        st.markdown("### :material/quiz: 模拟考试")
        st.caption("真题模拟或**针对你的弱项**组卷，交卷自动诊断并入库。")
        st.page_link(
            "app_pages/mock_exam.py",
            label="开始模拟考试",
            icon=":material/arrow_forward:",
        )
with card_cols[2]:
    with st.container(border=True, height=210):
        st.markdown("### :material/assignment_turned_in: 错题复盘")
        st.caption("翻看最近错题、逐题回顾解析，反复训练直到掌握。")
        st.page_link(
            "app_pages/wrong_book.py",
            label="打开错题本",
            icon=":material/arrow_forward:",
        )

with st.expander("学习闭环 Agent 工作流", icon=":material/account_tree:"):
    st.markdown(
        """
        ```mermaid
        graph LR
            A[选择科目] --> B[教材研读]
            A --> C[模拟考试]
            C --> D[提交一题]
            D --> E[即时解析]
            C --> F[整卷诊断]
            F --> G[按学生写入弱项与错题]
            G --> H[个性化复习与组卷]
            H --> I[标记已掌握]
        ```
        """
    )
    st.caption(
        "同一道错题对应的知识点会累计错误次数；掌握后被再次答错会自动回到待巩固清单。"
    )

cfg = llm_config()
if cfg.get("available"):
    st.caption(f"当前对话模型：{describe_llm(cfg)}")
else:
    st.warning(
        "尚未配置对话模型，模拟考试与 AI 答疑暂不可用；可先浏览教材资料。",
        icon=":material/warning:",
    )
