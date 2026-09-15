"""模拟考试出题、作答、判分与未掌握知识点分析。"""

from __future__ import annotations

import random
import re
from typing import Any

from cpa_tutor.courses import COURSES, Course, course_by_key
from cpa_tutor.exam_structure import CALC, CASE, COMP, SHORT
from cpa_tutor.knowledge import search_knowledge
from cpa_tutor.llm import LLMError, chat_completion, chat_json

SINGLE = "single"
MULTIPLE = "multiple"
SUBJECTIVE = "subjective"  # 兼容旧数据与旧组卷路径；新组卷使用下方细分题型

TYPE_LABELS = {
    SINGLE: "单项选择题",
    MULTIPLE: "多项选择题",
    CALC: "计算分析题",
    CASE: "案例分析题",
    SHORT: "简答题",
    COMP: "综合题",
    SUBJECTIVE: "主观题",
}

SUBJECTIVE_TYPE_SET = {SUBJECTIVE, CALC, CASE, SHORT, COMP}


def is_subjective_type(question_type: str) -> bool:
    """是否主观类题型（计算/案例/简答/综合/旧主观题）。"""
    return question_type in SUBJECTIVE_TYPE_SET

EXAMINER_SYSTEM = """你是拥有二十年教学经验的中国注册会计师（CPA）考试辅导专家与命题研究专家。
你非常熟悉各科目最近十年真题的命题风格、高频考点、易错陷阱与官方辅导教材的知识体系。
你出题严谨、表述规范，绝不出超纲或逻辑不自洽的题目，也绝不照抄材料中的原文题目。
"""

TUTOR_SYSTEM = """你是认真、耐心的 CPA 备考教练（Agent）。
你以帮助考生真正理解和掌握知识为目标，而不是只给答案。
回答时做到：条理清晰、先讲思路再给结论、必要时给出记忆口诀或对比表；
当引用教材/真题素材时请用 [资料] 标注来源；素材不足时坦诚说明，并基于你掌握的 CPA 知识补充讲解。
"""


def course(course_key: str) -> Course:
    return course_by_key(course_key) or COURSES["accounting"]


def type_label(question_type: str) -> str:
    return TYPE_LABELS.get(question_type, question_type)


# ---------------------------------------------------------------- 出题计划


def sample_exam_plan(
    total: int,
    *,
    single_ratio: float = 0.6,
    multiple_ratio: float = 0.25,
    subjective_ratio: float = 0.15,
    chapters: list[str] | None = None,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """随机生成试卷蓝图：每种题型的题量 + 每道题拟考查章节。"""
    total = max(1, min(int(total), 60))
    single_count = round(total * single_ratio)
    multiple_count = round(total * multiple_ratio)
    single_count = max(0, min(single_count, total))
    multiple_count = max(0, min(multiple_count, total - single_count))
    subjective_count = total - single_count - multiple_count
    types: list[str] = (
        [SINGLE] * single_count
        + [MULTIPLE] * multiple_count
        + [SUBJECTIVE] * subjective_count
    )
    rng = random.Random(seed)
    rng.shuffle(types)

    chapter_pool = [chapter for chapter in (chapters or []) if chapter and chapter != "未标注章节"]
    plan: list[dict[str, Any]] = []
    for index, qtype in enumerate(types, start=1):
        chapter = ""
        if chapter_pool:
            chapter = chapter_pool[index % len(chapter_pool)]
        plan.append({"index": index, "type": qtype, "chapter": chapter})
    return plan


def plan_summary(plan: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    scores: dict[str, float] = {}
    for item in plan:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
        # 旧组卷路径无分值字段，按每题 1 分展示
        scores[item["type"]] = scores.get(item["type"], 0.0) + float(
            item.get("score") or 1
        )
    parts = [f"共 {len(plan)} 题，满分 {round(sum(scores.values()), 1)} 分"]
    for qtype in counts:
        parts.append(f"{TYPE_LABELS.get(qtype, qtype)} {counts[qtype]} 题 {round(scores[qtype], 1)} 分")
    return "，".join(parts)


# ---------------------------------------------------------------- 出题与校验


def _extract_options_from_answer(answer: str) -> set[str]:
    return set(re.findall(r"[A-H]", str(answer).upper()))


def _normalize_options(options: Any) -> dict[str, str]:
    if isinstance(options, dict):
        return {str(key).upper(): str(value) for key, value in options.items() if value}
    return {}


def normalize_question(
    raw: Any,
    expected_type: str,
    *,
    index: int,
    chapter: str = "",
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("出题结果不是 JSON 对象。")
    question_type = str(raw.get("type") or expected_type).lower()
    if question_type not in TYPE_LABELS:
        raise ValueError(f"未知题型：{question_type}")
    stem = str(raw.get("stem") or "").strip()
    if not stem:
        raise ValueError("题目缺少题干。")

    options = _normalize_options(raw.get("options"))
    answer = raw.get("answer")
    analysis = str(raw.get("analysis") or "").strip()
    if not analysis:
        analysis = "（模型未提供解析，建议结合教材复习本题考点。）"

    if question_type in (SINGLE, MULTIPLE):
        if len(options) < 3:
            raise ValueError("选择题选项不足。")
        if question_type == SINGLE:
            letters = str(answer or "").upper()
            letters = _extract_options_from_answer(letters)
            if len(letters) != 1 or letters.pop() not in options:
                raise ValueError("单选题答案格式不正确。")
            canonical = str(answer).upper().strip()
        else:
            letters = _extract_options_from_answer(
                answer if isinstance(answer, str) else (answer or [])
            )
            if not letters or not letters.issubset(set(options)) or len(letters) < 2:
                raise ValueError("多选题答案格式不正确（应为 2 个以上正确选项）。")
            canonical = sorted(letters)
    else:
        answer_text = str(answer or "").strip()
        if len(answer_text) < 4:
            raise ValueError("主观题参考答案过短。")
        canonical = answer_text

    knowledge_points = raw.get("knowledge_points") or raw.get("knowledge_point") or []
    if isinstance(knowledge_points, str):
        knowledge_points = [knowledge_points]
    knowledge_points = [str(item).strip() for item in knowledge_points if str(item).strip()]
    if not knowledge_points:
        knowledge_points = [chapter or "综合应用"]

    question = {
        "index": int(index),
        "type": question_type,
        "stem": stem,
        "options": options,
        "answer": canonical,
        "analysis": analysis,
        "knowledge_points": knowledge_points[:5],
        "chapter": str(raw.get("chapter") or chapter or "未标注章节"),
        "refs": [],
    }
    question["answer_display"] = format_answer_for_display(question)
    return question


def format_answer_for_display(question: dict[str, Any]) -> str:
    answer = question["answer"]
    if question["type"] == SINGLE:
        letter = str(answer).upper().strip()
        option_text = question["options"].get(letter, "")
        return f"{letter}. {option_text}" if option_text else letter
    if question["type"] == MULTIPLE:
        parts = []
        for letter in answer:
            option_text = question["options"].get(letter, "")
            parts.append(f"{letter}. {option_text}" if option_text else letter)
        return "；".join(parts)
    return str(answer)


QTYPE_RULES = {
    SINGLE: "单选题：4 个备选项，只有一个正确选项。",
    MULTIPLE: "多选题：4~5 个备选项，2~4 个正确选项（少选、错选、多选均不得分）。",
    CALC: "计算分析题：给出完整计算过程与标准化参考答案，可包含 2~3 小问（难度递进）。",
    CASE: "案例分析题：结合法规/准则依据与案例事实作答，给出完整参考答案与依据条文。",
    SHORT: "简答题：直接作答要点，给出标准化参考答案与评分要点。",
    COMP: "综合题：跨章节综合应用，可包含多小问，给出完整解题过程与参考答案。",
    SUBJECTIVE: "主观题：可以是计算分析题、简答题或综合题，请给出完整、标准化的参考答案与解题步骤。",
}


def question_prompt(
    course_key: str,
    plan_item: dict[str, Any],
    context_snippets: list[str] | None = None,
    extra_rules: str = "",
) -> list[dict[str, str]]:
    c = course(course_key)
    qtype = plan_item["type"]
    qtype_rule = QTYPE_RULES[qtype]
    chapter_hint = plan_item.get("chapter") or "请自行从本课程常考章节中选择"
    score_hint = (
        f"\n本题分值：约 {plan_item['score']} 分（请按该分值把握题目容量与难度）。"
        if plan_item.get("score")
        else ""
    )
    user_content = f"""请为 CPA《{c.name}》命制一道{qtype_rule}{score_hint}

建议考查章节/范围：{chapter_hint}

题目要求：
1. 难度贴近最近十年真题中等水平，适合阶段自测；
2. 题目应体现该章节一个以上的核心考点，题干信息完整、数字/情景自洽；
3. 不要照抄下面参考素材里的原文题目，可以改编同考点、更换情景与数字；
4. 答案与解析必须准确，解析要说明“为什么对 / 为什么错”或完整计算/作答过程；
5. knowledge_points 用 1~3 个简短的考点名称描述本题考核的知识点；
6. 如果本章不适合该题型，请自动选择相邻相关章节的合适考点。

{"参考素材（注意仅用于把握考点，不要照抄）：\n" + "\n\n".join(context_snippets[:3]) if context_snippets else ""}

请只输出一个 JSON 对象（不要 ``` 标记、不要解释），字段如下：
{{
  "type": "{qtype}",
  "chapter": "章节名",
  "stem": "题干",
  "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
  "answer": "单选题填选项字母；多选题填正确选项字母数组如 [\"A\",\"C\"]；主观题填参考答案",
  "analysis": "完整解题思路与最终答案说明",
  "knowledge_points": ["考点1", "考点2"]
}}"""
    return [
        {"role": "system", "content": EXAMINER_SYSTEM},
        {"role": "user", "content": user_content},
    ]


def generate_question(
    course_key: str,
    plan_item: dict[str, Any],
    *,
    llm_overrides: dict[str, Any] | None = None,
    retriever: Any | None = None,
    embed_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """AI 生成并校验一道题。"""
    query = f"{course(course_key).name} {plan_item.get('chapter') or ''} 高频考点"
    snippets: list[str] = []
    hits: list[dict[str, Any]] = []
    try:
        hits = search_knowledge(
            course_key, query, k=3, retriever=retriever, embed_cfg=embed_cfg
        )
        snippets = [
            f"[资料{i + 1}] {hit['text'][:900]}" for i, hit in enumerate(hits)
        ]
    except Exception:
        snippets = []
    raw = chat_json(
        question_prompt(course_key, plan_item, snippets),
        overrides=llm_overrides,
    )
    question = normalize_question(
        raw,
        plan_item["type"],
        index=plan_item["index"],
        chapter=plan_item.get("chapter", ""),
    )
    question["refs"] = [
        {"source": hit.get("metadata", {}).get("source", ""), "text": hit["text"][:220]}
        for hit in hits[:2]
        if hit.get("metadata", {}).get("source")
    ]
    return question


def generate_questions_batch(
    course_key: str,
    plan: list[dict[str, Any]],
    *,
    llm_overrides: dict[str, Any] | None = None,
    retriever: Any | None = None,
    progress=None,
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for index, item in enumerate(plan, start=1):
        if progress:
            progress(index, len(plan))
        questions.append(
            generate_question(
                course_key,
                item,
                llm_overrides=llm_overrides,
                retriever=retriever,
            )
        )
    return questions


def assemble_exam(
    course_key: str,
    plan: list[dict[str, Any]],
    *,
    excluded_stems: list[str] | None = None,
    llm_overrides: dict[str, Any] | None = None,
    retriever: Any | None = None,
    progress=None,
) -> dict[int, dict[str, Any]]:
    """按蓝图装配试卷：优先从真题题库/生成缓存选题（快），缺口并行 AI 生成。

    返回 {index: question}；AI 生成失败的题目留空，由页面按需重试。
    生成成功的新题会写入 question_cache，供后续考试复用。
    """
    from concurrent.futures import ThreadPoolExecutor

    from cpa_tutor.config import exam_gen_workers
    from cpa_tutor.question_bank import QuestionBank

    bank = QuestionBank()
    used: set[str] = {str(stem)[:160] for stem in (excluded_stems or [])}
    questions: dict[int, dict[str, Any]] = {}
    gaps: list[dict[str, Any]] = []

    for item in plan:
        question = bank.sample_one(
            course_key, item["type"], chapter=item.get("chapter", ""), exclude_stems=used
        )
        if question is None:
            cached = bank.sample_cache_questions(
                course_key, item["type"],
                chapter=item.get("chapter", ""), exclude_stems=used, limit=1,
            )
            question = cached[0] if cached else None
        if question is not None:
            question["index"] = item["index"]
            question["score"] = item["score"]
            question["refs"] = question.get("refs") or []
            question["answer_display"] = format_answer_for_display(question)
            questions[item["index"]] = question
            used.add(str(question["stem"])[:160])
        else:
            gaps.append(item)

    if gaps:
        workers = max(1, exam_gen_workers())

        def _generate(item: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
            try:
                question = generate_question(
                    course_key,
                    item,
                    llm_overrides=llm_overrides,
                    retriever=retriever,
                )
                return int(item["index"]), question
            except Exception:
                return int(item["index"]), None

        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_to_item = {pool.submit(_generate, item): item for item in gaps}
                done = 0
                for future in future_to_item:
                    index, question = future.result()
                    done += 1
                    if progress:
                        progress(done, len(gaps))
                    if question is not None:
                        item = future_to_item[future]
                        question["score"] = item["score"]
                        questions[index] = question
                        bank.insert_cache_questions(course_key, [question])
        else:
            for done, item in enumerate(gaps, start=1):
                index, question = _generate(item)
                if progress:
                    progress(done, len(gaps))
                if question is not None:
                    question["score"] = item["score"]
                    questions[index] = question
                    bank.insert_cache_questions(course_key, [question])
    return questions


# ---------------------------------------------------------------- 真题题库解析


def bank_parse_messages(
    course_key: str, chunk_text: str, *, year: int | None = None
) -> list[dict[str, str]]:
    """让 LLM 从真题文本中批量提取结构化题目的提示词。"""
    c = course(course_key)
    return [
        {"role": "system", "content": EXAMINER_SYSTEM},
        {
            "role": "user",
            "content": f"""以下内容是 CPA《{c.name}》真题试卷的部分文本（含题干、选项与答案）。
请从中提取**所有完整题目**，逐题整理为结构化 JSON。

规则：
1. 题型取：single（单选）/ multiple（多选）/ calc（计算分析、计算问答）/ case（案例分析）/ short（简答）/ comp（综合）；
2. options 为选项字典；answer 为答案：单选题填选项字母、多选题填字母数组（如 ["A","C"]）、主观题填参考答案全文；
3. analysis 优先使用原文的答案解析；没有则根据你的 CPA 知识给出准确解析；
4. knowledge_points 为 1~3 个简短考点名；chapter 尽量识别（识别不出留空字符串）；
5. 题目必须完整（题干+答案齐全）；残缺题、只有答案没有题干、非题目的文本一律跳过。

{"年份：" + str(year) + "\n" if year else ""}
真题文本：
{chunk_text}

只输出 JSON 数组（不要 ```），每个元素：
{{"type": "...", "stem": "...", "options": {{...}}, "answer": "...", "analysis": "...", "knowledge_points": [...], "chapter": "..."}}""",
        },
    ]


def parse_bank_batch(
    course_key: str,
    chunks: list[dict[str, Any]],
    *,
    year: int | None = None,
    batch_chars: int = 6000,
    llm_overrides: dict[str, Any] | None = None,
    progress=None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """把真题文本分块批量提取题目，逐题校验。

    chunks 应来自 kind == "真题" 的可读文本分块（见 knowledge.load_course_chunks）。
    返回 (通过校验的题目列表, {batches, valid, invalid})。
    """
    batches: list[str] = []
    current = ""
    for chunk in chunks or []:
        text = str(chunk.get("text") or "").strip()
        if not text:
            continue
        if len(current) + len(text) > batch_chars and current:
            batches.append(current)
            current = ""
        current += text + "\n\n"
    if current:
        batches.append(current)

    questions: list[dict[str, Any]] = []
    stats = {"batches": len(batches), "valid": 0, "invalid": 0}
    for batch_no, batch in enumerate(batches, start=1):
        raw = chat_json(
            bank_parse_messages(course_key, batch, year=year),
            overrides=llm_overrides,
        )
        if isinstance(raw, dict):  # 单题对象也接受
            raw = [raw]
        if not isinstance(raw, list):
            stats["invalid"] += 1
            continue
        for item in raw:
            try:
                qtype = str(item.get("type") or "").lower()
                if qtype not in TYPE_LABELS:
                    raise ValueError(f"未知题型：{qtype}")
                question = normalize_question(
                    item,
                    qtype,
                    index=0,
                    chapter=str(item.get("chapter") or ""),
                )
                questions.append(question)
                stats["valid"] += 1
            except Exception:
                stats["invalid"] += 1
        if progress:
            progress(batch_no, len(batches))
    return questions, stats


# ---------------------------------------------------------------- 判分


def evaluate_choice_answer(
    question: dict[str, Any],
    selected: Any,
) -> tuple[bool, str]:
    """客观题本地判分。返回 (是否正确, 学生答案展示)。"""
    qtype = question["type"]
    if qtype == SINGLE:
        student = str(selected or "").upper().strip()
        correct = student == str(question["answer"]).upper().strip()
    else:
        student_letters = sorted(_extract_options_from_answer(selected or []))
        expected = sorted(question["answer"])
        correct = student_letters == expected
    return correct, format_student_answer(question, selected)


def format_student_answer(question: dict[str, Any], selected: Any) -> str:
    if is_subjective_type(question["type"]):
        return str(selected or "")
    if question["type"] == SINGLE:
        letter = str(selected or "").upper().strip()
        option_text = question["options"].get(letter, "")
        return f"{letter}. {option_text}" if option_text else (letter or "未作答")
    letters = sorted(_extract_options_from_answer(selected or []))
    parts = []
    for letter in letters:
        option_text = question["options"].get(letter, "")
        parts.append(f"{letter}. {option_text}" if option_text else letter)
    return "；".join(parts) if parts else "未作答"


def judge_subjective(
    question: dict[str, Any],
    student_answer: str,
    *,
    llm_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """主观题由 AI 按参考答案阅卷，返回 0~1 的得分比例（支持部分给分）。"""
    messages = [
        {
            "role": "system",
            "content": (
                "你是 CPA 主观题阅卷专家。请按官方参考答案与评分要点客观评分，"
                "不要在阅卷信息中泄露你的内部判断过程，只输出 JSON。"
            ),
        },
        {
            "role": "user",
            "content": f"""请批改以下 CPA 主观题答案。

【题目】{question["stem"]}
【标准答案与解题要点】{question["answer"]}
【考生答案】
{student_answer or "（空白，未作答）"}

请只输出 JSON 对象：
{{
  "verdict": "correct | partial | wrong",
  "is_correct": true 或 false,
  "score_ratio": 0 到 1 之间的小数（按 0.5 步进：1 完全正确、0.5 部分正确、0 错误；is_correct 为 true 时必须是 1），
  "feedback": "用一两段话点评：正确/错误/不完整之处，并给出改进建议",
  "key_points": ["本次暴露出的考点，如不完整可留空"]
}}""",
        },
    ]
    raw = chat_json(messages, temperature=0.1, overrides=llm_overrides)
    return normalize_judge_result(raw)


def normalize_judge_result(raw: Any) -> dict[str, Any]:
    """归一化 AI 阅卷结果：校验 verdict/score_ratio，兼容旧返回格式。

    旧格式（无 score_ratio）按 verdict 映射 1.0/0.5/0；ratio 夹紧到 0~1
    并取 0.5 步进；is_correct 为真时强制 1.0。
    """
    if not isinstance(raw, dict):
        raw = {}
    verdict = str(
        raw.get("verdict") or ("correct" if raw.get("is_correct") else "wrong")
    )
    if verdict not in {"correct", "partial", "wrong"}:
        verdict = "correct" if raw.get("is_correct") else "wrong"
    is_correct = bool(raw.get("is_correct", verdict == "correct"))
    try:
        ratio = float(raw.get("score_ratio"))
    except (TypeError, ValueError):
        ratio = {"correct": 1.0, "partial": 0.5, "wrong": 0.0}[verdict]
    ratio = max(0.0, min(1.0, round(ratio * 2) / 2))
    if is_correct:
        ratio = 1.0
    elif ratio == 0.0 and verdict == "correct":
        verdict, is_correct = "wrong", False
    return {
        "verdict": verdict,
        "is_correct": is_correct,
        "score_ratio": ratio,
        "feedback": str(raw.get("feedback") or "AI 阅卷未返回详细点评。").strip(),
        "key_points": raw.get("key_points") or [],
    }


def compute_exam_scores(
    results: list[dict[str, Any]], plan: list[dict[str, Any]]
) -> dict[str, Any]:
    """按题型分值计算成绩：客观题按对错、主观题按 AI 给分比例（支持部分给分）。

    会给每条 result 补充 earned（本题实际得分）字段；
    返回 {score(100 分制得分率), correct_count, earned_total, total_score,
    by_type: {type: {count, earned, total, correct}}}。
    """
    plan_score = {
        int(item["index"]): float(item.get("score") or 1) for item in plan or []
    }
    by_type: dict[str, dict[str, float]] = {}
    earned_total = 0.0
    total_score = 0.0
    correct_count = 0
    for item in results or []:
        question = item.get("question") or {}
        qtype = str(question.get("type") or "")
        full = float(
            question.get("score") or plan_score.get(question.get("index"), 1) or 1
        )
        if item.get("is_correct"):
            ratio = 1.0
        else:
            ratio = float((item.get("judge") or {}).get("score_ratio") or 0.0)
        earned = round(full * ratio, 1)
        item["earned"] = earned
        item["full_score"] = full
        bucket = by_type.setdefault(
            qtype, {"count": 0, "earned": 0.0, "total": 0.0, "correct": 0}
        )
        bucket["count"] += 1
        bucket["earned"] = round(bucket["earned"] + earned, 1)
        bucket["total"] = round(bucket["total"] + full, 1)
        if item.get("is_correct"):
            bucket["correct"] += 1
            correct_count += 1
        earned_total += earned
        total_score += full
    score = round(100.0 * earned_total / total_score, 1) if total_score else 0.0
    return {
        "score": score,
        "correct_count": correct_count,
        "earned_total": round(earned_total, 1),
        "total_score": round(total_score, 1),
        "by_type": by_type,
    }


def similar_question_messages(
    course_key: str,
    original: dict[str, Any],
    topic: str,
    *,
    retriever: Any | None = None,
) -> list[dict[str, str]]:
    """基于错题快照命制同题型、同考点、同难度的变式题（类似题）。"""
    c = course(course_key)
    qtype = str(original.get("type") or SUBJECTIVE)
    qtype_rule = QTYPE_RULES.get(qtype, QTYPE_RULES[SUBJECTIVE])
    snapshot = (
        f"原题题干：{original.get('stem', '')[:600]}\n"
        f"原题选项：{original.get('options') or '（无）'}\n"
        f"原题答案：{original.get('answer_display') or original.get('answer', '')}\n"
        f"原题解析：{original.get('analysis', '')[:400]}\n"
        f"原题考点：{'、'.join(original.get('knowledge_points') or [])}\n"
    )
    block, _ = _material_block(course_key, topic, retriever=retriever)
    return [
        {"role": "system", "content": EXAMINER_SYSTEM},
        {
            "role": "user",
            "content": f"""{block}

请根据下面的错题快照，命制一道**同题型、同考点、同难度**的变式题（类似题）：
更换场景、数字或表述，不得照抄原题，也不得照抄素材原文。
{qtype_rule}

{snapshot}
只输出一个 JSON 对象（不要 ```）：
{{
  "type": "{qtype}",
  "chapter": "章节名",
  "stem": "题干",
  "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
  "answer": "单选题填选项字母；多选题填正确选项字母数组；主观题填参考答案",
  "analysis": "完整解题思路与最终答案说明",
  "knowledge_points": ["考点1", "考点2"]
}}""",
        },
    ]


def match_question_points_to_weak(
    question_points: list[str], active_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """把题目考点与考生的未掌握知识点匹配：先精确同名，再互相包含（≥2 字）。"""
    matched: list[dict[str, Any]] = []
    for name in question_points or []:
        name = str(name).strip()
        if not name:
            continue
        for row in active_rows or []:
            point_name = str(row.get("knowledge_point") or "").strip()
            if not point_name or not row.get("id"):
                continue
            if name == point_name or (
                len(name) >= 2
                and len(point_name) >= 2
                and (name in point_name or point_name in name)
            ):
                if row not in matched:
                    matched.append(row)
    return matched


def given_up_result(question: dict[str, Any]) -> dict[str, Any]:
    """“我不会”作答结果：判错、0 分，并携带解析与正确答案供立即学习。"""
    return {
        "question": question,
        "student_answer": "",
        "student_answer_display": "不会",
        "is_correct": False,
        "given_up": True,
        "earned": 0,
        "full_score": question.get("score"),
        "judge": {
            "verdict": "wrong",
            "is_correct": False,
            "score_ratio": 0.0,
            "feedback": "考生标记“不会”，建议认真学习本题解析与对应知识点。",
            "key_points": question.get("knowledge_points", []),
        },
    }


def direct_weak_points_from_wrong(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """AI 诊断失败时的兜底：按错题自带考点直接沉淀弱项（去重）。"""
    points: dict[str, dict[str, str]] = {}
    for item in results or []:
        if item.get("is_correct"):
            continue
        question = item.get("question") or {}
        for name in question.get("knowledge_points") or []:
            name = str(name).strip()
            if not name or name == "出题失败" or name in points:
                continue
            points[name] = {
                "name": name,
                "reason": "本题答错，考点需要复习巩固。",
                "suggestion": "复习该考点后完成类似题练习。",
            }
    return list(points.values())


def analyze_wrong_questions(
    course_key: str,
    wrong_items: list[dict[str, Any]],
    *,
    llm_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """AI 综合分析答错题目，输出未掌握知识点清单。"""
    if not wrong_items:
        return {
            "summary": "本卷没有答错题，继续保持！建议定期模考检验。",
            "knowledge_points": [],
        }
    lines = []
    for item in wrong_items:
        question = item["question"]
        lines.append(
            f"- 第{question['index']}题（{TYPE_LABELS.get(question['type'], '')}，"
            f"考查：{'、'.join(question.get('knowledge_points', []))}）\n"
            f"  题干摘要：{question['stem'][:180]}\n"
            f"  考生答案：{item.get('student_answer_display') or item.get('student_answer')}\n"
            f"  正确要点：{question.get('answer_display', '')[:220]}"
        )
    messages = [
        {
            "role": "system",
            "content": (
                "你是 CPA 学习诊断专家。根据错题记录判断考生尚未掌握的知识点，"
                "知识点要具体到可复习的最小单元，避免一句话概括整章。"
            ),
        },
        {
            "role": "user",
            "content": f"""科目：CPA《{course(course_key).name}》

本次模考错题记录如下：
{"\n".join(lines)}

请分析：
1. 每道错题真正卡住的考点是什么；
2. 推断其背后尚未掌握/容易混淆的知识点；
3. 给出每个知识点对应的复习建议（一句话）。

只输出 JSON 对象：
{{
  "summary": "总体诊断（100~180字，指出薄弱方向）",
  "knowledge_points": [
    {{
      "name": "具体知识点名称",
      "reason": "为什么判定该点未掌握（结合错因）",
      "suggestion": "一句话复习建议"
    }}
  ]
}}""",
        },
    ]
    raw = chat_json(messages, temperature=0.15, overrides=llm_overrides)
    if not isinstance(raw, dict):
        raise LLMError("知识点分析结果不是 JSON 对象。")
    points = raw.get("knowledge_points") or []
    cleaned = []
    for point in points:
        if isinstance(point, dict) and str(point.get("name") or "").strip():
            cleaned.append(
                {
                    "name": str(point["name"]).strip(),
                    "reason": str(point.get("reason") or ""),
                    "suggestion": str(point.get("suggestion") or ""),
                }
            )
    return {
        "summary": str(raw.get("summary") or "").strip() or "AI 已完成错题诊断。",
        "knowledge_points": cleaned,
    }


# ---------------------------------------------------------------- 弱项复习素材


def _material_block(
    course_key: str,
    topic: str,
    extra: str = "",
    *,
    retriever: Any | None = None,
) -> tuple[str, list[str]]:
    query = f"{course(course_key).name} {topic}"
    refs: list[str] = []
    try:
        hits = search_knowledge(course_key, query, k=5, retriever=retriever)
        refs = [f"[资料{i + 1}] {hit['text'][:700]}" for i, hit in enumerate(hits)]
    except Exception:
        refs = []
    block = f"""科目：CPA《{course(course_key).name}》
待复习知识点：{topic}
{("错因备注：" + extra + "\n") if extra else ""}
"""
    if refs:
        block += "教材/真题相关素材（如素材不足，请结合你掌握的 CPA 知识讲透该考点）：\n" + "\n\n".join(refs)
    else:
        block += "当前知识库中没有检索到足够的相关素材，请基于你掌握的 CPA 知识讲解。"
    return block, refs


def summary_messages(
    course_key: str,
    topic: str,
    extra: str = "",
    *,
    retriever: Any | None = None,
) -> list[dict[str, str]]:
    block, _ = _material_block(course_key, topic, extra, retriever=retriever)
    return [
        {"role": "system", "content": TUTOR_SYSTEM},
        {
            "role": "user",
            "content": f"""{block}

请为该知识点生成一份适合考前回顾的“知识点摘要”：
- 概念/公式/法条要点（如有）；
- 常考角度与典型陷阱；
- 与相似知识点的辨析；
- 一条最有效的记忆口诀或联想。
用 Markdown 小标题组织，控制篇幅，重点突出。""",
        },
    ]


def quiz_messages(
    course_key: str,
    topic: str,
    question_count: int = 4,
    *,
    retriever: Any | None = None,
) -> list[dict[str, str]]:
    block, _ = _material_block(course_key, topic, retriever=retriever)
    return [
        {"role": "system", "content": EXAMINER_SYSTEM},
        {
            "role": "user",
            "content": f"""{block}

请围绕该知识点命制 {question_count} 道单选题，全部考查同一考点下的不同侧面
（概念辨析、适用条件、计算、易错陷阱等）。不得照抄素材原文。

只输出 JSON 数组（不要 ```），每个元素：
{{
  "stem": "题干",
  "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
  "answer": "正确选项字母，如 A",
  "analysis": "解析（说明对与错）",
  "knowledge_point": "考点名称"
}}""",
        },
    ]


def card_messages(
    course_key: str,
    topic: str,
    card_count: int = 5,
    *,
    retriever: Any | None = None,
) -> list[dict[str, str]]:
    block, _ = _material_block(course_key, topic, retriever=retriever)
    return [
        {"role": "system", "content": TUTOR_SYSTEM},
        {
            "role": "user",
            "content": f"""{block}

请围绕该知识点制作 {card_count} 张记忆卡片，覆盖最值得记的公式/法条/关键词/易错对比。
只输出 JSON 数组（不要 ```），每张卡片：
{{"front": "卡片正面：问题/公式名称/情景", "back": "卡片背面：答案/公式/要点", "hint": "可选：一条记忆提示/口诀"}}""",
        },
    ]


def mindmap_messages(
    course_key: str,
    topic: str,
    extra: str = "",
    *,
    retriever: Any | None = None,
) -> list[dict[str, str]]:
    """思维导图提示词：要求返回 JSON {{format: mermaid|tree, content}}。"""
    block, _ = _material_block(course_key, topic, extra, retriever=retriever)
    return [
        {"role": "system", "content": TUTOR_SYSTEM},
        {
            "role": "user",
            "content": f"""{block}

请为该知识点生成一张思维导图。只输出 JSON 对象（不要 ```）：
{{
  "format": "mermaid" 或 "tree",
  "content": "思维导图内容"
}}
要求：
- format 取 "mermaid" 时：content 必须是合法的 Mermaid mindmap 语法，
  形如 mindmap\\n  root((核心考点))\\n    分支一\\n      子要点，节点用简短中文短语，
  3 层左右、8~20 个节点，不要代码围栏标记；
- format 取 "tree" 时：content 为缩进纯文本树，每行一个要点；
- 如果对 Mermaid 语法没有把握，请选 "tree" 以保证可用。""",
        },
    ]


def lesson_messages(
    course_key: str,
    topic: str,
    extra: str = "",
    *,
    retriever: Any | None = None,
) -> list[dict[str, str]]:
    """详细讲解提示词：固定三节结构（核心概念/要点详解/易错辨析）。"""
    block, _ = _material_block(course_key, topic, extra, retriever=retriever)
    return [
        {"role": "system", "content": TUTOR_SYSTEM},
        {
            "role": "user",
            "content": f"""{block}

请为该知识点撰写一份详细讲解（约 600 字，Markdown），固定包含三节：
## 核心概念（定义/公式/法条，讲清楚“是什么”）
## 要点详解（适用条件、计算或判断步骤、常考变化）
## 易错辨析（用对比表列出易混点与正确理解）
内容要面向应试：直击考点、结论明确，不泛泛而谈。""",
        },
    ]


def examples_messages(
    course_key: str,
    topic: str,
    extra: str = "",
    *,
    retriever: Any | None = None,
) -> list[dict[str, str]]:
    """应用举例提示词：2~3 个真题风格小例题（题目+解答+考点）。"""
    block, _ = _material_block(course_key, topic, extra, retriever=retriever)
    return [
        {"role": "system", "content": EXAMINER_SYSTEM},
        {
            "role": "user",
            "content": f"""{block}

请围绕该知识点给出 2~3 个应用举例（真题风格的小例题）。只输出 JSON 数组（不要 ```），每个元素：
{{"title": "例题标题（说明考查角度）", "question": "题目内容", "solution": "完整解答过程", "key_points": ["该例题涉及的考点"]}}
例子应覆盖不同考查角度（概念判断/计算/易错陷阱），数字与情景自洽，不照抄素材原文。""",
        },
    ]


def choose_mindmap_format(raw: Any) -> str:
    """判断思维导图渲染方式：mermaid 仅在字段与内容都合法时使用，否则降级 tree。"""
    if isinstance(raw, dict) and str(raw.get("format") or "").lower() == "mermaid":
        content = str(raw.get("content") or "")
        if "mindmap" in content.lower():
            return "mermaid"
    return "tree"


def review_qa_messages(
    course_key: str,
    topic: str,
    history: list[dict[str, str]],
    user_text: str,
    *,
    retriever: Any | None = None,
) -> list[dict[str, str]]:
    block, _ = _material_block(course_key, topic, retriever=retriever)
    messages = [
        {
            "role": "system",
            "content": f"{TUTOR_SYSTEM}\n当前正在帮助考生复习知识点“{topic}”。\n{block}",
        }
    ]
    messages.extend(history[-8:])
    messages.append({"role": "user", "content": user_text})
    return messages


def general_qa_messages(
    course_key: str,
    history: list[dict[str, str]],
    user_text: str,
    *,
    retriever: Any | None = None,
) -> list[dict[str, str]]:
    try:
        hits = search_knowledge(course_key, user_text, k=5, retriever=retriever)
        refs = [f"[资料{i + 1}] {hit['text'][:650]}" for i, hit in enumerate(hits)]
    except Exception:
        refs = []
    context = ""
    if refs:
        context = "以下是从该课程知识库检索到的素材：\n" + "\n\n".join(refs) + "\n\n请优先依据素材回答并标注 [资料]。"
    else:
        context = "知识库中未检索到相关内容，请基于你掌握的 CPA 知识回答，并提示考生可补充资料。"
    system = (
        f"{TUTOR_SYSTEM}\n当前课程：CPA《{course(course_key).name}》\n{context}"
    )
    messages = [{"role": "system", "content": system}]
    messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_text})
    return messages
