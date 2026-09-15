"""错题与记录：按当前学生展示考试历史与错题，并提供“基于错题考点再练”。

错题快照在每次模考结算时完整入库（题干/选项/答案/解析），本页可随时复盘。
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime

import streamlit as st

from app_pages._helpers import current_user_id, page_guide, require_user
from cpa_tutor.config import llm_config
from cpa_tutor.courses import course_by_key
from cpa_tutor.exam_engine import (
    MULTIPLE,
    SINGLE,
    evaluate_choice_answer,
    format_student_answer,
    is_subjective_type,
    judge_subjective,
    normalize_question,
    plan_summary,
    similar_question_messages,
    type_label,
)
from cpa_tutor.exam_structure import build_exam_plan
from cpa_tutor.knowledge import best_retriever
from cpa_tutor.llm import LLMError, chat_json
from cpa_tutor.weak_points import WeakPointStore

require_user()
user_id = current_user_id()

course_key = st.session_state.get("selected_course_key") or "accounting"
course = course_by_key(course_key)
store = WeakPointStore()

st.title("错题与记录", icon=":material/assignment_turned_in:")
st.markdown(f"当前科目：**《{course.name}》** · 所有记录均来自你的账号")

page_guide(
    "错题与记录页操作指引",
    [
        "「错题回顾」列出你最近答错的题，选一道可查看题干、你的答案与完整解析；",
        "点击“基于本题考点再练”，系统会生成新题帮你巩固同考点；",
        "「考试记录」可查看每一次模考的得分和逐题明细，用于复盘进步曲线；",
        "错题复盘后到「未掌握知识点」继续摘要/测验/记忆卡片复习。",
    ],
)

current = store.course_progress(user_id).get(course_key, {})
wrong_items = store.wrong_questions(user_id, course_key, limit_attempts=80)

m1, m2, m3, m4 = st.columns(4)
with m1.container(border=True):
    st.metric("已完成模考", current.get("exam_count", 0))
with m2.container(border=True):
    st.metric("平均得分率", f"{current.get('avg_score', 0):.1f}%")
with m3.container(border=True):
    st.metric("错题数（近期）", len(wrong_items))
with m4.container(border=True):
    st.metric("待巩固知识点", current.get("active_weak", 0))

tab_wrong, tab_history = st.tabs(
    [":material/assignment_late: 错题回顾", ":material/history: 考试记录"]
)


def _launch_focus_exam(topics: list[str], title: str) -> None:
    """创建一份针对指定知识点的迷你巩固卷并跳转到模拟考试页作答。"""
    topics = list(dict.fromkeys(t for t in topics if t and t != "出题失败"))[:8]
    if not topics:
        topics = ["综合应用"]
    plan = build_exam_plan(
        course_key,
        "mini",
        chapters=topics,
        seed=random.randrange(1, 10**9),
    )
    st.session_state["exam"] = {
        "course_key": course_key,
        "title": title,
        "plan": plan,
        "questions": {},
        "current_index": plan[0]["index"],
        "results": [],
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished": False,
        "diagnosis_done": False,
        "diagnosis": None,
        "plan_summary": plan_summary(plan),
        "note": f"策略：由错题考点生成的个性化巩固卷（{len(topics)} 个考点）",
    }
    st.switch_page("app_pages/mock_exam.py")


with tab_wrong:
    if not wrong_items:
        st.info(
            "当前科目还没有错题记录。完成模拟考试后，答错的题目会自动进入这里。",
            icon=":material/info:",
        )
    else:
        all_topics = [
            topic
            for item in wrong_items
            for topic in (item.get("knowledge_points") or [])
            if topic and topic != "出题失败"
        ]
        if all_topics:
            top_col = st.container(horizontal=True, horizontal_alignment="right")
            top_col.button(
                "针对全部错题考点再练一套",
                icon=":material/autorenew:",
                on_click=_launch_focus_exam,
                args=(list(dict.fromkeys(all_topics)), f"《{course.name}》错题考点再练"),
            )

        options = []
        for idx, item in enumerate(wrong_items):
            q = item.get("question") or {}
            stem = (q.get("stem") or "（旧版记录，仅显示答题信息）")[:55]
            date = str(item.get("finished_at") or "")[:10]
            options.append(f"{date} · {stem}")
        selected_label = st.selectbox(
            "选择一道错题复盘",
            options=options,
            key=f"wrong_select_{user_id}_{course_key}",
        )
        selected = wrong_items[options.index(selected_label)]
        q = selected.get("question") or {}

        with st.container(border=True):
            st.markdown(f"**来源：** {selected.get('exam_title', '')}　"
                        f"考试时间：{(selected.get('finished_at') or '')[:16]}")
            if not q.get("stem"):
                st.caption("该错题来自旧版记录，仅保留了知识点与答题情况。")
            else:
                st.markdown(f"**{q.get('stem')}**")
                for letter in sorted((q.get("options") or {}).keys()):
                    option_text = q["options"][letter]
                    st.markdown(f"- {letter}. {option_text}")
                st.markdown("##### 你的答案")
                st.caption(selected.get("student_answer_display") or selected.get("student_answer") or "未作答")
                st.markdown("##### 正确答案与解析")
                with st.container(border=True):
                    st.markdown(f"**正确答案：** {q.get('answer_display', q.get('answer', ''))}")
                    st.markdown(q.get("analysis", "（旧版记录未保存解析）"))
                    if q.get("knowledge_points"):
                        st.caption("对应知识点：" + "、".join(q["knowledge_points"]))

        retrain_topics = list(dict.fromkeys(q.get("knowledge_points") or []))
        if retrain_topics:
            st.button(
                "基于本题考点再练（AI 生成新题）",
                icon=":material/refresh:",
                type="primary",
                width="stretch",
                on_click=_launch_focus_exam,
                args=(retrain_topics, f"《{course.name}》错题考点巩固"),
            )

        # ---------------------------------------------------------------- 类似题练习（AI 变式）
        if q.get("stem"):
            stem_hash = hashlib.md5(q.get("stem", "").encode("utf-8")).hexdigest()[:10]
            sim_key = f"sim_q_{user_id}_{stem_hash}"
            want_similar = st.button(
                "类似题练习（AI 生成变式题，答对自动巩固）",
                icon=":material/psychology_alt:",
                key=f"btn_similar_{user_id}_{stem_hash}",
            )
            if want_similar and st.session_state.get(sim_key) is None:
                if not llm_config().get("available"):
                    st.warning(
                        "当前没有可用的对话模型，无法生成类似题。",
                        icon=":material/warning:",
                    )
                else:
                    try:
                        with st.status("AI 正在生成类似题…", expanded=False) as sim_status:
                            retriever = best_retriever(course_key)
                            raw = chat_json(
                                similar_question_messages(
                                    course_key,
                                    q,
                                    "、".join(q.get("knowledge_points") or ["综合应用"]),
                                    retriever=retriever,
                                ),
                                temperature=0.25,
                            )
                            st.session_state[sim_key] = normalize_question(
                                raw,
                                str(q.get("type") or "single"),
                                index=1,
                                chapter=q.get("chapter", ""),
                            )
                            sim_status.update(label="类似题生成完成", state="complete")
                    except (LLMError, ValueError) as exc:
                        st.error(f"类似题生成失败：{exc}", icon=":material/error:")

            sim_question = st.session_state.get(sim_key)
            if sim_question:
                st.markdown("#### :material/psychology_alt: 类似题练习")
                st.caption(
                    "变式题与原错题同题型、同考点、同难度；答对会累计掌握进度，"
                    "连续答对达到阈值后该知识点自动从「未掌握」移除，答错则清零重计。"
                )
                st.markdown(sim_question["stem"])
                sim_option_labels = {
                    f"{letter}. {text}": letter
                    for letter, text in (sim_question.get("options") or {}).items()
                }
                with st.form(f"sim_form_{user_id}_{stem_hash}"):
                    if sim_question["type"] == SINGLE:
                        sim_selected = st.radio(
                            "选择你认为正确的答案",
                            options=list(sim_option_labels),
                            key=f"sim_ans_{user_id}_{stem_hash}",
                        )
                        sim_answer = sim_option_labels.get(sim_selected, "")
                    elif sim_question["type"] == MULTIPLE:
                        sim_multi = st.multiselect(
                            "选择所有你认为正确的答案",
                            options=list(sim_option_labels),
                            key=f"sim_ans_{user_id}_{stem_hash}",
                        )
                        sim_answer = [sim_option_labels[label] for label in sim_multi]
                    else:
                        sim_answer = st.text_area(
                            "写出你的解答过程（包括必要计算/结论）",
                            key=f"sim_ans_{user_id}_{stem_hash}",
                        )
                    sim_submitted = st.form_submit_button(
                        "提交并查看解析",
                        icon=":material/send:",
                        type="primary",
                        width="stretch",
                    )

                if sim_submitted:
                    if is_subjective_type(
                        sim_question["type"]
                    ) and not str(sim_answer or "").strip():
                        st.error("主观题请先填写答案再提交。", icon=":material/error:")
                    else:
                        try:
                            if is_subjective_type(sim_question["type"]):
                                verdict = judge_subjective(
                                    sim_question,
                                    str(sim_answer or ""),
                                    llm_overrides=st.session_state.get("llm_overrides"),
                                )
                                is_correct = bool(verdict["is_correct"])
                                display = format_student_answer(sim_question, sim_answer)
                                feedback = verdict.get("feedback", "")
                            else:
                                is_correct, display = evaluate_choice_answer(
                                    sim_question, sim_answer
                                )
                                feedback = ""
                            topics = (
                                sim_question.get("knowledge_points")
                                or q.get("knowledge_points")
                                or []
                            )
                            mastered_names = []
                            for topic_name in topics:
                                updated = store.record_review_result(
                                    user_id,
                                    course_key,
                                    topic_name,
                                    bool(is_correct),
                                    reason="错题类似题练习答错，该考点仍需巩固。",
                                    related_question=sim_question["stem"][:200],
                                )
                                if is_correct and updated and updated.get("mastered"):
                                    mastered_names.append(updated["knowledge_point"])
                            if mastered_names:
                                st.toast(
                                    "连续答对，已自动标记掌握："
                                    + "、".join(mastered_names)
                                    + "！"
                                )
                            if is_correct:
                                st.success(
                                    "答对了！对应考点已累计一次掌握进度。",
                                    icon=":material/check_circle:",
                                )
                            else:
                                st.error(
                                    "答错了：该考点已记入未掌握清单，请认真学习解析。",
                                    icon=":material/error:",
                                )
                            st.markdown(f"**你的答案：** {display or '未作答'}")
                            st.markdown(
                                f"**正确答案：** "
                                f"{sim_question.get('answer_display', sim_question['answer'])}"
                            )
                            if feedback:
                                st.caption(f"AI 阅卷：{feedback}")
                            with st.container(border=True):
                                st.markdown(sim_question["analysis"])
                            st.caption(
                                "完成后可到「未掌握知识点」页继续摘要/思维导图/记忆卡片复习。"
                            )
                        except LLMError as exc:
                            st.error(str(exc), icon=":material/error:")

with tab_history:
    attempts = store.recent_exams(user_id, course_key, limit=50)
    if not attempts:
        st.caption("还没有考试记录。")
    else:
        table_rows = [
            {
                "时间": str(item["finished_at"])[:16],
                "试卷": item["exam_title"],
                "完成": f"{item['total_questions']} 题",
                "得分率": f"{item['score']:.1f}%",
            }
            for item in attempts
        ]
        st.dataframe(table_rows, hide_index=True, width="stretch")
        selected_attempt = st.selectbox(
            "查看某一次考试逐题明细",
            options=[f"{item['finished_at'][:16]} · {item['exam_title']}" for item in attempts],
            key=f"attempt_select_{user_id}_{course_key}",
        )
        history = store.exam_history(user_id, course_key, limit=50)
        selected_time = selected_attempt.split(" · ", 1)[0]
        attempt = next(
            (item for item in history if item["finished_at"].startswith(selected_time)),
            history[0],
        )
        for detail in attempt.get("details") or []:
            question = detail.get("question") or {}
            badge = ":green-badge[答对]" if detail.get("is_correct") else ":red-badge[答错]"
            st.markdown(
                f"**{question.get('index', '?')}. "
                f"{type_label(question.get('type', ''))}** {badge}"
            )
            if question.get("stem"):
                st.markdown(question["stem"])
                st.caption(
                    f"你的答案：{detail.get('student_answer_display') or detail.get('student_answer')}"
                )
                st.caption(
                    f"正确答案：{question.get('answer_display', question.get('answer', ''))}"
                )
            else:
                st.caption(
                    f"你的答案：{detail.get('student_answer_display') or detail.get('student_answer')}"
                    f"；知识点：{'、'.join(question.get('knowledge_points', []))}"
                )
