"""账号页：未登录时完成注册/登录，已登录时管理资料与退出。

页面由 app.py 的登录门禁控制；未登录状态下导航里只有本页。
登录成功后会清理上一个账号遗留的会话学习数据，避免串号。
"""

from __future__ import annotations

import streamlit as st
import streamlit.components.v1 as components

from cpa_tutor.accounts import AccountError, AccountStore
from cpa_tutor.config import free_login_limits, get_env
from cpa_tutor.quota import quota_exhausted, quota_status


def _qr_image_bytes(url: str) -> bytes | None:
    """生成可扫码打开的链接二维码；未安装 qrcode 时返回 None。"""
    try:
        from io import BytesIO

        import qrcode
        from qrcode.image.pil import PilImage

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=7,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        image = qr.make_image(image_factory=PilImage, fill_color="#1565A8", back_color="white")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        return None


def _reset_student_state() -> None:
    """退出/切换账号时清空会话中与学习过程相关的临时状态。"""
    prefixes = ("review_chat_", "wp_content_", "wp_chat_")
    drop_keys = {
        "exam",
        "card_idx",
        "card_owner",
        "llm_overrides",
        "ocr_enabled",
        "selected_course_key",
        "selected_course_name",
    }
    for key in list(st.session_state.keys()):
        if key in drop_keys or key.startswith(prefixes):
            del st.session_state[key]


GUEST_COOKIE = "cpa_guest_id"


def _guest_cookie_value() -> str:
    """游客 Cookie 标识：优先会话，其次 Cookie，最后新生成并写入浏览器。

    浏览器 Cookie 只能靠前端 JS 写入，下一轮 rerun 起才可被读到，
    因此本次会话同时写入 st.session_state 保证立刻可用。
    """
    token = (st.session_state.get(GUEST_COOKIE) or "").strip()
    if not token:
        try:
            token = (st.context.cookies.get(GUEST_COOKIE) or "").strip()
        except Exception:
            token = ""
    if not token:
        import uuid

        token = uuid.uuid4().hex
        st.session_state[GUEST_COOKIE] = token
        components.html(
            f"<script>document.cookie='{GUEST_COOKIE}={token};"
            "path=/;max-age=31536000';</script>",
            height=0,
        )
    return token


def _current_guest():
    """当前浏览器对应的游客账号（可能为 None）。"""
    token = (st.session_state.get(GUEST_COOKIE) or "").strip()
    if not token:
        try:
            token = (st.context.cookies.get(GUEST_COOKIE) or "").strip()
        except Exception:
            token = ""
    return store.find_guest(token) if token else None


def _enter_guest() -> None:
    """游客进入：按 Cookie 找回游客账号，不存在则创建；超次则拒绝。"""
    token = _guest_cookie_value()
    guest = store.find_guest(token)
    if guest is None:
        guest = store.create_guest(token, nickname="游客")
    if quota_exhausted(guest):
        st.error(
            "游客免费体验次数已用完。请退出后注册账号继续学习，"
            "注册时可继承当前游客的学习记录。",
            icon=":material/lock:",
        )
        return
    _reset_student_state()
    st.session_state[GUEST_COOKIE] = token
    st.session_state["auth_user"] = guest
    st.rerun()


store = AccountStore()
auth_user = st.session_state.get("auth_user")

# ---------------------------------------------------------------- 未登录

if auth_user is None:
    st.title("CPA 智能备考助手", icon=":material/school:")
    st.markdown(
        """
        面向 CPA 考生的 AI 学习平台：基于最近十年真题与 2026 年六科辅导教材，
        完成 **教材研读 → 模拟考试 → 错题诊断 → 弱项闭环复习**。
        """
    )

    public_url = (get_env("APP_PUBLIC_URL") or "").strip().rstrip("/")
    if public_url:
        with st.container(border=True):
            qr_left, qr_right = st.columns([2.1, 1], vertical_alignment="center")
            qr_left.markdown("#### :material/qr_code_scanner: 手机微信扫码进入")
            qr_left.caption(
                "用微信「扫一扫」或任意手机浏览器打开以下链接，即可注册/登录开始学习。"
            )
            qr_left.link_button(
                "在手机上打开学习平台",
                public_url,
                icon=":material/open_in_new:",
                width="stretch",
            )
            qr_bytes = _qr_image_bytes(public_url)
            if qr_bytes:
                qr_right.image(
                    qr_bytes,
                    caption="长按识别 / 微信扫一扫",
                    width=180,
                )
    else:
        st.caption(
            "本地开发阶段可用任意浏览器打开；部署后把 APP_PUBLIC_URL 填入 .env，"
            "即可在本页生成微信可扫的二维码入口。微信网页授权需开放平台凭据，"
            "上线后可增量接入自动登录。"
        )

    tab_login, tab_register = st.tabs(
        [":material/login: 登录", ":material/person_add: 注册新账号"]
    )

    with tab_login:
        with st.form("login_form", border=True):
            account = st.text_input(
                "账号 / 手机号", placeholder="请输入注册时使用的手机号或账号"
            )
            password = st.text_input("密码", type="password")
            login_clicked = st.form_submit_button(
                "登录", icon=":material/login:", type="primary", width="stretch"
            )
        if login_clicked:
            user = store.authenticate(account, password)
            if user:
                _reset_student_state()
                st.session_state["auth_user"] = user
                st.rerun()
            st.error("账号或密码不正确，请重试。", icon=":material/error:")

    with tab_register:
        inherit_guest = False
        current_guest = _current_guest()
        with st.form("register_form", border=True):
            phone = st.text_input("手机号", placeholder="用于登录与找回账号，例如 13800000000")
            nickname = st.text_input("昵称", placeholder="同学，你希望怎么称呼？")
            reg_password = st.text_input("设置密码", type="password", help="至少 6 位")
            confirm = st.text_input("确认密码", type="password")
            if current_guest is not None:
                inherit_guest = st.checkbox(
                    "继承游客学习记录（错题、未掌握知识点、考试记录）",
                    value=True,
                    help="注册后，游客身份下产生的学习记录会迁移到新账号。",
                )
            register_clicked = st.form_submit_button(
                "注册并开始学习",
                icon=":material/how_to_reg:",
                type="primary",
                width="stretch",
            )
        if register_clicked:
            if reg_password != confirm:
                st.error("两次输入的密码不一致。", icon=":material/error:")
            else:
                try:
                    user = store.register(phone, reg_password, nickname, phone=phone)
                    if inherit_guest and current_guest is not None:
                        moved = store.inherit_guest_data(
                            int(current_guest["id"]), int(user["id"])
                        )
                        st.toast(f"已继承游客学习记录（{moved} 条）")
                    _reset_student_state()
                    st.session_state["auth_user"] = user
                    st.rerun()
                except AccountError as exc:
                    st.error(str(exc), icon=":material/error:")

    limits = free_login_limits()
    st.markdown("##### :material/explore: 游客体验（无需注册）")
    st.caption(
        f"不注册可直接体验完整学习闭环，免费登录 {limits['guest']} 次；"
        f"注册账号可免费登录 {limits['user']} 次。游客身份保存在本浏览器中，"
        "注册账号时可继承游客学习记录。"
    )
    if st.button(
        "以游客身份进入",
        key="guest_entry",
        icon=":material/explore:",
        type="secondary",
        width="stretch",
    ):
        _enter_guest()

    with st.container(border=True):
        st.markdown(
            """
            #### :material/help: 新手操作指引

            1. 手机微信扫一扫本页二维码（或直接打开链接）进入平台；
            2. 可先点 **以游客身份进入** 免费体验，再 **注册新账号** 创建
               属于你的学习空间（注册时可继承游客学习记录）；
            3. 登录后在左侧选择备考科目，建议从 会计/税法/经济法 等先攻一门；
            4. 按 **教材研读 → 模拟考试 → 未掌握知识点 → 错题与记录** 的顺序学习；
            5. 系统只记录**你自己**的模考、错题与弱项，AI 会根据它们安排巩固内容。
            """
        )
    st.stop()

# ---------------------------------------------------------------- 已登录

st.title("我的账号", icon=":material/person:")

if quota_exhausted(auth_user):
    if str(auth_user.get("role") or "") == "guest":
        st.error(
            "游客免费体验次数已用完：退出登录后注册账号，"
            "可继承游客学习记录继续学习。",
            icon=":material/lock:",
        )
    else:
        st.error(
            "免费登录次数已用完：请联系管理员开通后继续学习。",
            icon=":material/lock:",
        )

col_profile, col_status = st.columns([1.4, 1], vertical_alignment="top")
with col_profile:
    with st.container(border=True):
        st.markdown(f"### :material/account_circle: {auth_user['nickname']}")
        st.caption(
            f"账号：{auth_user.get('phone') or auth_user.get('account')}　"
            f"注册时间：{(auth_user.get('created_at') or '')[:10]}"
        )
        if str(auth_user.get("role") or "") == "developer":
            st.caption(":material/admin_panel_settings: 开发者账号：拥有“知识库与设置”管理权限")
        with st.form("profile_form", border=False):
            new_nickname = st.text_input("昵称", value=auth_user["nickname"])
            saved = st.form_submit_button("保存昵称", icon=":material/save:")
        if saved:
            try:
                updated = store.update_profile(int(auth_user["id"]), new_nickname)
                st.session_state["auth_user"] = updated
                st.toast("昵称已更新")
                st.rerun()
            except AccountError as exc:
                st.error(str(exc))

with col_status:
    with st.container(border=True):
        st.markdown("### :material/confirmation_number: 免费次数")
        st.caption(quota_status(auth_user))
        if str(auth_user.get("role") or "") == "guest":
            st.caption("当前为游客身份，注册正式账号后可把学习记录迁移过去。")
        st.markdown("### :material/sync_alt: 微信绑定")
        if auth_user.get("wechat_openid"):
            st.success("已绑定微信账号，可微信快捷登录。", icon=":material/check_circle:")
        else:
            st.caption(
                "当前为账号密码登录。微信 OAuth（扫码登录）需要已部署域名与微信开放平台"
                "凭据，接入后即可在此显示绑定状态。"
            )

if str(auth_user.get("role") or "") != "guest":
    with st.form("password_form", border=True):
        st.markdown("#### 修改密码")
        old_password = st.text_input("原密码", type="password")
        new_password = st.text_input("新密码", type="password")
        confirm_new = st.text_input("确认新密码", type="password")
        changed = st.form_submit_button("修改密码", icon=":material/password:")

    if changed:
        if new_password != confirm_new:
            st.error("两次输入的新密码不一致。", icon=":material/error:")
        else:
            try:
                store.change_password(int(auth_user["id"]), old_password, new_password)
                st.success("密码已修改，下次请使用新密码登录。", icon=":material/check_circle:")
            except AccountError as exc:
                st.error(str(exc), icon=":material/error:")
else:
    st.info(
        "游客身份没有密码；退出登录后注册账号，即可设置密码并继承游客学习记录。",
        icon=":material/info:",
    )

st.space()
if st.button(
    "退出登录",
    icon=":material/logout:",
    type="secondary",
    width="stretch",
):
    _reset_student_state()
    st.session_state["auth_user"] = None
    st.rerun()
