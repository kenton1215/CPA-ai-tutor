"""知识库与设置：模型配置、PDF 上传、解析与向量索引。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

from app_pages._helpers import page_guide, require_developer
from cpa_tutor.config import (
    CACHE_DIR,
    CHROMA_DIR,
    DATA_DIR,
    describe_llm,
    embedding_config,
    llm_config,
)
from cpa_tutor.courses import COURSES, COURSE_KEYS, course_by_key
from cpa_tutor.exam_engine import TYPE_LABELS, parse_bank_batch
from cpa_tutor.knowledge import (
    build_vector_index,
    course_status,
    delete_vector_index,
    load_course_chunks,
    make_embedder,
    readable_ratio_label,
    refresh_course_cache,
)
from cpa_tutor.question_bank import QuestionBank

course_key = st.session_state.get("selected_course_key") or "accounting"
course = course_by_key(course_key)

require_developer()

st.title(":material/tune: 知识库与设置")

page_guide(
    "知识库与设置页操作指引",
    [
        "「模型设置」：显示当前对话/向量模型；首次使用可先在 .env 配置，也可在本页临时填写并应用；",
        "「知识库管理」：先点“① 解析 PDF（本地缓存）”，再点“② 建立 / 更新向量索引”；",
        "上传的补充讲义会自动保存到当前科目的资料目录，上传后需要重新解析；",
        "扫描版教材没有文字层时，可开启 OCR（需电脑安装 Tesseract 与 chi_sim）后重新解析。",
    ],
)

tab_llm, tab_kb, tab_bank = st.tabs(
    [
        ":material/settings_input_component: 模型设置",
        ":material/database: 知识库管理",
        ":material/library_add: 真题题库",
    ]
)


with tab_llm:
    st.subheader("对话模型（Agent 大脑）")
    llm_cfg = llm_config()
    if llm_cfg["available"]:
        st.success(f"当前：{describe_llm(llm_cfg)}")
    else:
        st.warning("当前没有可用的对话模型配置。")

    st.markdown(
        """
        **推荐配置方式**：在项目根目录 `.env` 中填写（参见 `.env.example`）：
        - `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`；
        - 也可直接使用已检测到的 **DashScope（通义 qwen）**、**DeepSeek** 或 **OpenAI** Key；
        - 未配置任何 Key 时，程序会尝试连接本地 **Ollama**（`http://127.0.0.1:11434/v1`）。
        """
    )

    with st.form("llm_override_form", border=True):
        st.caption("以下临时覆盖只在当前浏览器会话生效（不会写入 .env）：")
        override_base = st.text_input("接口地址 Base URL", placeholder="https://…/v1")
        override_key = st.text_input("API Key", type="password")
        override_model = st.text_input("模型名称", placeholder="qwen-plus / deepseek-chat / gpt-4o-mini / qwen2.5:7b")
        override_temp = st.slider("生成温度", min_value=0.0, max_value=1.5, value=0.25, step=0.05)
        submitted = st.form_submit_button("应用临时模型设置", icon=":material/check:")
    if submitted:
        st.session_state["llm_overrides"] = {
            "base_url": override_base.strip(),
            "api_key": override_key.strip(),
            "model": override_model.strip(),
            "temperature": override_temp,
        }
        new_cfg = llm_config(st.session_state["llm_overrides"])
        if new_cfg["available"]:
            st.success(f"已应用：{describe_llm(new_cfg)}")
        else:
            st.warning("填写不完整，尚未应用成功。")
    if st.session_state.get("llm_overrides"):
        if st.button("清除临时模型设置", icon=":material/restart_alt:"):
            del st.session_state["llm_overrides"]
            st.rerun()

    st.divider()
    st.subheader("Embedding（向量化）")
    embed_cfg = embedding_config()
    provider_label = "远程 API（OpenAI 兼容）" if embed_cfg["provider"] == "api" else "本地 sentence-transformers"
    st.write(f"当前方式：**{provider_label}**，模型：`{embed_cfg['model']}`")
    if embed_cfg["provider"] == "api" and not embed_cfg["api_key"]:
        st.warning("远程向量化缺少 API Key，建立向量索引前请先在 .env 配置 DASHSCOPE_API_KEY / OPENAI_API_KEY。")
    st.caption(
        "没有向量模型也能使用：应用会自动回退到本地关键词检索（覆盖已解析的教材/真题文本）。"
    )


with tab_kb:
    st.subheader("六门课程知识库概览")
    overview_rows = []
    for key in COURSE_KEYS:
        status = course_status(key)
        overview_rows.append(
            {
                "科目": COURSES[key].name,
                "PDF 资料": len(status["files"]),
                "文本分块": status["chunk_count"],
                "向量片段": status["vector_count"],
                "文本可读性": readable_ratio_label(status),
            }
        )
    st.dataframe(overview_rows, hide_index=True, width="stretch")

    st.divider()
    st.subheader(f"当前科目：{course.name}")

    current = course_status(course_key)
    if current["files"]:
        for file in current["files"]:
            st.caption(f"· {file.name}（{file.kind_label}，{file.size_mb} MB）")
    else:
        st.caption("该科目目录下暂无 PDF。")

    with st.container(border=True):
        st.markdown("**文本缓存**")
        ocr_enabled = st.toggle(
            "扫描版 PDF 启用 OCR（需电脑安装 Tesseract 与 chi_sim 语言包；速度较慢）",
            value=bool(st.session_state.get("ocr_enabled", False)),
            key="ocr_enabled",
        )
        col_parse, col_index = st.columns(2)
        parse_clicked = col_parse.button(
            "① 解析 PDF（本地缓存）",
            icon=":material/file_copy:",
            help="从 PDF 提取文本并分块缓存，不调用任何外部接口。",
        )
        index_clicked = col_index.button(
            "② 建立 / 更新向量索引",
            icon=":material/route:",
            help="需要已完成的本地文本缓存和可用的 Embedding。",
            disabled=not current["has_cache"],
        )

        if parse_clicked:
            with st.status("正在解析 PDF…", expanded=True) as parse_status:
                def parse_progress(label: str, ratio: float) -> None:
                    parse_status.update(
                        label=f"{label}　{int(ratio * 100)}%",
                        state="running" if ratio < 1 else "complete",
                    )

                result = refresh_course_cache(course_key, ocr=ocr_enabled, progress=parse_progress)
            if result["ok"]:
                st.success(f"解析完成：新增 {result['meta']['chunk_total']} 个文本分块。")
                st.rerun()
            else:
                st.error(result["message"])

        if index_clicked:
            if not current["has_cache"]:
                st.warning("请先执行“解析 PDF（本地缓存）”。")
            else:
                try:
                    embedder = make_embedder(embedding_config())
                except Exception as exc:
                    st.error(f"Embedding 初始化失败：{exc}")
                    embedder = None
                if embedder is not None:
                    with st.status("正在建立向量索引…", expanded=True) as index_status:
                        def index_progress(label: str, ratio: float) -> None:
                            index_status.update(
                                label=f"{label}　{int(ratio * 100)}%",
                                state="running" if ratio < 1 else "complete",
                            )

                        result = build_vector_index(
                            course_key,
                            embedder=embedder,
                            progress=index_progress,
                        )
                    if result["ok"]:
                        message = (
                            f"向量索引完成：本次新增 {result['added']}，"
                            f"跳过 {result['skipped']}，当前共 {result['total']} 条。"
                        )
                        if result.get("note"):
                            message += f"（{result['note']}）"
                        st.success(message)
                    else:
                        st.error(result["message"])

    with st.container(border=True):
        st.markdown("**上传 PDF 到当前科目**")
        uploader_key = f"upload_{course_key}"
        uploaded = st.file_uploader(
            "选择 PDF 文件（教材、真题或补充讲义）",
            type=["pdf"],
            accept_multiple_files=True,
            key=uploader_key,
        )
        if uploaded and st.button(
            "保存并刷新文件列表",
            icon=":material/upload_file:",
            key=f"save_upload_{course_key}",
        ):
            target_dir = DATA_DIR / course_key
            target_dir.mkdir(parents=True, exist_ok=True)
            saved = []
            for file in uploaded:
                safe_name = Path(file.name).name
                target = target_dir / safe_name
                if target.exists():
                    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    target = target_dir / f"{target.stem}_{stamp}.pdf"
                target.write_bytes(file.getbuffer())
                saved.append(target.name)
            st.success("已保存：" + "、".join(saved))
            st.info("上传后请点击“① 解析 PDF（本地缓存）”以纳入本课程知识库。")

    with st.expander("高级：清理当前科目的缓存/索引"):
        st.caption("该操作只删除本应用自动生成的文件，不会删除 data/cpa_docs 下的原始 PDF。")
        col_clear_cache, col_clear_vector = st.columns(2)
        if col_clear_cache.button("删除文本缓存", icon=":material/cleaning_services:"):
            cache_file = CACHE_DIR / f"{course_key}.json.gz"
            if cache_file.exists():
                cache_file.unlink()
            st.success("文本缓存已删除。")
            st.rerun()
        if col_clear_vector.button("删除向量索引", icon=":material/cleaning_services:"):
            delete_vector_index(course_key)
            st.success("向量索引已删除。")
            st.rerun()

    st.divider()
    st.markdown(
        f"""
        **资料存放路径**：`{DATA_DIR}`（根目录按文件名自动识别科目；
        `{DATA_DIR / '<科目key>'}` 子目录存放上传资料）。

        应用生成文件：文本缓存位于 `{CACHE_DIR}`，向量索引位于 `{CHROMA_DIR}`，
        学习记录（弱项/模考）与真题题库位于 `data/cpa_user.db`。
        """
    )


with tab_bank:
    st.subheader("真题题库（模拟考试优先从这里选题）")
    bank = QuestionBank()
    overview_rows = []
    for key in COURSE_KEYS:
        stats = bank.bank_stats(key)
        type_text = (
            "、".join(f"{TYPE_LABELS.get(t, t)}×{n}" for t, n in stats["by_type"].items())
            or "—"
        )
        overview_rows.append(
            {
                "科目": COURSES[key].name,
                "可用题数": stats["total"],
                "题型分布": type_text,
            }
        )
    st.dataframe(overview_rows, hide_index=True, width="stretch")
    st.caption(
        "题库为空时，模拟考试会由 AI 参照教材与十年命题规律现场生成题目；"
        "解析入库后优先使用真题原题/改编题，组卷更快、更贴近真题。"
    )

    st.divider()
    st.subheader(f"当前科目：{course.name} · AI 批量解析真题入库")
    paper_chunks: list[dict] = []
    if course_status(course_key)["has_cache"]:
        try:
            paper_chunks = [
                chunk
                for chunk in load_course_chunks(course_key)
                if chunk.get("kind") == "真题"
            ]
        except Exception:
            paper_chunks = []
    if not paper_chunks:
        st.warning(
            "当前科目还没有解析出“真题”文本分块。请先在「知识库管理」页执行"
            "“① 解析 PDF（本地缓存）”，确保真题 PDF 已放入资料目录。",
            icon=":material/info:",
        )
    else:
        st.caption(f"共 {len(paper_chunks)} 个真题文本分块可用于解析（只解析可读文本）。")
        with st.form("bank_parse_form", border=True):
            parse_source = st.text_input(
                "来源标注", value="十年真题", help="写入题库的 source 字段，便于追溯"
            )
            parse_year = st.number_input(
                "年份（0 = 不标注；单个文件含多年份时建议填 0）",
                min_value=0,
                max_value=2100,
                value=0,
                step=1,
            )
            parse_clicked = st.form_submit_button(
                "开始解析入库",
                icon=":material/auto_awesome:",
                type="primary",
            )
        if parse_clicked:
            with st.status("AI 正在逐批提取题目…", expanded=True) as parse_status:
                def _bank_progress(done: int, total: int) -> None:
                    parse_status.update(
                        label=f"已处理 {done}/{total} 批", state="running"
                    )

                try:
                    questions, parse_stats = parse_bank_batch(
                        course_key,
                        paper_chunks,
                        year=int(parse_year) if parse_year else None,
                        llm_overrides=st.session_state.get("llm_overrides"),
                        progress=_bank_progress,
                    )
                except Exception as exc:
                    questions, parse_stats = [], {"batches": 0, "valid": 0, "invalid": 0}
                    st.error(f"解析失败：{exc}")
            if questions:
                inserted, skipped = bank.insert_questions(
                    course_key,
                    questions,
                    source=parse_source.strip() or "真题",
                    year=int(parse_year) if parse_year else None,
                )
                st.success(
                    f"解析完成：共 {parse_stats['batches']} 批，有效题 {parse_stats['valid']}，"
                    f"新增入库 {inserted}，重复跳过 {skipped}，格式不合法丢弃 {parse_stats['invalid']}。"
                )
            else:
                st.error(
                    "没有解析出有效题目，请检查真题 PDF 文本质量或对话模型配置。",
                    icon=":material/error:",
                )

    st.divider()
    st.subheader("题库校对与启停")
    bank_search = st.text_input("搜索题干 / 章节 / 考点", key="bank_search")
    bank_rows = bank.list_bank(course_key, search=bank_search, limit=30)
    if not bank_rows:
        st.caption("当前筛选下没有题目；解析入库后可在此校对、停用、删除。")
    else:
        st.caption(
            f"显示最近 {len(bank_rows)} 条（本科目可用题共 {bank.bank_stats(course_key)['total']} 道）"
        )
        for question in bank_rows:
            status_badge = ":green-badge[启用]" if question["active"] else ":gray-badge[已停用]"
            with st.expander(
                f"#{question['id']} {status_badge} "
                f"{TYPE_LABELS.get(question['type'], question['type'])}　"
                f"{question['stem'][:50]}…",
                expanded=False,
            ):
                st.markdown(question["stem"])
                if question["options"]:
                    st.caption(
                        "选项：" + "；".join(
                            f"{k}. {v}" for k, v in question["options"].items()
                        )
                    )
                st.caption(
                    f"答案：{question['answer']}　"
                    f"考点：{'、'.join(question['knowledge_points']) or '无'}　"
                    f"章节：{question['chapter'] or '未标注'}　"
                    f"年份：{question['year'] or '未标注'}　来源：{question['source'] or '—'}"
                )
                st.markdown(question["analysis"][:400])
                col_on, col_del = st.columns(2)
                if question["active"]:
                    if col_on.button(
                        "停用（组卷不再使用）",
                        icon=":material/block:",
                        key=f"bank_off_{question['id']}",
                    ):
                        bank.set_active(question["id"], False)
                        st.rerun()
                else:
                    if col_on.button(
                        "重新启用",
                        icon=":material/check_circle:",
                        key=f"bank_on_{question['id']}",
                    ):
                        bank.set_active(question["id"], True)
                        st.rerun()
                if col_del.button(
                    "删除",
                    icon=":material/delete:",
                    key=f"bank_del_{question['id']}",
                ):
                    bank.delete_question(question["id"])
                    st.rerun()

    st.subheader("修正题目内容")
    editable_ids = [q["id"] for q in bank.list_bank(course_key, limit=50)]
    if editable_ids:
        edit_id = st.selectbox("选择题号（最近 50 条）", editable_ids, key="bank_edit_id")
        target = bank.get_question(edit_id)
        if target:
            with st.form("bank_edit_form", border=True):
                edit_type = st.selectbox(
                    "题型",
                    options=list(TYPE_LABELS),
                    format_func=lambda t: TYPE_LABELS[t],
                    index=list(TYPE_LABELS).index(target["type"])
                    if target["type"] in TYPE_LABELS
                    else 0,
                )
                edit_stem = st.text_area("题干", value=target["stem"], height=120)
                edit_answer = st.text_input(
                    "答案（多选填字母数组如 [\"A\",\"C\"]）",
                    value=str(target["answer"]),
                )
                edit_analysis = st.text_area(
                    "解析", value=target["analysis"], height=120
                )
                edit_chapter = st.text_input("章节", value=target["chapter"])
                edit_knowledge = st.text_input(
                    "考点（逗号分隔）", value="、".join(target["knowledge_points"])
                )
                save_edit = st.form_submit_button(
                    "保存修正", icon=":material/save:", type="primary"
                )
            if save_edit:
                knowledge_list = [
                    item.strip()
                    for item in edit_knowledge.replace("，", ",").split(",")
                    if item.strip()
                ]
                bank.update_question(
                    edit_id,
                    {
                        "type": edit_type,
                        "stem": edit_stem.strip(),
                        "answer": edit_answer.strip(),
                        "analysis": edit_analysis.strip(),
                        "chapter": edit_chapter.strip(),
                        "knowledge_points": knowledge_list,
                    },
                )
                st.success("已保存修正。")
                st.rerun()
    else:
        st.caption("题库为空，暂无题目可修正。")
