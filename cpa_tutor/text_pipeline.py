"""PDF 文本提取、可读性判断与分块。

CPA 教材/真题可能同时存在三种情况：
  1. 带文本层的 PDF：pypdf 直接提取；
  2. 扫描图片版 PDF：可选用 Tesseract OCR（需安装中文语言包）；
  3. 带自定义字形但无 Unicode 映射的 PDF：提取结果为乱码，被识别为不可读。
分块结果会缓存为 gzip JSON，避免每次启动都重新解析 PDF。
"""

from __future__ import annotations

import gzip
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from cpa_tutor.courses import extract_year

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - 依赖缺失时的降级
    PdfReader = None


CHAPTER_RE = re.compile(
    r"第\s*[0-9一二三四五六七八九十百千万零两]+\s*[章篇编部分节]\s*[^\n。；]{0,40}"
)
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(_CJK_RE.findall(text)) / max(len(text), 1)


def _alnum_count(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum() or ch.isdigit())


def looks_readable(text: str, min_chars: int = 18) -> bool:
    """粗略判断提取出的文本是否可读（非乱码/空页）。

    扫描版教材提取出的乱码大多是散布的不可打印控制字符，
    可读页至少应包含足够多的中文字符/字母数字。
    """
    text = text or ""
    if len(text.strip()) < 6:
        return False
    alnum = _alnum_count(text)
    if alnum < min_chars:
        return False
    cjk_count = len(_CJK_RE.findall(text))
    if cjk_count >= 3:
        return True
    # 极少数页面可能没有中文（如英文/表格），用英文单词数量兜底
    word_count = sum(len(word) >= 2 for word in re.findall(r"[A-Za-z]+", text))
    return word_count >= 5


def extract_pdf_pages(
    pdf_path: str | Path,
    ocr: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """逐页提取 PDF 文本。

    返回 [{"page": 页码(从1开始), "text": 提取文本, "readable": bool, "method": str}]
    """
    pdf_path = Path(pdf_path)
    pages: list[dict[str, Any]] = []
    if PdfReader is None:
        return pages

    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)
    for index, page in enumerate(reader.pages, start=1):
        if progress:
            progress(index, total)
        try:
            text = (page.extract_text() or "").replace("\x00", " ")
            text = re.sub(r"[ \t]+", " ", text)
        except Exception:
            text = ""
        method = "pdf"
        readable = looks_readable(text)
        if not readable and ocr:
            ocr_text = _ocr_page(pdf_path, index)
            if ocr_text:
                text, method, readable = ocr_text, "ocr", True
        pages.append({"page": index, "text": text, "readable": readable, "method": method})
    return pages


def _ocr_page(pdf_path: Path, page_no: int) -> str:
    """调用 Tesseract 对单页扫描 PDF 做 OCR。"""
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
    except Exception:
        return ""
    try:
        images = convert_from_bytes(
            pdf_path.read_bytes(),
            first_page=page_no,
            last_page=page_no,
            dpi=200,
        )
        if not images:
            return ""
        return pytesseract.image_to_string(images[0], lang="chi_sim+eng") or ""
    except Exception:
        return ""


def split_pages_into_chunks(
    pages: list[dict[str, Any]],
    source_name: str,
    chunk_size: int = 900,
    overlap: int = 160,
) -> list[dict[str, Any]]:
    """把可读页按段落切分为带来源与章节目录信息的分块。"""
    readable_pages = [page for page in pages if page.get("readable")]
    units: list[tuple[int, str]] = []
    for page in readable_pages:
        text = (page.get("text") or "").strip()
        if not text:
            continue
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        # 有些 PDF 每行一个自然段，把连续短行聚合成段落
        merged = _merge_short_lines(paragraphs)
        for paragraph in merged:
            if not paragraph.strip():
                continue
            units.append((page["page"], paragraph))

    chunks: list[dict[str, Any]] = []
    buffer_parts: list[tuple[int, str]] = []
    buffer_len = 0
    tail_text = ""
    tail_page = 0

    for page_no, paragraph in units:
        if len(paragraph) > chunk_size * 2:
            # 超长段落单独强制切分
            if buffer_parts:
                chunks.append(
                    _make_chunk(
                        _join_buffer(buffer_parts),
                        source_name,
                        buffer_parts[0][0],
                        buffer_parts[-1][0],
                    )
                )
                buffer_parts, buffer_len = [], 0
            for start in range(0, len(paragraph), chunk_size):
                pieces = paragraph[start : start + chunk_size]
                chunks.append(
                    _make_chunk(pieces, source_name, page_no, page_no)
                )
            continue

        if buffer_parts and buffer_len + len(paragraph) > chunk_size:
            # 让下一块从上一块末尾取少量重叠，保证跨块语义连续
            if tail_text and tail_page:
                buffer_parts = [(tail_page, tail_text)]
                buffer_len = len(tail_text)
            else:
                buffer_parts, buffer_len = [], 0

        if not buffer_parts:
            buffer_parts.append((page_no, paragraph))
            buffer_len = len(paragraph)
        else:
            buffer_parts.append((page_no, paragraph))
            buffer_len += 1 + len(paragraph)

        # 达到容量即落块，并把末尾重叠文本留给下一块
        if buffer_len >= chunk_size:
            chunks.append(
                _make_chunk(
                    _join_buffer(buffer_parts),
                    source_name,
                    buffer_parts[0][0],
                    buffer_parts[-1][0],
                )
            )
            tail_text = _safe_tail(_join_buffer(buffer_parts), overlap)
            tail_page = buffer_parts[-1][0]
            buffer_parts, buffer_len = [], 0

    if buffer_parts:
        chunks.append(
            _make_chunk(
                _join_buffer(buffer_parts),
                source_name,
                buffer_parts[0][0],
                buffer_parts[-1][0],
            )
        )

    # 兜底：仍可能有少量超过上限的块
    refined: list[dict[str, Any]] = []
    for chunk in chunks:
        if len(chunk["text"]) > chunk_size * 1.2:
            refined.extend(_force_split(chunk, chunk_size, overlap))
        else:
            refined.append(chunk)
    return refined


def _join_buffer(buffer_parts: list[tuple[int, str]]) -> str:
    parts = [text for _, text in buffer_parts if text]
    return "\n".join(parts)


def _merge_short_lines(paragraphs: list[str]) -> list[str]:
    merged: list[str] = []
    for paragraph in paragraphs:
        if merged and len(paragraph) < 45 and len(merged[-1]) > 40:
            merged[-1] = f"{merged[-1]}{paragraph}"
        else:
            merged.append(paragraph)
    return merged


def _safe_tail(text: str, overlap: int) -> str:
    if len(text) <= overlap:
        return text
    cut = text[-overlap:]
    # 尽量从换行或句号处断开，避免从字符中间截断
    for sep in ("。", "；", "\n", "，"):
        idx = cut.find(sep)
        if idx != -1:
            cut = cut[idx + 1 :]
            break
    return cut


def _make_chunk(text: str, source_name: str, first_page: int, last_page: int) -> dict[str, Any]:
    chapter = infer_chapter(text)
    return {
        "text": text.strip(),
        "source": source_name,
        "kind": _kind_hint(source_name),
        "chapter": chapter,
        "page_start": first_page,
        "page_end": last_page,
        "year": extract_year(source_name),
    }


def _force_split(chunk: dict[str, Any], chunk_size: int, overlap: int) -> list[dict[str, Any]]:
    text = chunk["text"]
    if len(text) <= chunk_size:
        return [chunk]
    parts: list[dict[str, Any]] = []
    start = 0
    page_range = (chunk.get("page_start", 1), chunk.get("page_end", 1))
    while start < len(text):
        end = start + chunk_size
        part_text = text[start:end]
        parts.append(_make_chunk(part_text, chunk["source"], page_range[0], page_range[1]))
        if end >= len(text):
            break
        start = end - overlap
    return parts


def infer_chapter(text: str) -> str:
    """从文本开头识别“第X章”或“第X编/篇”标题。"""
    head = re.sub(r"\s+", " ", text[:600])
    match = CHAPTER_RE.search(head)
    if match:
        return match.group(0).strip()
    match = re.search(r"第\s*[0-9一二三四五六七八九十百千万零两]+\s*[章篇编部分节]", head)
    return match.group(0).strip() if match else "未标注章节"


def _kind_hint(source_name: str) -> str:
    if "真题" in source_name or "题干" in source_name:
        return "真题"
    if "教材" in source_name:
        return "教材"
    return "资料"


# ---------------------------------------------------------------- 缓存


def save_chunk_cache(course_key: str, chunks: list[dict[str, Any]], meta: dict[str, Any]) -> Path:
    from cpa_tutor.config import CACHE_DIR

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "course_key": course_key,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "meta": meta,
        "chunks": chunks,
    }
    path = CACHE_DIR / f"{course_key}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return path


def load_chunk_cache(course_key: str) -> dict[str, Any] | None:
    from cpa_tutor.config import CACHE_DIR

    path = CACHE_DIR / f"{course_key}.json.gz"
    if not path.is_file():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None
