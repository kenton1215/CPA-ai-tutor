"""应用配置：路径、模型服务、环境变量读取。

配置优先级：Streamlit 页面中的临时覆盖 > 环境变量 / .env > 内置默认值。
纯业务代码不依赖 streamlit，因此页面会把临时覆盖传入调用函数。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from cpa_tutor.courses import DATA_DIR, PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR.mkdir(parents=True, exist_ok=True)

CHROMA_DIR = PROJECT_ROOT / "chroma_db"
CACHE_DIR = PROJECT_ROOT / "data" / "kb_cache"


def get_user_db_path() -> Path:
    """返回学生数据文件路径，支持环境变量覆盖（便于部署与测试隔离）。"""
    override = os.getenv("CPA_USER_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return PROJECT_ROOT / "data" / "cpa_user.db"


USER_DB_PATH = get_user_db_path()

for _dir in (CHROMA_DIR, CACHE_DIR, PROJECT_ROOT / "data"):
    _dir.mkdir(parents=True, exist_ok=True)


def get_env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def mask_key(key: str | None) -> str:
    if not key:
        return "未配置"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"


# ---------------------------------------------------------------- LLM

DASHSCOPE_COMPAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"


def llm_config(overrides: dict | None = None) -> dict:
    """解析当前可用的对话模型配置。

    返回 dict：
      available / provider / base_url / api_key / model / temperature / max_tokens
    若无任何可用配置，available=False，并附带说明。
    """
    overrides = overrides or {}
    base_url = (
        overrides.get("base_url")
        or get_env("LLM_BASE_URL")
        or get_env("DEEPSEEK_BASE_URL")
    )
    model = overrides.get("model") or get_env("LLM_MODEL")
    api_key = (
        overrides.get("api_key")
        or get_env("LLM_API_KEY")
        or get_env("DEEPSEEK_API_KEY")
        or get_env("OPENAI_API_KEY")
        or get_env("DASHSCOPE_API_KEY")
    )

    provider = overrides.get("provider") or ""
    if base_url:
        provider = provider or ("deepseek" if "deepseek" in base_url.lower() else "custom")
        if not model:
            if provider == "deepseek" or get_env("DEEPSEEK_API_KEY"):
                model = "deepseek-chat"
            elif get_env("DASHSCOPE_API_KEY"):
                model = "qwen-plus"
            elif get_env("OPENAI_API_KEY"):
                model = "gpt-4o-mini"
    elif get_env("DEEPSEEK_API_KEY"):
        base_url = get_env("DEEPSEEK_BASE_URL")
        provider, model = "deepseek", model or "deepseek-chat"
    elif get_env("DASHSCOPE_API_KEY"):
        base_url = DASHSCOPE_COMPAT_URL
        provider, model = "dashscope", model or "qwen-plus"
    elif get_env("OPENAI_API_KEY"):
        base_url = OPENAI_BASE_URL
        provider, model = "openai", model or "gpt-4o-mini"
    else:
        # 本地 Ollama（OpenAI 兼容接口），不需要 API Key
        base_url = get_env("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
        provider, model = "ollama", model or get_env("OLLAMA_MODEL", "qwen2.5:7b")

    temperature = float(overrides.get("temperature") or get_env("LLM_TEMPERATURE") or 0.25)
    max_tokens = int(overrides.get("max_tokens") or get_env("LLM_MAX_TOKENS") or 4096)
    base_url = (base_url or "").rstrip("/")
    available = bool(base_url) and bool(model)
    return {
        "available": available,
        "provider": provider or "custom",
        "provider_label": _provider_label(provider),
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "temperature": min(max(temperature, 0.0), 1.5),
        "max_tokens": min(max(max_tokens, 512), 8192),
        "api_key_masked": mask_key(api_key),
    }


def _provider_label(provider: str) -> str:
    return {
        "dashscope": "阿里云百炼 DashScope（通义）",
        "deepseek": "DeepSeek",
        "openai": "OpenAI",
        "ollama": "本地 Ollama",
        "custom": "自定义 OpenAI 兼容服务",
    }.get(provider, provider or "自定义 OpenAI 兼容服务")


def describe_llm(cfg: dict) -> str:
    if not cfg.get("available"):
        return "未检测到可用对话模型配置。"
    key_part = f"（Key: {cfg['api_key_masked']}）" if cfg.get("api_key") else "（本地无 Key）"
    return f"{cfg['provider_label']} · {cfg['model']}{key_part}"


# ---------------------------------------------------------------- Embedding

DASHSCOPE_EMBED_MODEL = "text-embedding-v1"
LOCAL_EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def embedding_config(overrides: dict | None = None) -> dict:
    """解析向量化（Embedding）配置。

    provider: api(远程 OpenAI 兼容) | local(本地 sentence-transformers)
    """
    overrides = overrides or {}
    dashscope_key = get_env("DASHSCOPE_API_KEY")
    openai_key = get_env("OPENAI_API_KEY")
    api_key = (
        overrides.get("embed_api_key")
        or get_env("EMBEDDING_API_KEY")
        or dashscope_key
        or openai_key
    )
    if overrides.get("embed_provider"):
        provider = overrides["embed_provider"]
    elif get_env("EMBEDDING_PROVIDER"):
        provider = get_env("EMBEDDING_PROVIDER", "api")
    elif api_key:
        provider = "api"
    else:
        provider = "local"

    if provider == "local":
        return {
            "provider": "local",
            "model": get_env("EMBEDDING_MODEL_LOCAL", LOCAL_EMBED_MODEL),
            "base_url": "",
            "api_key": "",
            "dimension": None,
        }
    embed_base_url = overrides.get("embed_base_url") or get_env("EMBEDDING_BASE_URL")
    if not embed_base_url:
        if dashscope_key:
            embed_base_url = DASHSCOPE_COMPAT_URL
        elif openai_key:
            embed_base_url = OPENAI_BASE_URL
        else:
            # 只有 DeepSeek Key 等无 Embedding 服务时，自动回退本地向量模型
            return {
                "provider": "local",
                "model": get_env("EMBEDDING_MODEL_LOCAL", LOCAL_EMBED_MODEL),
                "base_url": "",
                "api_key": "",
                "dimension": None,
                "note": "未找到可用的远程 Embedding 服务，已回退本地模型。",
            }
    model = get_env("EMBEDDING_MODEL")
    if not model:
        model = (
            DASHSCOPE_EMBED_MODEL
            if "dashscope" in (embed_base_url or "").lower()
            else "text-embedding-3-small"
        )
    return {
        "provider": "api",
        "model": model,
        "base_url": (embed_base_url or "").rstrip("/"),
        "api_key": api_key or "",
        "dimension": None,
    }


# ---------------------------------------------------------------- 业务限额


def get_env_int(name: str, default: int) -> int:
    """读取整数环境变量，缺失或非法时回退默认值。"""
    raw = get_env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def free_login_limits() -> dict[str, int]:
    """免费登录次数限额：guest=未注册游客，user=注册考生（.env 可配）。"""
    return {
        "guest": max(0, get_env_int("FREE_GUEST_LOGINS", 5)),
        "user": max(0, get_env_int("FREE_USER_LOGINS", 10)),
    }


def mastery_streak_threshold() -> int:
    """类似题连续答对多少次后，自动把未掌握知识点标记为已掌握。"""
    return max(1, get_env_int("WEAK_POINT_MASTERY_STREAK", 2))


def exam_gen_workers() -> int:
    """AI 组卷并行生成题目的线程数（1-8）。"""
    return max(1, min(8, get_env_int("EXAM_GEN_WORKERS", 4)))
