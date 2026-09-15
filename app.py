"""CPA 智能备考助手 - 网络版入口。

运行：streamlit run app.py
- 未登录：只展示“登录 / 注册”账号页；
- 已登录：展示六门课学习导航（首页画像、教材研读、模拟考试、
  未掌握知识点、错题与记录、知识库与设置、我的账号）。
- 侧边栏顶部可选择备考科目，科目选择跨页面保留。

学生注册后所有学习数据（模考、错题、弱项）都按账号隔离，
数据文件保存在服务器/本机 data/cpa_user.db。
"""

from __future__ import annotations

import os

import streamlit as st

# 部署到 Streamlit Community Cloud 时，密钥配置写在 Secrets（st.secrets）里，
# 而业务模块统一通过 os.getenv 读取配置，这里在入口把 Secrets 注入为环境变量。
# 本地无 Secrets 时 st.secrets 为空 dict，无任何影响。
try:
    for _key, _value in st.secrets.items():
        if isinstance(_value, (str, int, float)) and _key and not os.environ.get(_key):
            os.environ[_key] = str(_value)
except Exception:
    pass

from cpa_tutor.accounts import AccountStore
from cpa_tutor.courses import COURSES, COURSE_KEYS, course_by_key
from cpa_tutor.quota import quota_exhausted, quota_status

st.set_page_config(
    page_title="CPA 智能备考助手",
    page_icon=":material/school:",
    layout="wide",
    initial_sidebar_state="auto",
)


# ---------------------------------------------------------------- 登录状态

def _account_store() -> AccountStore:
    return AccountStore()


st.session_state.setdefault("auth_user", None)
auth_user = st.session_state["auth_user"]
if auth_user is not None:
    # 每次重跑都校验账号仍然有效，避免数据库被清理后残留“伪登录”
    fresh = _account_store().get_user(int(auth_user["id"]))
    if fresh is None:
        auth_user = None
        st.session_state["auth_user"] = None
    else:
        st.session_state["auth_user"] = fresh
        auth_user = fresh

is_logged_in = auth_user is not None
is_developer = bool(auth_user and auth_user.get("role") == "developer")
# 免费登录次数用完：只保留账号页（提示注册 / 联系管理员），锁定学习功能
quota_locked = bool(auth_user and quota_exhausted(auth_user))


# ---------------------------------------------------------------- 侧边栏

if is_logged_in:
    with st.sidebar:
        st.markdown(
            f"### :material/account_circle: {auth_user['nickname']}"
        )
        if auth_user.get("phone"):
            st.caption(f"账号：{auth_user['phone']}")
        elif auth_user.get("account"):
            st.caption(f"账号：{auth_user['account']}")
        st.caption(quota_status(auth_user))
        if is_developer:
            st.caption(":material/admin_panel_settings: 开发者 · 可管理知识库与设置")
        st.markdown("##### :material/library_books: 备考科目")

    # 课程选择放在入口，所有页面共享，并跨页面保留
    course_options = {course_by_key(key).name: key for key in COURSE_KEYS}
    selected_name = st.sidebar.selectbox(
        "备考科目",
        options=list(course_options),
        key="selected_course_name",
        persist_state="session",
        label_visibility="collapsed",
    )
    st.session_state["selected_course_key"] = course_options[selected_name]


# ---------------------------------------------------------------- 页面导航

if not is_logged_in:
    pages = [
        st.Page(
            "app_pages/account.py",
            title="登录 / 注册",
            icon=":material/login:",
            default=True,
        ),
    ]
elif quota_locked:
    pages = [
        st.Page(
            "app_pages/account.py",
            title="账号 · 免费次数已用完",
            icon=":material/lock:",
            default=True,
        ),
    ]
else:
    pages = {
        "学习": [
            st.Page(
                "app_pages/home.py",
                title="学习首页",
                icon=":material/dashboard:",
                default=True,
            ),
            st.Page(
                "app_pages/review.py",
                title="教材研读",
                icon=":material/menu_book:",
            ),
            st.Page(
                "app_pages/mock_exam.py",
                title="模拟考试",
                icon=":material/quiz:",
            ),
            st.Page(
                "app_pages/weak_points.py",
                title="未掌握知识点",
                icon=":material/psychology:",
            ),
            st.Page(
                "app_pages/wrong_book.py",
                title="错题与记录",
                icon=":material/assignment_turned_in:",
            ),
        ],
        "账号": [
            st.Page(
                "app_pages/account.py",
                title="我的账号",
                icon=":material/person:",
            ),
        ],
    }
    if is_developer:
        pages = {
            "学习": pages["学习"],
            "开发者": [
                st.Page(
                    "app_pages/settings.py",
                    title="知识库与设置",
                    icon=":material/tune:",
                ),
            ],
            "账号": pages["账号"],
        }

st.navigation(pages, position="sidebar").run()
