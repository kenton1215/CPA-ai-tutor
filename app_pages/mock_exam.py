"""模拟考试：AI 随机组卷 / 针对个人弱项组卷，逐题作答、整卷诊断入库。

每场考试结束后按当前学生保存：得分、每题明细（题干/选项/答案/解析）、
AI 诊断出的未掌握知识点。错题自动成为该学生错题本的数据源。
"""

from __future__ import annotations

import random
from datetime import datetime

import streamlit as st

from app_pages._helpers import current_user_id, page_guide, require_user
from cpa_tutor.config import llm_config
from cpa_tutor.courses import course_by_key
from cpa_tutor.exam_engine import (
    MULTIPLE,
    SINGLE,
    analyze_wrong_questions,
    assemble_exam,
    compute_exam_scores,
    direct_weak_points_from_wrong,
    evaluate_choice_answer,
    format_answer_for_display,
    format_student_answer,
    generate_question,
    given_up_result,
    is_subjective_type,
    judge_subjective,
    match_question_points_to_weak,
    plan_summary,
    type_label,
)
from cpa_tutor.exam_structure import SCALES, build_exam_plan, structure_text
from cpa_tutor.question_bank import QuestionBank
from cpa_tutor.knowledge import (
    best_retriever,
    build_chapter_index,
    course_status,
    load_course_chunks,
)
from cpa_tutor.weak_points import WeakPointStore

require_user()
user_id = current_user_id()

course_key = st.session_state.get("selected_course_key") or "accounting"
course = course_by_key(course_key)
store = WeakPointStore()


def _save_draft() -> None:
    """把进行中的考试草稿写入数据库，中途退出后下次登录可从首页续考。"""
    exam = st.session_state.get("exam")
    if exam:
        store.save_exam_draft(
            user_id, exam.get("course_key") or course_key, exam
        )


def settle_exam() -> None:
    """自动结算整卷：分值制成绩 → AI 诊断 → 考试记录入库 → 清理草稿。

    AI 诊断失败时使用错题自带考点兜底，保证考试记录与弱项绝不丢失。
    结算后 exam["score_summary"] / exam["diagnosis"] 就绪，页面跳转报告。
    """
    exam = st.session_state["exam"]
    results = exam["results"]
    summary = compute_exam_scores(results, exam["plan"])
    wrong_items = [item for item in results if not item.get("is_correct")]
    with st.status("AI 正在诊断错题与未掌握知识点…", expanded=True) as diag_status:
        try:
            if wrong_items:
                diagnosis = analyze_wrong_questions(
                    course_key,
                    wrong_items[:12],
                    llm_overrides=st.session_state.get("llm_overrides"),
                )
            else:
                diagnosis = {
                    "summary": "本卷全部答对，没有需要沉淀的新薄弱点。继续保持！",
                    "knowledge_points": [],
                }
        except Exception:
            diagnosis = {
                "summary": "AI 诊断暂不可用，已按错题考点直接沉淀未掌握知识点。",
                "knowledge_points": direct_weak_points_from_wrong(wrong_items),
            }
        diag_status.update(label="诊断完成", state="complete")
    saved = store.add_weak_points(
        user_id,
        course_key,
        diagnosis.get("knowledge_points", []),
        related_question="、".join(
            (item["question"].get("knowledge_points") or [""])[0]
            for item in wrong_items[:3]
        ),
    )
    # 巩固闭环：把每题作答结果映射到该学生的未掌握知识点
    # （答对累计连续答对次数，达到阈值自动标记掌握；答错清零并累计错误）
    active_rows = store.list_weak_points(user_id, course_key, mastered=0)
    if active_rows:
        for item in results:
            question = item.get("question") or {}
            matched = match_question_points_to_weak(
                question.get("knowledge_points") or [], active_rows
            )
            for row in matched:
                updated = store.record_review_result(
                    user_id,
                    course_key,
                    row["knowledge_point"],
                    bool(item.get("is_correct")),
                    related_question=question.get("stem", "")[:200],
                )
                if (
                    item.get("is_correct")
                    and updated
                    and updated.get("mastered")
                ):
                    st.toast(
                        f"连续答对，已自动标记掌握：{updated['knowledge_point']}！"
                    )
    exam["diagnosis"] = diagnosis
    exam["diagnosis_done"] = True
    exam["score_summary"] = summary
    details = [
        {
            "question": item["question"],
            "is_correct": item.get("is_correct"),
            "student_answer": item.get("student_answer"),
            "student_answer_display": item.get("student_answer_display"),
            "knowledge_points": item["question"].get("knowledge_points", []),
            "judge": item.get("judge"),
            "earned": item.get("earned"),
            "given_up": bool(item.get("given_up")),
        }
        for item in results
    ]
    store.record_exam_attempt(
        user_id,
        course_key,
        exam["title"],
        len(results),
        summary["correct_count"],
        summary["score"],
        details,
        exam["started_at"],
    )
    store.delete_exam_draft(user_id)
    if saved:
        st.toast(f"已将 {saved} 个未掌握知识点加入你的弱项清单")


def show_result_and_controls(
    result_item: dict,
    current_index: int,
    plan_items: list[dict],
    exam_state: dict,
) -> None:
    """展示已作答一题的结果，并提供下一题/完成整卷入口。"""
    question = result_item["question"]
    is_correct = bool(result_item.get("is_correct"))
    if result_item.get("given_up"):
        st.warning(
            "已标记“不会”：本题按答错计入，对应考点已加入你的未掌握清单。"
            "请认真学习下面的解题思路。",
            icon=":material/help:",
        )
    elif is_correct:
        st.success("回答正确！下面给出 AI 解题思路。", icon=":material/check_circle:")
    else:
        st.error("这道题答错了。请仔细阅读 AI 解析，掌握对应知识点。", icon=":material/error:")
    st.markdown("#### AI 解题思路与最终答案")
    with st.container(border=True):
        st.markdown(question["analysis"])
        st.markdown(f"**最终答案：** {question.get('answer_display', '')}")
        if question.get("knowledge_points"):
            st.caption("对应知识点：" + "、".join(question["knowledge_points"]))
    if question.get("refs"):
        with st.expander("本题参考的课程素材", icon=":material/article:"):
            for ref in question["refs"]:
                st.caption(f"· {ref.get('source', '')}")

    next_cols = st.columns([1, 1, 1])
    if any(item["index"] > current_index for item in plan_items):
        if next_cols[1].button(
            "下一题",
            icon=":material/arrow_forward:",
            type="primary",
            width="stretch",
            key=f"next_btn_{current_index}_{question['index']}",
        ):
            next_item = min(
                (item for item in plan_items if item["index"] > current_index),
                key=lambda item: item["index"],
            )
            exam_state["current_index"] = next_item["index"]
            _save_draft()
            st.rerun()
    else:
        if next_cols[1].button(
            "完成整卷，查看报告",
            icon=":material/flag:",
            type="primary",
            width="stretch",
            key=f"finish_btn_{current_index}",
        ):
            exam_state["finished"] = True
            exam_state["finished_at"] = datetime.now().isoformat(timespec="seconds")
            _save_draft()
            st.rerun()


st.title("模拟考试", icon=":material/quiz:")
st.markdown(f"当前科目：**《{course.name}》** · 你的考试成绩与错题将自动存档")

if not llm_config().get("available"):
    st.warning(
        "当前没有可用的对话模型，无法生成试卷。请先配置模型。",
        icon=":material/warning:",
    )
    st.stop()

status = course_status(course_key)
chapters: list[str] = []
if status["has_cache"]:
    chapters = [
        item["chapter"]
        for item in build_chapter_index(load_course_chunks(course_key))
    ]
if not chapters:
    st.caption(
        "知识库暂无解析文本，AI 将基于自身 CPA 专业知识出题；"
        "在“知识库与设置”页完成 PDF 解析可获得教材/真题素材支持。"
    )

exam = st.session_state.get("exam")

# ---------------------------------------------------------------- 新试卷配置
if exam is None:
    page_guide(
        "模拟考试操作指引",
        [
            "选择试卷规格与组卷策略：新手建议先用“迷你卷”快速自测；",
            "题型与分值占比按该科目历年官方考试结构自动确定，每题标注分值；",
            "想突击薄弱环节就选“针对我的薄弱知识点”，系统会优先用你未掌握考点出题；",
            "考试时一题一答，提交后立刻看到解析；主观题由 AI 阅卷；",
            "交卷后系统自动阅卷并诊断，成绩、错题与未掌握知识点写入你的账号。",
        ],
    )
    st.markdown(
        f"""
        AI 会依据《{course.name}》的教材知识体系与最近十年命题规律组卷。
        除“真题模拟”外，你可以选择 **针对自己的薄弱知识点组卷**——
        系统会优先从你的未掌握清单取考点，帮助你集中攻克错题。
        """
    )
    focus_points = store.focus_recommendations(user_id, course_key, limit=8)

    with st.container(border=True):
        strategy = st.segmented_control(
            "组卷策略",
            options=["真题模拟（随机章节）", "针对我的薄弱知识点"],
            default="真题模拟（随机章节）",
        )
        scale = st.segmented_control(
            "试卷规格",
            options=["迷你卷（约30分）", "半卷（约50分）", "整卷（约100分）"],
            default="迷你卷（约30分）",
        )
        st.caption(
            f"本科目历年官方考试题型与分值占比：{structure_text(course_key)}；"
            "组卷时按所选规格等比缩放。"
        )
        st.caption("主观题（计算分析/案例分析/简答/综合）将由 AI 阅卷并支持部分给分。")

        if strategy == "针对我的薄弱知识点" and not focus_points:
            st.warning(
                "当前科目还没有可用的薄弱知识点，将自动使用真题随机组卷。",
                icon=":material/info:",
            )
        if focus_points:
            st.caption(
                "待巩固考点：" + "、".join(
                    f"{p['knowledge_point']}（{p['wrong_count']}次）"
                    for p in focus_points[:5]
                )
            )
        start = st.button(
            "开始生成试卷",
            icon=":material/play_arrow:",
            type="primary",
            width="stretch",
        )

    if start:
        scale_key = {
            "迷你卷（约30分）": "mini",
            "半卷（约50分）": "half",
            "整卷（约100分）": "full",
        }[scale]

        if strategy == "针对我的薄弱知识点" and focus_points:
            plan_chapters = [p["knowledge_point"] for p in focus_points]
            exam_title = f"《{course.name}》薄弱知识点巩固"
            note = f"策略：针对你的 {len(plan_chapters)} 个薄弱知识点组卷"
        else:
            plan_chapters = chapters
            exam_title = f"《{course.name}》AI 模拟试卷"
            note = "策略：真题模拟（随机章节）"

        plan = build_exam_plan(
            course_key,
            scale_key,
            chapters=plan_chapters,
            seed=random.randrange(1, 10**9),
        )
        excluded = store.recent_question_stems(user_id, course_key)
        with st.status(
            "正在装配试卷：优先从十年真题题库选题，缺口由 AI 补充生成…",
            expanded=True,
        ) as assemble_status:
            progress_bar = st.progress(0.0)

            def _gen_progress(done: int, total: int) -> None:
                ratio = min(done / max(total, 1), 1.0)
                progress_bar.progress(ratio, text=f"AI 补充生成 {done}/{total} 题")

            try:
                retriever = best_retriever(course_key)
            except Exception:
                retriever = None
            questions = assemble_exam(
                course_key,
                plan,
                excluded_stems=excluded,
                llm_overrides=st.session_state.get("llm_overrides"),
                retriever=retriever,
                progress=_gen_progress,
            )
            assemble_status.update(
                label=f"试卷装配完成：{len(questions)}/{len(plan)} 题已就绪（未就绪的题将在作答时现场生成）",
                state="complete",
            )
        st.session_state["exam"] = {
            "course_key": course_key,
            "title": exam_title,
            "plan": plan,
            "questions": questions,
            "current_index": plan[0]["index"],
            "results": [],
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished": False,
            "diagnosis_done": False,
            "diagnosis": None,
            "scale": scale_key,
            "plan_summary": plan_summary(plan),
            "note": note,
        }
        _save_draft()
        st.rerun()
    st.stop()

# ---------------------------------------------------------------- 科目切换保护
if exam.get("course_key") != course_key:
    st.warning(
        "当前选择科目与进行中的试卷不一致，请先结束当前试卷或切回原科目。",
        icon=":material/warning:",
    )
    st.stop()

# ---------------------------------------------------------------- 整卷报告（自动阅卷与结算）
if exam.get("finished"):
    if not exam.get("diagnosis_done"):
        # 交卷即自动结算：分值制成绩 + AI 诊断 + 入库（诊断失败自动兜底）
        settle_exam()
        st.rerun()

    st.subheader("整卷报告")
    if exam.get("note"):
        st.caption(exam["note"])
    if exam.get("plan_summary"):
        st.caption(f"试卷结构：{exam['plan_summary']}")

    results = exam["results"]
    summary = exam.get("score_summary") or compute_exam_scores(results, exam["plan"])
    total_count = len(results)
    score = summary["score"]
    correct_count = summary["correct_count"]

    m1, m2, m3, m4 = st.columns(4)
    with m1.container(border=True):
        st.metric("得分率", f"{score:.1f}%")
    with m2.container(border=True):
        st.metric("答对", correct_count)
    with m3.container(border=True):
        st.metric("答错 / 不会", total_count - correct_count)
    with m4.container(border=True):
        st.metric(
            "实得 / 满分",
            f"{summary['earned_total']:.1f} / {summary['total_score']:.1f}",
        )

    if summary.get("by_type"):
        with st.expander("各题型得分占比（对照官方题型结构）", expanded=True):
            type_rows = [
                {
                    "题型": type_label(qtype),
                    "题数": int(bucket["count"]),
                    "得分": bucket["earned"],
                    "满分": bucket["total"],
                    "得分率": (
                        f"{100 * bucket['earned'] / bucket['total']:.0f}%"
                        if bucket["total"]
                        else "—"
                    ),
                }
                for qtype, bucket in summary["by_type"].items()
            ]
            st.dataframe(type_rows, hide_index=True, width="stretch")

    if exam.get("diagnosis"):
        st.info(exam["diagnosis"].get("summary", ""), icon=":material/diagnosis:")

    with st.expander("逐题回顾（含你的答案与 AI 解析）", expanded=True):
        for item in results:
            question = item["question"]
            if item.get("given_up"):
                badge = ":red-badge[不会]"
            elif item.get("is_correct"):
                badge = ":green-badge[答对]"
            elif (item.get("judge") or {}).get("verdict") == "partial":
                badge = ":orange-badge[部分得分]"
            else:
                badge = ":red-badge[答错]"
            full = float(item.get("full_score") or question.get("score") or 1)
            st.markdown(
                f"**{question['index']}. {type_label(question['type'])}** {badge}　"
                f"本题得分 {item.get('earned', 0)}/{full:g} 分　"
                f"考点：{'、'.join(question.get('knowledge_points', []))}"
            )
            st.markdown(question["stem"])
            if question["type"] in (SINGLE, MULTIPLE):
                st.caption(f"你的答案：{item.get('student_answer_display')}")
                st.caption(f"正确答案：{question.get('answer_display')}")
            else:
                st.caption(f"你的答案：{str(item.get('student_answer'))[:500]}")
                st.caption(f"参考答案：{question.get('answer_display', '')[:500]}")
                if item.get("judge"):
                    st.caption(f"AI 阅卷：{item['judge'].get('feedback', '')}")
            with st.expander(f"第 {question['index']} 题解析", icon=":material/lightbulb:"):
                st.markdown(question.get("analysis", ""))

    if st.button("再考一套新试卷", icon=":material/refresh:"):
        st.session_state.pop("exam", None)
        store.delete_exam_draft(user_id)
        st.rerun()
    st.stop()

# ---------------------------------------------------------------- 考试进行中
plan = exam["plan"]
current_index = exam["current_index"]
plan_item = next(item for item in plan if item["index"] == current_index)
results = exam["results"]
answered_indexes = {item["question"]["index"] for item in results}
done_count = sum(1 for item in plan if item["index"] in answered_indexes)
total_plan = len(plan)

st.progress(min(done_count / total_plan, 1.0), text=f"已完成 {done_count}/{total_plan} 题")
top_actions = st.columns([1, 1, 2], vertical_alignment="center")
if top_actions[0].button("返回组卷页（不保存）", icon=":material/logout:", key="exit_exam"):
    st.session_state.pop("exam", None)
    store.delete_exam_draft(user_id)
    st.rerun()
with top_actions[1].popover("提前结算已答部分", icon=":material/flag:"):
    st.caption("已完成题目按当前成绩结算，并照常执行薄弱点诊断。")
    if st.button("确认提前结算", icon=":material/check:"):
        exam["finished"] = True
        exam["finished_at"] = datetime.now().isoformat(timespec="seconds")
        exam["note"] = "提前结算（未完成整卷）"
        _save_draft()
        st.rerun()

question = exam["questions"].get(current_index)
if question is None:
    with st.status(f"正在准备第 {current_index} 题…", expanded=True) as gen_status:
        try:
            # 优先从真题题库/生成缓存抽取（快），无可用题再 AI 现场生成
            bank = QuestionBank()
            question = bank.sample_one(
                course_key, plan_item["type"], chapter=plan_item.get("chapter", "")
            )
            if question is None:
                cached = bank.sample_cache_questions(
                    course_key,
                    plan_item["type"],
                    chapter=plan_item.get("chapter", ""),
                    limit=1,
                )
                question = cached[0] if cached else None
            if question is not None:
                question["index"] = current_index
                question["score"] = plan_item.get("score")
                question["refs"] = question.get("refs") or []
                question["answer_display"] = format_answer_for_display(question)
                gen_status.update(
                    label=f"第 {current_index} 题已从题库抽取", state="complete"
                )
            else:
                retriever = best_retriever(course_key)
                question = generate_question(
                    course_key,
                    plan_item,
                    llm_overrides=st.session_state.get("llm_overrides"),
                    retriever=retriever,
                )
                question["score"] = plan_item.get("score")
                bank.insert_cache_questions(course_key, [question])
                gen_status.update(
                    label=f"第 {current_index} 题 AI 生成完成", state="complete"
                )
            exam["questions"][current_index] = question
            _save_draft()
        except Exception as exc:
            gen_status.update(label="出题失败", state="error")
            st.error(f"出题失败：{exc}", icon=":material/error:")
            retry_col, skip_col = st.columns(2)
            if retry_col.button("重新生成本题", icon=":material/refresh:", width="stretch"):
                st.rerun()
            if skip_col.button("跳过本题（按答错计入）", icon=":material/skip_next:", width="stretch"):
                question = {
                    "index": current_index,
                    "type": plan_item["type"],
                    "stem": "（本题出题失败，已跳过）",
                    "options": {},
                    "answer": "跳过",
                    "analysis": "出题失败，本场考试按答错处理。",
                    "knowledge_points": ["出题失败"],
                    "chapter": plan_item.get("chapter", ""),
                    "score": plan_item.get("score"),
                }
                exam["questions"][current_index] = question
                _save_draft()
                st.rerun()
            st.stop()

st.markdown(
    f"**第 {current_index} 题 · {type_label(question['type'])} · "
    f"本题 {question.get('score') or 1} 分**"
)
if question.get("chapter"):
    st.caption(f"考查范围：{question['chapter']}")
st.markdown(question["stem"])

option_labels = {
    f"{letter}. {text}": letter
    for letter, text in (question.get("options") or {}).items()
}

existing_result = next(
    (item for item in results if item["question"]["index"] == current_index), None
)
if existing_result is not None:
    show_result_and_controls(existing_result, current_index, plan, exam)
    st.stop()

with st.form(f"exam_form_{current_index}", border=True):
    if question["type"] == SINGLE:
        selected = st.radio(
            "选择你认为正确的答案",
            options=list(option_labels),
            key=f"exam_ans_{user_id}_{course_key}_{current_index}",
        )
        student_submit = option_labels.get(selected, "")
    elif question["type"] == MULTIPLE:
        selected_multi = st.multiselect(
            "选择所有你认为正确的答案",
            options=list(option_labels),
            key=f"exam_ans_{user_id}_{course_key}_{current_index}",
        )
        student_submit = [option_labels[label] for label in selected_multi]
    else:
        student_submit = st.text_area(
            "写出你的解答过程（包括必要计算/结论）",
            key=f"exam_ans_{user_id}_{course_key}_{current_index}",
        )
    submit_cols = st.columns(2)
    submitted = submit_cols[0].form_submit_button(
        "提交本题",
        icon=":material/send:",
        type="primary",
        width="stretch",
    )
    gave_up = submit_cols[1].form_submit_button(
        "我不会，查看解题思路",
        icon=":material/help:",
        width="stretch",
    )

if gave_up:
    # 确定不会：立即给出解题思路与正确答案，并把考点加入未掌握清单
    result = given_up_result(question)
    result["submitted_at"] = datetime.now().isoformat(timespec="seconds")
    knowledge = [
        name
        for name in question.get("knowledge_points", [])
        if name and name != "出题失败"
    ]
    if knowledge:
        for name in knowledge:
            store.upsert_weak_point(
                user_id,
                course_key,
                name,
                reason="考生标记不会，未掌握该考点。",
                related_question=question["stem"][:200],
            )
        st.toast(f"已将 {len(knowledge)} 个知识点加入待复习清单")
    exam["results"].append(result)
    _save_draft()
    show_result_and_controls(result, current_index, plan, exam)
    st.stop()

if submitted:
    if is_subjective_type(question["type"]) and not str(student_submit or "").strip():
        st.error("主观题请先填写答案再提交。", icon=":material/error:")
        st.stop()
    try:
        if is_subjective_type(question["type"]):
            with st.status("AI 阅卷中…", expanded=True) as judge_status:
                verdict = judge_subjective(
                    question,
                    str(student_submit or ""),
                    llm_overrides=st.session_state.get("llm_overrides"),
                )
                judge_status.update(label="阅卷完成", state="complete")
            is_correct = bool(verdict["is_correct"])
            result = {
                "question": question,
                "student_answer": student_submit,
                "student_answer_display": format_student_answer(question, student_submit),
                "is_correct": is_correct,
                "judge": verdict,
                "submitted_at": datetime.now().isoformat(timespec="seconds"),
            }
        else:
            is_correct, display = evaluate_choice_answer(question, student_submit)
            result = {
                "question": question,
                "student_answer": student_submit,
                "student_answer_display": display,
                "is_correct": is_correct,
                "judge": None,
                "submitted_at": datetime.now().isoformat(timespec="seconds"),
            }
        exam["results"].append(result)
        _save_draft()
    except Exception as exc:
        st.error(f"本题处理失败：{exc}", icon=":material/error:")
        st.stop()

    show_result_and_controls(result, current_index, plan, exam)
