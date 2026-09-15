"""教材研读：AI 教练答疑 + 教材章节阅读。"""

from __future__ import annotations

import streamlit as st

from app_pages._helpers import current_user_id, page_guide
from cpa_tutor.config import llm_config
from cpa_tutor.courses import course_by_key, source_count_labels
from cpa_tutor.exam_engine import general_qa_messages
from cpa_tutor.knowledge import (
    best_retriever,
    build_chapter_index,
    compile_review_text,
    course_status,
    load_course_chunks,
)
from cpa_tutor.llm import LLMError, chat_completion

course_key = st.session_state.get("selected_course_key") or "accounting"
course = course_by_key(course_key)
status = course_status(course_key)
user_id = current_user_id()

st.title(f":material/menu_book: {course.name} · 教材研读")
st.caption(
    f"课程资料：{source_count_labels(status['files']) if status['files'] else '暂无 PDF'}"
)

page_guide(
    "教材研读页操作指引",
    [
        "「AI 教练答疑」：把看不懂的概念、例题直接打字提问，教练会先检索该课程知识库再讲解；",
        "想换一类问题可点上方“试试这样问”的快捷短语；",
        "「教材阅读」：选择章节和单次阅读量，点击下载按钮可把 PDF 原文存到手机/电脑；",
        "如果教材显示为扫描版无法检索，请到「知识库与设置」开启 OCR 或使用带文字层的 PDF。",
    ],
)

chat_key = f"review_chat_{user_id}_{course_key}"

tab_ask, tab_read = st.tabs([":material/forum: AI 教练答疑", ":material/article: 教材阅读"])


with tab_ask:
    st.markdown(
        "把教材研读中不懂的地方直接问教练。教练会先检索该课程知识库，"
        "再结合 CPA 知识体系讲解；可追问“再出一道同考点例题”。"
    )
    st.session_state.setdefault(chat_key, [])

    suggestions = {
        "请帮我梳理本章框架与高频考点": "请先介绍这门课的整体框架，并说明当前最常考的高频考点分布。",
        "讲一个计算/政策要点": "请结合近十年真题，选一个高频且容易出错的考点举例讲解。",
        "给我出几道辨析题": "请出三道针对易混淆概念的单选题，并附解析。",
    }

    if not st.session_state[chat_key]:
        chosen = st.pills(
            "试试这样问",
            options=list(suggestions),
            label_visibility="collapsed",
        )
        if chosen:
            st.session_state[chat_key].append(
                {"role": "user", "content": suggestions[chosen]}
            )
            st.rerun()

    for message in st.session_state[chat_key]:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    if prompt := st.chat_input(
        f"向《{course.name}》AI 教练提问…",
        key=f"{chat_key}_input",
        submit_mode="disable",
    ):
        st.session_state[chat_key].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant", avatar=":material/school:"):
            try:
                with st.status("检索课程知识库…", type="step") as retrieve_status:
                    retriever = best_retriever(course_key)
                    retrieve_status.update(label="检索完成", state="complete")
                with st.status("AI 教练组织讲解…", type="step") as think_status:
                    messages = general_qa_messages(
                        course_key,
                        st.session_state[chat_key][:-1],
                        prompt,
                        retriever=retriever,
                    )
                    response = chat_completion(messages, temperature=0.3)
                    think_status.update(label="讲解完成", state="complete")
                st.markdown(response)
                st.session_state[chat_key].append(
                    {"role": "assistant", "content": response}
                )
            except LLMError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"AI 答疑失败：{exc}")

    if st.session_state[chat_key]:
        with st.container(horizontal=True, horizontal_alignment="right"):
            st.button(
                "清空当前科目对话",
                icon=":material/delete_sweep:",
                on_click=lambda: st.session_state.pop(chat_key, None),
            )


with tab_read:
    if not status["files"]:
        st.warning("该课程尚未放置 PDF 资料。请将教材/真题 PDF 放入 data/cpa_docs 后刷新。")
        st.stop()

    for file in status["files"]:
        try:
            file_bytes = file.path.read_bytes()
        except OSError:
            continue
        with st.container(border=True):
            col_name, col_meta, col_btn = st.columns([3, 1.4, 1.2], vertical_alignment="center")
            col_name.markdown(f"**{file.name}**")
            col_meta.caption(f"{file.kind_label} · {file.size_mb} MB")
            col_btn.download_button(
                "下载原文 PDF",
                data=file_bytes,
                file_name=file.name,
                mime="application/pdf",
                key=f"download_{course_key}_{file.name}",
            )

    if not status["has_cache"]:
        st.info(
            "当前课程还没有本地文本缓存。请前往“知识库与设置”页点击“解析 PDF（本地缓存）”。"
            "若 PDF 为扫描版且未开启 OCR，将只能阅读/下载原文，无法做向量检索。"
        )
    else:
        chunks = load_course_chunks(course_key)
        chapters = build_chapter_index(chunks)
        chapter_labels = [item["chapter"] for item in chapters]
        with st.container(border=True):
            st.subheader("章节浏览")
            col_chapter, col_continue = st.columns([3, 1], vertical_alignment="bottom")
            chapter_label = col_chapter.selectbox(
                "选择章节",
                options=chapter_labels,
                key=f"read_chapter_{course_key}",
            )
            continue_chars = col_continue.selectbox(
                "单次阅读量",
                options=[1600, 2600, 4200],
                format_func=lambda n: f"约 {n} 字",
                key=f"read_chars_{course_key}",
            )
            content = compile_review_text(chapters, chapter_label, max_chars=int(continue_chars))
            with st.container(border=True, height=520):
                if content:
                    st.markdown(content)
                else:
                    st.caption("该章节暂无可阅读文本（可能为扫描页）。")
            st.caption(
                "提示：扫描版教材建议先下载原文阅读；文本分块来自可解析的 PDF 内容，可能混有真题题干。"
            )
