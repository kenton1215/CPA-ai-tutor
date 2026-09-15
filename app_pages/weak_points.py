"""未掌握知识点：按当前学生展示诊断结果 + AI 摘要/测验/记忆卡片复习。

所有写入都绑定 user_id，因此两个学生登录后看到的弱项清单完全隔离。
"""

from __future__ import annotations

import html
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from app_pages._helpers import current_user_id, page_guide, require_user
from cpa_tutor.courses import course_by_key
from cpa_tutor.exam_engine import (
    card_messages,
    choose_mindmap_format,
    examples_messages,
    lesson_messages,
    mindmap_messages,
    normalize_question,
    quiz_messages,
    review_qa_messages,
    summary_messages,
)
from cpa_tutor.knowledge import best_retriever
from cpa_tutor.llm import LLMError, chat_completion, chat_json
from cpa_tutor.weak_points import WeakPointStore


def render_mindmap(raw: Any) -> None:
    """渲染思维导图：Mermaid 用前端组件画图，否则降级纯文本树。"""
    mindmap_format = choose_mindmap_format(raw)
    if mindmap_format == "mermaid":
        raw_code = str((raw or {}).get("content") or "")
        safe_code = html.escape(raw_code)
        components.html(
            """
            <script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
            <style>
                body { margin: 0; }
                .mermaid svg { max-width: 100%; height: auto; }
            </style>
            <pre class="mermaid">"""
            + safe_code
            + """</pre>
            <script>mermaid.initialize({ startOnLoad: true, securityLevel: 'strict' });</script>
            """,
            height=520,
            scrolling=True,
        )
        with st.expander("查看思维导图源码（Mermaid）", icon=":material/code:"):
            st.code(raw_code, language="mermaid")
    else:
        st.code(str((raw or {}).get("content") or ""))

require_user()
user_id = current_user_id()

course_key = st.session_state.get("selected_course_key") or "accounting"
course = course_by_key(course_key)
store = WeakPointStore()

st.title("未掌握知识点", icon=":material/psychology:")
st.markdown(f"当前科目：**《{course.name}》** · 仅显示属于你账号的记录")

page_guide(
    "未掌握知识点页操作指引",
    [
        "列表只显示你在当前科目答错过、且 AI 诊断出的知识点；",
        "选中一个知识点后，可生成：知识点摘要 / 详细讲解 / 思维导图 / 应用举例 / 巩固测验 / 记忆卡片；",
        "测验答错会重新累计错误次数；连续答对会自动标记为已掌握；",
        "也可手动点“标记为已掌握”；以后再次答错会自动回到待巩固清单。",
    ],
)

stats = store.stats(user_id, course_key)
col_active, col_done, col_total = st.columns(3)
with col_active.container(border=True):
    st.metric("待巩固", stats["active"])
with col_done.container(border=True):
    st.metric("已掌握", stats["mastered"])
with col_total.container(border=True):
    st.metric("累计记录", stats["active"] + stats["mastered"])

records = store.list_weak_points(user_id, course_key)
if not records:
    st.info(
        "当前科目还没有未掌握知识点记录。完成一次模拟考试后，"
        "AI 会把答错题目对应的知识点自动沉淀到这个清单里。",
        icon=":material/psychology:",
    )
    st.stop()

filter_col, search_col = st.columns([1, 1.6], vertical_alignment="bottom")
status_filter = filter_col.segmented_control(
    "状态筛选",
    options=["未掌握", "已掌握"],
    default="未掌握",
    key=f"wp_filter_{user_id}_{course_key}",
)
search_text = search_col.text_input(
    "搜索知识点",
    placeholder="输入关键词…",
    key=f"wp_search_{user_id}_{course_key}",
    label_visibility="collapsed",
)

filtered = store.list_weak_points(
    user_id,
    course_key,
    mastered=0 if status_filter == "未掌握" else 1,
    search=search_text,
)
if not filtered:
    st.caption("没有符合筛选条件的知识点。")
    st.stop()

options = [f"{item['knowledge_point']}（错 {item['wrong_count']} 次）" for item in filtered]
selected_label = st.selectbox(
    "选择要复习的知识点",
    options=options,
    key=f"wp_select_{user_id}_{course_key}",
)
selected_index = options.index(selected_label)
point = filtered[selected_index]
point_id = point["id"]

info_cols = st.columns(4)
info_cols[0].caption(f"首次出错：{point['first_seen_at'][:10]}")
info_cols[1].caption(f"最近出错：{point['last_wrong_at'][:16]}")
info_cols[2].caption(f"出错次数：{point['wrong_count']}")
info_cols[3].caption(f"状态：{'未掌握' if not point['mastered'] else '已掌握'}")
if point.get("suggestion"):
    st.info(f"教练建议：{point['suggestion']}", icon=":material/tips_and_updates:")
if point.get("reason"):
    with st.expander("查看错因诊断", icon=":material/health_and_safety:"):
        st.write(point["reason"])

st.subheader(f"AI 教练带你巩固：{point['knowledge_point']}")

content_key = f"wp_content_{user_id}_{point_id}"
st.session_state.setdefault(content_key, {})
content = st.session_state[content_key]


def refresh_retriever():
    return best_retriever(course_key)


# 首页“一键复习”推送：自动选中该知识点并自动生成摘要（点击即到复习卡片）
auto_gen = st.session_state.pop("wp_auto_gen", None)
auto_summary = bool(
    auto_gen
    and int(auto_gen.get("user_id") or 0) == user_id
    and auto_gen.get("course_key") == course_key
    and int(auto_gen.get("point_id") or 0) == point_id
    and auto_gen.get("kind") == "summary"
)

action_cols = st.columns(4)
want_summary = (
    action_cols[0].button(
        "生成知识点摘要",
        icon=":material/summarize:",
        key=f"btn_summary_{user_id}_{point_id}",
    )
    or auto_summary
)
want_lesson = action_cols[1].button(
    "生成详细讲解", icon=":material/menu_book:", key=f"btn_lesson_{user_id}_{point_id}"
)
want_mindmap = action_cols[2].button(
    "生成思维导图", icon=":material/account_tree:", key=f"btn_mindmap_{user_id}_{point_id}"
)
want_examples = action_cols[3].button(
    "生成应用举例", icon=":material/work:", key=f"btn_examples_{user_id}_{point_id}"
)

action_cols2 = st.columns(4)
want_quiz = action_cols2[0].button(
    "生成测验", icon=":material/quiz:", key=f"btn_quiz_{user_id}_{point_id}"
)
want_cards = action_cols2[1].button(
    "生成记忆卡片", icon=":material/style:", key=f"btn_cards_{user_id}_{point_id}"
)
mark_done = action_cols2[2].button(
    "标记为已掌握",
    icon=":material/check_circle:",
    type="primary",
    key=f"btn_master_{user_id}_{point_id}",
    help="掌握后再次答错会自动重新进入未掌握清单。",
)

if mark_done:
    store.mark_mastered(user_id, point_id, mastered=True)
    st.toast("已标记掌握，继续加油！")
    st.rerun()

retriever = None
if want_summary or want_quiz or want_cards or want_lesson or want_mindmap or want_examples:
    with st.status("准备课程知识素材…", type="compact") as prep_status:
        retriever = refresh_retriever()
        prep_status.update(label="素材准备完成", state="complete")

if want_summary:
    try:
        with st.status("AI 教练撰写摘要…", expanded=False) as run_status:
            messages = summary_messages(
                course_key,
                point["knowledge_point"],
                point.get("reason", ""),
                retriever=retriever,
            )
            content["summary"] = chat_completion(messages, temperature=0.3)
            run_status.update(label="摘要生成完成", state="complete")
    except LLMError as exc:
        st.error(str(exc), icon=":material/error:")

if content.get("summary"):
    st.markdown("#### :material/summarize: 知识点摘要")
    with st.container(border=True):
        st.markdown(content["summary"])

if want_lesson:
    try:
        with st.status("AI 教练撰写详细讲解…", expanded=False) as run_status:
            messages = lesson_messages(
                course_key,
                point["knowledge_point"],
                point.get("reason", ""),
                retriever=retriever,
            )
            content["lesson"] = chat_completion(messages, temperature=0.3)
            run_status.update(label="详细讲解生成完成", state="complete")
    except LLMError as exc:
        st.error(str(exc), icon=":material/error:")

if content.get("lesson"):
    st.markdown("#### :material/menu_book: 详细讲解")
    with st.container(border=True):
        st.markdown(content["lesson"])

if want_mindmap:
    try:
        with st.status("AI 教练绘制思维导图…", expanded=False) as run_status:
            messages = mindmap_messages(
                course_key,
                point["knowledge_point"],
                point.get("reason", ""),
                retriever=retriever,
            )
            content["mindmap"] = chat_json(messages, temperature=0.3)
            run_status.update(label="思维导图生成完成", state="complete")
    except (LLMError, ValueError) as exc:
        st.error(f"思维导图生成失败：{exc}", icon=":material/error:")

if content.get("mindmap"):
    st.markdown("#### :material/account_tree: 思维导图")
    st.caption("网络正常时以图形渲染；离线或模型输出异常时自动降级为文本树。")
    render_mindmap(content["mindmap"])

if want_examples:
    try:
        with st.status("AI 教练编写应用举例…", expanded=False) as run_status:
            messages = examples_messages(
                course_key,
                point["knowledge_point"],
                point.get("reason", ""),
                retriever=retriever,
            )
            raw_examples = chat_json(messages, temperature=0.3)
            content["examples"] = [
                {
                    "title": str(item.get("title") or f"例题 {index + 1}"),
                    "question": str(item.get("question") or ""),
                    "solution": str(item.get("solution") or ""),
                    "key_points": item.get("key_points") or [],
                }
                for index, item in enumerate(raw_examples or [])
                if isinstance(item, dict) and item.get("question")
            ]
            run_status.update(label="应用举例生成完成", state="complete")
    except (LLMError, ValueError) as exc:
        st.error(f"应用举例生成失败：{exc}", icon=":material/error:")

if content.get("examples"):
    st.markdown("#### :material/work: 应用举例")
    for example in content["examples"]:
        with st.expander(example["title"], icon=":material/lightbulb:"):
            st.markdown("**题目**")
            st.markdown(example["question"])
            st.markdown("**解答**")
            st.markdown(example["solution"])
            if example.get("key_points"):
                st.caption("考点：" + "、".join(example["key_points"]))

if want_quiz:
    try:
        with st.status("AI 教练命制测验题…", expanded=False) as run_status:
            messages = quiz_messages(
                course_key, point["knowledge_point"], question_count=4, retriever=retriever
            )
            raw_quiz = chat_json(messages, temperature=0.25)
            content["quiz"] = [
                normalize_question(item, "single", index=idx + 1, chapter=point["knowledge_point"])
                for idx, item in enumerate(raw_quiz or [])
            ]
            run_status.update(label="测验生成完成", state="complete")
    except (LLMError, ValueError) as exc:
        st.error(f"测验生成失败：{exc}", icon=":material/error:")

if content.get("quiz"):
    quiz = content["quiz"]
    st.markdown("#### :material/quiz: 巩固测验")
    st.caption("每道题选择一个答案后点击“提交测验”，系统即时判分并给出解析。")
    with st.form(f"wp_quiz_form_{user_id}_{point_id}"):
        answers: dict[int, str] = {}
        for question in quiz:
            option_labels = {
                f"{letter}. {text}": letter
                for letter, text in question["options"].items()
            }
            selection = st.radio(
                f"{question['index']}. {question['stem']}",
                options=list(option_labels),
                key=f"wp_q_{user_id}_{point_id}_{question['index']}",
                label_visibility="visible",
            )
            if selection:
                answers[question["index"]] = option_labels[selection]
        submitted = st.form_submit_button("提交测验", icon=":material/assignment_turned_in:")

    if submitted:
        for question in quiz:
            student_letter = answers.get(question["index"], "")
            expected_letter = str(question["answer"]).upper()
            correct = student_letter == expected_letter
            updated = store.record_review_result(
                user_id,
                course_key,
                (question["knowledge_points"] or [point["knowledge_point"]])[0],
                correct,
                reason="弱项巩固测验答错，说明该知识点仍需加强。",
                related_question=question["stem"][:200],
            )
            if correct and updated and updated.get("mastered"):
                st.toast(f"连续答对，已自动标记掌握：{updated['knowledge_point']}！")
            with st.container(border=True):
                st.markdown(
                    f"**第 {question['index']} 题**"
                    f"{'：:green-badge[答对]' if correct else '：:red-badge[答错]'}"
                )
                if correct:
                    st.write(f"你的答案：{expected_letter} ✓")
                else:
                    st.write(f"你的答案：{student_letter or '未作答'}；正确答案：{expected_letter}")
                with st.expander("查看解析"):
                    st.markdown(question["analysis"])
                    st.caption("该题考点：" + "、".join(question.get("knowledge_points", [])))
        if any(answers.get(q["index"]) != str(q["answer"]).upper() for q in quiz):
            st.toast("答错的题已计入未掌握清单，连续答对会自动移除。")

if want_cards:
    try:
        with st.status("AI 教练整理记忆卡片…", expanded=False) as run_status:
            messages = card_messages(
                course_key, point["knowledge_point"], card_count=5, retriever=retriever
            )
            raw_cards = chat_json(messages, temperature=0.35)
            content["cards"] = [
                {
                    "front": str(item.get("front", "")),
                    "back": str(item.get("back", "")),
                    "hint": str(item.get("hint", "")),
                }
                for item in (raw_cards or [])
                if isinstance(item, dict) and item.get("front")
            ]
            run_status.update(label="记忆卡片生成完成", state="complete")
    except (LLMError, ValueError) as exc:
        st.error(f"记忆卡片生成失败：{exc}", icon=":material/error:")

if content.get("cards"):
    cards = content["cards"]
    st.markdown("#### :material/style: 记忆卡片")
    card_names = [f"卡片 {i + 1}" for i in range(len(cards))]
    owner_key = f"card_owner_{user_id}"
    if "card_idx" not in st.session_state or st.session_state.get(owner_key) != point_id:
        st.session_state["card_idx"] = 0
        st.session_state[owner_key] = point_id
    selected_card_name = st.radio(
        "选择卡片",
        options=card_names,
        horizontal=True,
        key=f"card_pick_{user_id}_{point_id}",
    )
    card_index = card_names.index(selected_card_name)
    card = cards[card_index]
    with st.container(border=True, height=210):
        st.markdown(f"**:material/help: {card['front']}**")
        show_back = st.toggle(
            "显示背面答案",
            key=f"card_show_{user_id}_{point_id}_{card_index}",
        )
        if show_back:
            st.markdown(f":material/lightbulb: {card['back']}")
            if card.get("hint"):
                st.caption(f"记忆提示：{card['hint']}")

st.markdown("#### :material/forum: 继续向教练追问")
chat_key = f"wp_chat_{user_id}_{point_id}"
st.session_state.setdefault(chat_key, [])
for message in st.session_state[chat_key]:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input(
    f"围绕“{point['knowledge_point']}”继续提问…",
    key=f"wp_chat_input_{user_id}_{point_id}",
    submit_mode="disable",
):
    st.session_state[chat_key].append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant", avatar=":material/school:"):
        try:
            retriever = refresh_retriever()
            messages = review_qa_messages(
                course_key,
                point["knowledge_point"],
                st.session_state[chat_key][:-1],
                prompt,
                retriever=retriever,
            )
            response = chat_completion(messages, temperature=0.3)
            st.markdown(response)
            st.session_state[chat_key].append(
                {"role": "assistant", "content": response}
            )
        except LLMError as exc:
            st.error(str(exc), icon=":material/error:")

if st.button(
    "删除该知识点记录",
    icon=":material/delete:",
    key=f"wp_delete_{user_id}_{point_id}",
):
    store.delete_weak_point(user_id, point_id)
    st.toast("记录已删除")
    st.rerun()
