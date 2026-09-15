"""页面共享工具：读取当前登录学生身份。

真实入口 app.py 已在导航层做登录门禁；本模块仅用于让页面统一、安全地
获取 user_id，避免每个页面重复读取 session_state。
"""

from __future__ import annotations

from typing import Any

import streamlit as st


def current_user() -> dict[str, Any]:
    return st.session_state.get("auth_user") or {}


def current_user_id() -> int:
    user = current_user()
    raw = user.get("id")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def require_user() -> dict[str, Any]:
    user = current_user()
    if not user or not current_user_id():
        st.warning("请先登录后再使用学习功能。", icon=":material/login:")
        st.stop()
    return user


def is_developer(user: dict[str, Any] | None = None) -> bool:
    if user is None:
        user = current_user()
    return str(user.get("role") or "") == "developer"


def require_developer() -> dict[str, Any]:
    """只允许开发者访问：用于“知识库与设置”等运维/后台页面。"""
    user = require_user()
    if not is_developer(user):
        st.error(
            "该页面仅限应用开发者使用。如需配置知识库或模型，请联系管理员。",
            icon=":material/admin_panel_settings:",
        )
        st.stop()
    return user


def page_guide(
    title: str = "本页怎么用",
    steps: list[str] | tuple[str, ...] = (),
    *,
    expanded: bool = False,
    icon: str = ":material/tips_and_updates:",
) -> None:
    """在每个功能页顶部渲染统一、清晰的数字分步操作指引。"""
    if not steps:
        return
    with st.expander(f"{icon} {title}", expanded=expanded):
        for index, step in enumerate(steps, start=1):
            st.markdown(f"{index}. {step}")
