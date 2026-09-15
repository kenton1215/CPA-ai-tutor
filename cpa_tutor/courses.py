"""CPA 六门科目与资料文件登记。

课程采用稳定英文键（course_key），界面使用中文名称。
资料目录默认位于 data/cpa_docs：既支持根目录下的 PDF，
也支持 data/cpa_docs/<course_key>/ 子目录（用于用户上传资料）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "cpa_docs"


@dataclass(frozen=True)
class Course:
    key: str
    name: str
    short_name: str
    description: str


COURSES: dict[str, Course] = {
    "accounting": Course(
        key="accounting",
        name="会计",
        short_name="会计",
        description="企业会计准则、会计要素确认计量与财务报告编制。",
    ),
    "audit": Course(
        key="audit",
        name="审计",
        short_name="审计",
        description="审计基本原理、风险评估、审计证据与审计报告。",
    ),
    "financial_management": Course(
        key="financial_management",
        name="财务成本管理",
        short_name="财管",
        description="财务管理、成本计算与管理会计。",
    ),
    "tax": Course(
        key="tax",
        name="税法",
        short_name="税法",
        description="增值税、消费税、企业所得税、个人所得税等十八个税种。",
    ),
    "economic_law": Course(
        key="economic_law",
        name="经济法",
        short_name="经济法",
        description="民事法律、合同、公司、证券、破产、票据等法律制度。",
    ),
    "strategy": Course(
        key="strategy",
        name="公司战略与风险管理",
        short_name="战略",
        description="战略分析、战略选择、战略实施与风险管理。",
    ),
}

COURSE_KEYS: list[str] = list(COURSES)

# 文件名关键词 -> course_key（按顺序优先匹配）
_NAME_PATTERNS: tuple[tuple[str, str], ...] = (
    ("公司战略与风险管理", "strategy"),
    ("战略", "strategy"),
    ("财务成本管理", "financial_management"),
    ("财管", "financial_management"),
    ("经济法", "economic_law"),
    ("会计", "accounting"),
    ("审计", "audit"),
    ("税法", "tax"),
)

# 文件名关键词 -> 资料类型
_TEXTBOOK_HINTS = ("教材", "课本", "考试辅导")
_PAST_PAPER_HINTS = ("真题", "历年考题", "题干", "试卷")

_YEAR_RE = re.compile(r"(20\d{2})\s*年")


def extract_year(filename: str) -> int | None:
    """从文件名识别年份（如“2016年真题”）；无法识别或超出范围返回 None。"""
    match = _YEAR_RE.search(filename or "")
    if not match:
        return None
    year = int(match.group(1))
    return year if 2000 <= year <= 2100 else None


@dataclass(frozen=True)
class CourseFile:
    """课程下的一个 PDF 资料。"""

    path: Path
    kind: str  # textbook | past_papers | other
    size_mb: float
    year: int | None  # 从文件名识别的年份，如 2016；无法识别为 None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def kind_label(self) -> str:
        return {
            "textbook": "辅导教材",
            "past_papers": "历年真题",
            "other": "其他资料",
        }.get(self.kind, self.kind)


def course_by_key(key: str) -> Course | None:
    return COURSES.get(key)


def match_course_key(filename: str) -> str | None:
    """根据文件名匹配课程；无法识别时返回 None。"""
    for keyword, course_key in _NAME_PATTERNS:
        if keyword in filename:
            return course_key
    return None


def classify_file(filename: str) -> str:
    if any(hint in filename for hint in _PAST_PAPER_HINTS):
        return "past_papers"
    if any(hint in filename for hint in _TEXTBOOK_HINTS):
        return "textbook"
    return "other"


def _iter_pdf_files(root: Path, folder_key: str | None = None) -> list[tuple[Path, str | None]]:
    if not root.is_dir():
        return []
    pdfs: list[tuple[Path, str | None]] = []
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix.lower() == ".pdf":
            pdfs.append((path, folder_key))
        elif path.is_dir() and path.name in COURSES:
            pdfs.extend(_iter_pdf_files(path, path.name))
    return pdfs


def discover_sources() -> dict[str, list[CourseFile]]:
    """扫描本地资料目录，返回每个课程下的 PDF 列表。"""
    result: dict[str, list[CourseFile]] = {key: [] for key in COURSES}
    seen: set[Path] = set()
    for pdf, folder_key in _iter_pdf_files(DATA_DIR):
        course_key = folder_key or match_course_key(pdf.name)
        if course_key is None or pdf in seen:
            continue
        seen.add(pdf)
        try:
            size_mb = round(pdf.stat().st_size / (1024 * 1024), 2)
        except OSError:
            size_mb = 0.0
        result[course_key].append(
            CourseFile(
                path=pdf,
                kind=classify_file(pdf.name),
                size_mb=size_mb,
                year=extract_year(pdf.name),
            )
        )
    return result


def source_count_labels(files: list[CourseFile]) -> str:
    counts: dict[str, int] = {}
    for file in files:
        counts[file.kind] = counts.get(file.kind, 0) + 1
    label = []
    for kind in ("textbook", "past_papers", "other"):
        if counts.get(kind):
            label.append(f"{kind_label(kind)} {counts[kind]} 份")
    return "、".join(label)


def kind_label(kind: str) -> str:
    return {
        "textbook": "教材",
        "past_papers": "真题",
        "other": "其他资料",
    }.get(kind, kind)


def readable_filename(path: str | Path) -> str:
    """返回适合展示、去除多余空白的文件名。"""
    return re.sub(r"\s+", " ", os.path.basename(str(path))).strip()
