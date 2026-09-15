"""课程知识库：PDF 解析缓存、向量检索、关键词兜底检索。"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from cpa_tutor.config import CHROMA_DIR, embedding_config
from cpa_tutor.courses import COURSES, CourseFile, discover_sources
from cpa_tutor.text_pipeline import (
    extract_pdf_pages,
    load_chunk_cache,
    looks_readable,
    save_chunk_cache,
    split_pages_into_chunks,
)

try:
    import chromadb
except ImportError:  # pragma: no cover
    chromadb = None


# ---------------------------------------------------------------- 解析与缓存


def course_status(course_key: str) -> dict[str, Any]:
    """返回某课程的资料与索引状态（供界面展示，不做耗时解析）。"""
    files = discover_sources().get(course_key, [])
    cache = load_chunk_cache(course_key)
    chunks = (cache or {}).get("chunks", [])
    meta = (cache or {}).get("meta", {})
    file_stats = meta.get("file_stats", {})
    readable_chars = sum(int(stats.get("readable_chars", 0)) for stats in file_stats.values())
    return {
        "course_key": course_key,
        "files": files,
        "has_cache": cache is not None,
        "chunk_count": len(chunks),
        "readable_chars": readable_chars,
        "vector_count": vector_count(course_key),
        "cache_meta": meta,
    }


def _stats_for_files(
    files: list[CourseFile],
    ocr: bool = False,
    progress: Callable[[str, float], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """解析全部 PDF，返回分块列表与每个文件的统计信息。"""
    chunks: list[dict[str, Any]] = []
    file_stats: dict[str, dict[str, Any]] = {}
    for file in files:
        if progress:
            progress(f"正在解析：{file.name}", 0.0)
        pages: list[dict[str, Any]] = []

        def _page_progress(page_no: int, total: int) -> None:
            if progress:
                progress(f"正在解析：{file.name}（{page_no}/{total} 页）", page_no / max(total, 1))

        try:
            pages = extract_pdf_pages(file.path, ocr=ocr, progress=_page_progress)
        except Exception as exc:  # 单个文件失败不应中断整门课程
            if progress:
                progress(f"解析失败：{file.name}（{exc}）", 1.0)
        readable = [page for page in pages if page.get("readable")]
        unreadable = len(pages) - len(readable)
        if readable:
            file_chunks = split_pages_into_chunks(readable, file.name)
            chunks.extend(file_chunks)
        readable_chars = sum(len(page.get("text", "")) for page in readable)
        file_stats[file.name] = {
            "kind": file.kind,
            "year": file.year,
            "pages": len(pages),
            "readable_pages": len(readable),
            "unreadable_pages": unreadable,
            "readable_chars": readable_chars,
            "chunk_count": len(readable),
            "ocr_enabled": bool(ocr),
        }
    return chunks, file_stats


def refresh_course_cache(
    course_key: str,
    ocr: bool = False,
    progress: Callable[[str, float], None] | None = None,
) -> dict[str, Any]:
    """从本地 PDF 重建某课程的文本分块缓存（不调用外部模型）。"""
    files = discover_sources().get(course_key, [])
    if not files:
        return {"ok": False, "message": "该课程目录下没有 PDF 文件。", "files": files}
    chunks, file_stats = _stats_for_files(files, ocr=ocr, progress=progress)
    if not chunks:
        return {
            "ok": False,
            "message": (
                "未能从 PDF 中提取到可读文本。若教材是扫描版，请在“知识库与设置”页"
                "开启 OCR 后重试，或提供带文本层的教材 PDF。"
            ),
            "files": files,
            "file_stats": file_stats,
        }
    meta = {
        "source_root": str(files[0].path.parent),
        "file_stats": file_stats,
        "chunk_total": len(chunks),
        "built_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_chunk_cache(course_key, chunks, meta)
    return {
        "ok": True,
        "chunks": chunks,
        "meta": meta,
        "files": files,
        "file_stats": file_stats,
    }


def load_course_chunks(course_key: str) -> list[dict[str, Any]]:
    cache = load_chunk_cache(course_key)
    return (cache or {}).get("chunks", [])


def cache_modified_time(course_key: str) -> float:
    from cpa_tutor.config import CACHE_DIR

    path = CACHE_DIR / f"{course_key}.json.gz"
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def readable_ratio_label(stats: dict[str, Any]) -> str:
    file_stats = (stats.get("cache_meta") or {}).get("file_stats", {})
    if not file_stats:
        return "尚未解析"
    total_pages = sum(int(item.get("pages", 0)) for item in file_stats.values())
    readable_pages = sum(int(item.get("readable_pages", 0)) for item in file_stats.values())
    return f"可读 {readable_pages}/{total_pages} 页"


# ---------------------------------------------------------------- Embedding


def make_embedder(cfg: dict[str, Any] | None = None):
    cfg = cfg or embedding_config()
    if cfg.get("provider") == "api":
        return _ApiEmbedder(cfg)
    return _LocalEmbedder(cfg)


class EmbedderError(RuntimeError):
    pass


class _ApiEmbedder:
    """OpenAI 兼容 /embeddings 接口。"""

    def __init__(self, cfg: dict[str, Any]):
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.api_key = cfg.get("api_key") or ""
        self.model = cfg.get("model") or "text-embedding-v1"

    @property
    def name(self) -> str:
        return f"api:{self.model}"

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        import requests

        if not self.api_key or not self.base_url:
            raise EmbedderError("远程向量化未配置 API Key，请检查 .env 或设置页。")
        result: list[list[float]] = []
        batch_size = 20
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            try:
                response = requests.post(
                    f"{self.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "input": batch},
                    timeout=120,
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                raise EmbedderError(f"向量化接口调用失败：{exc}") from exc
            data = response.json().get("data") or []
            if len(data) != len(batch):
                raise EmbedderError("向量化接口返回数量与请求不一致。")
            ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
            result.extend([item["embedding"] for item in ordered])
        return result


class _LocalEmbedder:
    """本地 sentence-transformers（首次加载需下载模型）。"""

    _model = None
    _model_name = ""

    def __init__(self, cfg: dict[str, Any]):
        self.model_name = cfg.get("model") or ""
        self._load()

    @property
    def name(self) -> str:
        return f"local:{self.model_name}"

    def _load(self) -> None:
        if _LocalEmbedder._model is None or _LocalEmbedder._model_name != self.model_name:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover
                raise EmbedderError("未安装 sentence-transformers。") from exc
            _LocalEmbedder._model = SentenceTransformer(self.model_name)
            _LocalEmbedder._model_name = self.model_name

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if _LocalEmbedder._model is None:
            raise EmbedderError("本地向量模型尚未加载。")
        vectors = _LocalEmbedder._model.encode(
            texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False
        )
        return [list(map(float, row)) for row in vectors]


# ---------------------------------------------------------------- Chroma 索引


def _chunk_id(chunk: dict[str, Any]) -> str:
    raw = f"{chunk.get('source','')}|{chunk.get('page_start',0)}|{chunk.get('text','')[:120]}"
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:24]


def _installed_chroma_migrations() -> dict[tuple[str, str], str]:
    """当前安装 chromadb 自带的迁移文件映射 (目录, 文件名) -> SQL 文本。"""
    migrations: dict[tuple[str, str], str] = {}
    if chromadb is None:
        return migrations
    migration_root = Path(chromadb.__file__).resolve().parent / "migrations"
    if not migration_root.is_dir():
        return migrations
    for directory in migration_root.iterdir():
        if not directory.is_dir():
            continue
        for migration_file in directory.glob("[0-9][0-9][0-9][0-9][0-9]-*.sqlite.sql"):
            try:
                migrations[(directory.name, migration_file.name)] = migration_file.read_text(
                    encoding="utf-8"
                )
            except OSError:
                continue
    return migrations


def _chroma_store_version_mismatch(target_dir: Path) -> bool:
    """判断既有 Chroma 目录是否由其它版本的 chromadb 生成。

    当数据库里的迁移记录不是当前安装版本所拥有（或内容不一致）时，说明该库
    由其它版本创建；chromadb 只支持按当前代码版本向前升级，旧代码无法读写
    这种库，只能删除索引目录后按当前版本重建。
    """
    if chromadb is None:
        return False
    sqlite_path = target_dir / "chroma.sqlite3"
    if not sqlite_path.is_file():
        return False
    installed = _installed_chroma_migrations()
    try:
        import sqlite3

        conn = sqlite3.connect(str(sqlite_path))
        try:
            rows = conn.execute("SELECT dir, filename, sql FROM migrations").fetchall()
        finally:
            conn.close()
    except Exception:
        return False
    return any(installed.get((directory, filename)) != sql for directory, filename, sql in rows)


def vector_count(course_key: str) -> int:
    """读取该课程独立 Chroma 集合中的向量数（只读）。"""
    if chromadb is None:
        return 0
    target_dir = CHROMA_DIR / course_key
    if not target_dir.is_dir() or not (target_dir / "chroma.sqlite3").is_file():
        return 0
    try:
        client = chromadb.PersistentClient(path=str(target_dir))
        try:
            collection = client.get_collection("cpa_chunks")
            return collection.count()
        except Exception:
            return 0
    except Exception:
        return 0


def build_vector_index(
    course_key: str,
    chunks: list[dict[str, Any]] | None = None,
    embedder: Any | None = None,
    progress: Callable[[str, float], None] | None = None,
) -> dict[str, Any]:
    """把缓存分块写入课程独立的 Chroma 集合。"""
    if chromadb is None:
        return {"ok": False, "message": "未安装 chromadb，无法建立向量索引。"}
    chunks = load_course_chunks(course_key) if chunks is None else chunks
    if not chunks:
        return {"ok": False, "message": "暂无文本分块，请先在“知识库与设置”页完成 PDF 解析。"}
    embedder = embedder or make_embedder()
    try:
        dimension = len(embedder.embed_texts([chunks[0]["text"][:200]])[0])
    except Exception as exc:
        return {"ok": False, "message": f"向量模型初始化失败：{exc}"}

    target_dir = CHROMA_DIR / course_key
    target_dir.mkdir(parents=True, exist_ok=True)
    reset = _chroma_store_version_mismatch(target_dir)
    if reset:
        # 旧库由其它版本 chromadb 创建，当前环境无法读写；删除后按当前版本重建。
        delete_vector_index(course_key)
        target_dir.mkdir(parents=True, exist_ok=True)
    try:
        client = chromadb.PersistentClient(path=str(target_dir))
        collection = client.get_or_create_collection(
            "cpa_chunks",
            metadata={"hnsw:space": "cosine", "embedding_model": embedder.name, "dimension": dimension},
        )
        ids = [_chunk_id(chunk) for chunk in chunks]
        existing: set[str] = set()
        try:
            existing = set(collection.get(ids=ids, include=[]).get("ids", []))
        except Exception:
            existing = set()

        pending: list[tuple[str, dict[str, Any]]] = [
            (chunk_id, chunk)
            for chunk_id, chunk in zip(ids, chunks)
            if chunk_id not in existing
        ]
        if pending:
            for start in range(0, len(pending), 20):
                batch = pending[start : start + 20]
                batch_ids = [item[0] for item in batch]
                batch_chunks = [item[1] for item in batch]
                if progress:
                    progress(
                        f"向量化中：{len(batch_ids)} 条 / 本批",
                        (start + len(batch_ids)) / max(len(pending), 1),
                    )
                try:
                    vectors = embedder.embed_texts([item["text"] for item in batch_chunks])
                except Exception as exc:
                    return {
                        "ok": False,
                        "message": f"向量化第 {start + 1} 条时失败：{exc}",
                    }
                collection.add(
                    ids=batch_ids,
                    embeddings=vectors,
                    documents=[item["text"] for item in batch_chunks],
                    metadatas=[
                        {
                            "source": item.get("source", ""),
                            "kind": item.get("kind", "资料"),
                            "chapter": item.get("chapter", ""),
                            "page_start": int(item.get("page_start", 0)),
                            "year": item.get("year"),
                        }
                        for item in batch_chunks
                    ],
                )
        return {
            "ok": True,
            "added": len(pending),
            "skipped": len(ids) - len(pending),
            "total": len(ids),
            "dimension": dimension,
            "embedder": embedder.name,
            "note": "已重置由其它版本 chromadb 生成的旧索引。" if reset else "",
        }
    except Exception as exc:
        return {"ok": False, "message": f"向量索引写入失败：{exc}"}


def delete_vector_index(course_key: str) -> None:
    target = CHROMA_DIR / course_key
    if not target.is_dir():
        return
    import shutil

    shutil.rmtree(target, ignore_errors=True)


# ---------------------------------------------------------------- 检索

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def _tokens(text: str) -> list[str]:
    """中文按字符、英文/数字按词切分，便于关键词兜底检索。"""
    tokens: list[str] = []
    lowered = text.lower()
    for match in _TOKEN_RE.finditer(lowered):
        token = match.group(0)
        if token and token[0] >= "\u4e00":
            tokens.append(token)
        else:
            tokens.append(token)
    # 对超过 2 个字符的查询增加相邻二元组，提高中文词组匹配能力
    if len(tokens) >= 2:
        tokens.extend(
            f"{tokens[i]}{tokens[i + 1]}"
            for i in range(len(tokens) - 1)
            if tokens[i][0] >= "\u4e00" and tokens[i + 1][0] >= "\u4e00"
        )
    return tokens


class KeywordRetriever:
    """不使用外部模型的关键词检索（TF-IDF 风格），保证应用可离线运行。"""

    def __init__(self, chunks: list[dict[str, Any]]):
        self.chunks = chunks
        self.doc_count = len(chunks)
        self._doc_tokens = [_tokens(chunk["text"]) for chunk in chunks]

    def search(
        self,
        query: str,
        k: int = 6,
        chapter: str | None = None,
        kind: str | None = None,
        year: int | None = None,
    ) -> list[dict[str, Any]]:
        query_tokens = list(dict.fromkeys(_tokens(query)))
        if not query_tokens:
            return []
        scores: list[tuple[float, int]] = []
        for index, doc_tokens in enumerate(self._doc_tokens):
            chunk = self.chunks[index]
            if chapter and chapter != "全部章节" and chunk.get("chapter", "") != chapter:
                continue
            if kind and kind != "全部" and chunk.get("kind") != kind:
                continue
            if year and chunk.get("year") != int(year):
                continue
            score = 0.0
            for token in query_tokens:
                count = doc_tokens.count(token)
                if count:
                    df = sum(1 for tokens in self._doc_tokens if token in tokens)
                    idf = math.log((self.doc_count + 1) / (df + 1)) + 1
                    score += count * idf * (1.0 if len(token) > 1 else 0.55)
            if score > 0:
                scores.append((score, index))
        scores.sort(key=lambda item: item[0], reverse=True)
        return [
            {
                "text": self.chunks[index]["text"],
                "metadata": self.chunks[index],
                "score": round(score, 4),
            }
            for score, index in scores[:k]
        ]


class VectorRetriever:
    """课程独立 Chroma 向量检索；失败时上层会自动回退关键词。"""

    def __init__(self, course_key: str, path: Path, collection_name: str = "cpa_chunks"):
        self.course_key = course_key
        self.client = chromadb.PersistentClient(path=str(path))
        self.collection = self.client.get_collection(collection_name)

    def search(
        self,
        query: str,
        k: int = 6,
        embedder: Any | None = None,
        chapter: str | None = None,
        kind: str | None = None,
        year: int | None = None,
    ) -> list[dict[str, Any]]:
        embedder = embedder or make_embedder()
        where: dict[str, Any] | None = None
        if chapter and chapter != "全部章节":
            where = {"chapter": chapter}
        if kind and kind != "全部":
            where = {"kind": kind} if where is None else {"$and": [where, {"kind": kind}]}
        if year:
            where = (
                {"year": int(year)}
                if where is None
                else {"$and": [where, {"year": int(year)}]}
            )
        vector = embedder.embed_texts([query])[0]
        payload = self.collection.query(
            query_embeddings=[vector],
            n_results=k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        results: list[dict[str, Any]] = []
        documents = (payload.get("documents") or [[]])[0]
        metadatas = (payload.get("metadatas") or [[]])[0]
        distances = (payload.get("distances") or [[]])[0]
        for text, metadata, distance in zip(documents, metadatas, distances):
            results.append(
                {
                    "text": text,
                    "metadata": metadata or {},
                    "score": round(max(0.0, 1 - float(distance)), 4),
                }
            )
        return results


def best_retriever(course_key: str):
    """优先向量检索，其次关键词检索；都没有则返回 None。"""
    chunks = load_course_chunks(course_key)
    vector_path = CHROMA_DIR / course_key
    if chromadb is not None and (vector_path / "chroma.sqlite3").is_file():
        try:
            return VectorRetriever(course_key, vector_path)
        except Exception:
            pass
    if chunks:
        return KeywordRetriever(chunks)
    return None


def search_knowledge(
    course_key: str,
    query: str,
    k: int = 5,
    retriever: Any | None = None,
    embed_cfg: dict[str, Any] | None = None,
    year: int | None = None,
) -> list[dict[str, Any]]:
    """带回退的检索：向量失败自动转关键词；year 可按真题年份过滤。"""
    retriever = retriever or best_retriever(course_key)
    if retriever is None:
        return []
    if isinstance(retriever, VectorRetriever):
        try:
            return retriever.search(
                query, k=k, embedder=make_embedder(embed_cfg), year=year
            )
        except Exception:
            fallback = KeywordRetriever(load_course_chunks(course_key))
            return fallback.search(query, k=k, year=year)
    return retriever.search(query, k=k, year=year)


# ---------------------------------------------------------------- 教材浏览


def build_chapter_index(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按章节出现的先后顺序聚合教材/真题分块。"""
    order: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        chapter = chunk.get("chapter") or "未标注章节"
        if chapter not in groups:
            order.append(chapter)
            groups[chapter] = []
        groups[chapter].append(chunk)
    return [
        {
            "chapter": chapter,
            "chunk_count": len(groups[chapter]),
            "chunks": groups[chapter],
            "char_total": sum(len(c["text"]) for c in groups[chapter]),
        }
        for chapter in order
    ]


def compile_review_text(
    chapters: list[dict[str, Any]],
    chapter_label: str,
    max_chars: int = 2600,
) -> str:
    """把某章节的分块拼接为适合连续阅读的文本。"""
    selected = next((item for item in chapters if item["chapter"] == chapter_label), None)
    if not selected:
        return ""
    lines: list[str] = []
    used = 0
    for chunk in selected["chunks"]:
        if used >= max_chars:
            lines.append("\n……（后续内容可继续选择“继续阅读”或使用 AI 答疑）")
            break
        source = chunk.get("source", "")
        page = chunk.get("page_start", "")
        lines.append(f"（{source} · 第 {page} 页）\n{chunk['text']}")
        used += len(chunk["text"])
    return "\n\n".join(lines)
