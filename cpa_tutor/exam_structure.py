"""按科目内置 CPA 官方题型与分值结构，实现"智能组卷"（题型占比智能确定）。

题型与分值占比数据：2021 年改革后中注协公布的专业阶段各科题型、题量与
分值结构（满分 100）。说明：
- 审计"简答题 6 题共 31 分"、战略"简答题 4 题共 26 分"等为非整分值，
  请以中注协当年公告为准；全部数据集中在本表，便于按最新公告一处修订；
- 组卷时按"整卷/半卷/迷你卷"等比缩放题量，每题分值按官方比例折算
  （最小 0.5 分），总分近似目标分值；报告页按实际满分计算得分率。
"""

from __future__ import annotations

import random
from typing import Any

SINGLE = "single"
MULTIPLE = "multiple"
CALC = "calc"  # 计算分析题（会计/财管）或计算问答题（税法）
CASE = "case"  # 案例分析题（经济法）
SHORT = "short"  # 简答题（审计/战略）
COMP = "comp"  # 综合题

CHOICE_TYPES = (SINGLE, MULTIPLE)
SUBJECTIVE_TYPES = (CALC, CASE, SHORT, COMP)

SCALES: dict[str, int] = {
    "full": 100,  # 整卷：题量与分值按官方结构等比例
    "half": 50,  # 半卷：约一半题量，适合阶段巩固
    "mini": 30,  # 迷你卷：快速自测
}

EXAM_STRUCTURES: dict[str, list[dict[str, Any]]] = {
    "accounting": [
        {"type": SINGLE, "label": "单项选择题", "count": 13, "total_score": 26},
        {"type": MULTIPLE, "label": "多项选择题", "count": 12, "total_score": 24},
        {"type": CALC, "label": "计算分析题", "count": 2, "total_score": 18},
        {"type": COMP, "label": "综合题", "count": 2, "total_score": 32},
    ],
    "audit": [
        {"type": SINGLE, "label": "单项选择题", "count": 20, "total_score": 20},
        {"type": MULTIPLE, "label": "多项选择题", "count": 15, "total_score": 30},
        {"type": SHORT, "label": "简答题", "count": 6, "total_score": 31},
        {"type": COMP, "label": "综合题", "count": 1, "total_score": 19},
    ],
    "tax": [
        {"type": SINGLE, "label": "单项选择题", "count": 26, "total_score": 26},
        {"type": MULTIPLE, "label": "多项选择题", "count": 16, "total_score": 24},
        {"type": CALC, "label": "计算问答题", "count": 4, "total_score": 20},
        {"type": COMP, "label": "综合题", "count": 2, "total_score": 30},
    ],
    "economic_law": [
        {"type": SINGLE, "label": "单项选择题", "count": 26, "total_score": 26},
        {"type": MULTIPLE, "label": "多项选择题", "count": 16, "total_score": 24},
        {"type": CASE, "label": "案例分析题", "count": 4, "total_score": 50},
    ],
    "financial_management": [
        {"type": SINGLE, "label": "单项选择题", "count": 13, "total_score": 26},
        {"type": MULTIPLE, "label": "多项选择题", "count": 12, "total_score": 24},
        {"type": CALC, "label": "计算分析题", "count": 4, "total_score": 36},
        {"type": COMP, "label": "综合题", "count": 1, "total_score": 14},
    ],
    "strategy": [
        {"type": SINGLE, "label": "单项选择题", "count": 26, "total_score": 26},
        {"type": MULTIPLE, "label": "多项选择题", "count": 16, "total_score": 24},
        {"type": SHORT, "label": "简答题", "count": 4, "total_score": 26},
        {"type": COMP, "label": "综合题", "count": 1, "total_score": 24},
    ],
}


def exam_structure(course_key: str) -> list[dict[str, Any]]:
    """某科目的官方题型结构（未知科目回退会计）。"""
    return EXAM_STRUCTURES.get(course_key) or EXAM_STRUCTURES["accounting"]


def structure_text(course_key: str) -> str:
    """"单选 13 题共 26 分、多选 12 题共 24 分、…（满分 100 分）"展示文案。"""
    parts = [
        f"{group['label']} {group['count']} 题共 {group['total_score']} 分"
        for group in exam_structure(course_key)
    ]
    return "、".join(parts) + "（满分 100 分）"


def plan_total_score(plan: list[dict[str, Any]]) -> float:
    """试卷蓝图的总分。"""
    return round(sum(float(item.get("score") or 0) for item in plan), 1)


def build_exam_plan(
    course_key: str,
    scale: str = "full",
    *,
    chapters: list[str] | None = None,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """按官方题型与分值结构生成试卷蓝图。

    scale：full / half / mini（见 SCALES）。题型顺序与真实试卷一致
    （单选 → 多选 → 主观题），题型分区内按章节能者轮转分配考查章节；
    seed 只影响章节轮转起点，保证可复现。
    """
    ratio = SCALES.get(scale, 100) / 100.0
    chapter_pool = [
        chapter for chapter in (chapters or []) if chapter and chapter != "未标注章节"
    ]
    rng = random.Random(seed)
    offset = rng.randrange(max(1, len(chapter_pool))) if chapter_pool else 0
    plan: list[dict[str, Any]] = []
    index = 1
    for group in exam_structure(course_key):
        count = max(1, int(round(group["count"] * ratio)))
        official_score = group["total_score"] / group["count"]
        per_question = max(0.5, round(official_score * ratio * 2) / 2)
        for _ in range(count):
            chapter = ""
            if chapter_pool:
                chapter = chapter_pool[(offset + index - 1) % len(chapter_pool)]
            plan.append(
                {
                    "index": index,
                    "type": group["type"],
                    "chapter": chapter,
                    "score": per_question,
                }
            )
            index += 1
    return plan
