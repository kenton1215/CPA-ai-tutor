"""对话模型调用（OpenAI 兼容 /chat/completions）。

支持阿里云百炼 DashScope、DeepSeek、OpenAI 及本地 Ollama。
统一只发送一次（或一次带修复的）请求，避免应用无限重试卡住界面。
"""

from __future__ import annotations

import json
import re
from typing import Any

from cpa_tutor.config import llm_config


class LLMError(RuntimeError):
    """对话模型调用失败（配置、网络或格式错误）。"""


def _post_chat(cfg: dict[str, Any], payload: dict[str, Any]) -> str:
    import requests

    url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=180)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise LLMError(f"模型接口调用失败：{exc}") from exc
    data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"模型返回内容格式异常：{data}") from exc
    if not isinstance(content, str):
        content = str(content)
    return content


def chat_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    overrides: dict[str, Any] | None = None,
) -> str:
    cfg = llm_config(overrides)
    if not cfg["available"]:
        raise LLMError("尚未配置对话模型。请在 .env 或“知识库与设置”页配置模型。")
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": float(temperature if temperature is not None else cfg["temperature"]),
        "max_tokens": int(max_tokens if max_tokens is not None else cfg["max_tokens"]),
        "stream": False,
    }
    try:
        return _post_chat(cfg, payload)
    except LLMError:
        raise
    except Exception as exc:
        raise LLMError(f"模型调用异常：{exc}") from exc


def extract_json(text: str) -> Any:
    """从模型输出中稳健地提取 JSON 对象或数组。"""
    if not text:
        raise LLMError("模型没有返回内容。")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    cleaned = cleaned.strip()
    start_candidates = []
    for ch in ("{", "["):
        idx = cleaned.find(ch)
        if idx != -1:
            start_candidates.append((idx, ch))
    if not start_candidates:
        raise LLMError("模型未输出 JSON 结构，请重试。")
    idx, opening = min(start_candidates)
    closing = "}" if opening == "{" else "]"
    try:
        return json.loads(cleaned[idx:])
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(cleaned[idx:])
        return value
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型 JSON 解析失败：{exc}") from exc


def chat_json(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.15,
    overrides: dict[str, Any] | None = None,
    repair_once: bool = True,
) -> Any:
    """请求模型输出 JSON；失败时追加纠错指令再请求一次。"""
    try:
        content = chat_completion(
            messages,
            temperature=temperature,
            max_tokens=4096,
            overrides=overrides,
        )
        return extract_json(content)
    except (LLMError, ValueError):
        if not repair_once:
            raise
        fixed_messages = list(messages)
        fixed_messages.append(
            {
                "role": "user",
                "content": (
                    "你上一次的回答不是可解析的 JSON。请重新输出："
                    "只输出一个合法 JSON 对象/数组，不要包含 ``` 标记、解释或多余文字。"
                ),
            }
        )
        content = chat_completion(
            fixed_messages,
            temperature=0.05,
            max_tokens=4096,
            overrides=overrides,
        )
        return extract_json(content)
