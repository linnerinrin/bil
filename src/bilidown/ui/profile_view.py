"""个人信息视图，字段统一安全取值，头像由控制器在后台线程下载"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from typing import Any

from PIL import Image, ImageTk

from ..config.settings import STYLE
from .base import BaseView
from .widgets import make_button, make_label

AVATAR_SIZE = 96


class ProfileView(BaseView):
    """展示登录用户资料"""

    def __init__(
        self,
        parent: tk.Misc,
        user_info: dict[str, Any] | None,
        *,
        on_refresh: Callable[[], None] | None = None,
        on_login: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._avatar_photo: ImageTk.PhotoImage | None = None

        if user_info is None:
            self._build_empty(on_login)
            return
        self._build_profile(user_info, on_refresh)

    #------------------------------------------------------------- 未登录
    def _build_empty(self, on_login: Callable[[], None] | None) -> None:
        make_label(
            self, "尚未登录", font_key="title_font", anchor="center"
        ).pack(expand=True, pady=(160, 10))
        make_label(
            self,
            "登录后可查看个人资料并下载更高清晰度内容",
            font_key="normal_font",
            fg=str(STYLE["muted_text"]),
            anchor="center",
        ).pack(pady=(0, 16))
        if on_login is not None:
            make_button(self, "去登录", on_login).pack()

    #--------------------------------------------------------------- 已登录
    def _build_profile(
        self, info: dict[str, Any], on_refresh: Callable[[], None] | None
    ) -> None:
        left = tk.Frame(self, bg=str(STYLE["frame_bg"]))
        left.pack(side=tk.LEFT, padx=40, pady=30, anchor="n")

        self.avatar_label = tk.Label(
            left,
            text="头像",
            font=STYLE["small_font"],  #type: ignore[arg-type]
            bg=str(STYLE["frame_bg"]),
            fg=str(STYLE["muted_text"]),
            width=12,
            height=6,
            relief=tk.RAISED,
            bd=1,
        )
        self.avatar_label.pack()

        right = tk.Frame(self, bg=str(STYLE["frame_bg"]))
        right.pack(side=tk.LEFT, padx=20, pady=30, fill=tk.BOTH, expand=True)

        nickname = _get(info, "name", "未设置昵称")
        uid = _get(info, "mid", "-")
        level = _get(info, "level", "-")
        make_label(
            right,
            f"{nickname}｜UID: {uid}｜等级: {level}",
            font_key="title_font",
            fg="#2E86AB",
        ).pack(anchor="w", pady=(6, 8))

        rows = (
            ("性别", _get(info, "sex", "未知")),
            ("硬币", _get(info, "coins", 0)),
            ("关注", _get(info, "following", 0)),
            ("粉丝", _get(info, "follower", _get(info, "fans", 0))),
        )
        for label, value in rows:
            make_label(
                right,
                f"{label}：{value}",
                font_key="normal_font",
                fg=str(STYLE["muted_text"]),
            ).pack(anchor="w", pady=2)

        make_label(
            right,
            f"个性签名：{_get(info, 'sign', '暂无个性签名')}",
            font_key="normal_font",
            fg=str(STYLE["muted_text"]),
            wraplength=560,
        ).pack(anchor="w", pady=(8, 0))

        if on_refresh is not None:
            make_button(right, "刷新资料", on_refresh, padx=14, pady=6).pack(
                anchor="w", pady=(16, 0)
            )

    #------------------------------------------------------------- 头像
    def set_avatar(self, image: Image.Image | None) -> None:
        """设置头像图片（由控制器在后台线程下载后调用）"""
        if not self.alive:
            return
        if image is None:
            self.avatar_label.config(text="头像加载失败")
            return
        try:
            #显式master，避免多Tk root时图片注册到别的解释器上
            self._avatar_photo = ImageTk.PhotoImage(
                image.resize((AVATAR_SIZE, AVATAR_SIZE)), master=self
            )
        except Exception:  #noqa: BLE001
            return
        self.avatar_label.config(image=self._avatar_photo, text="", width=AVATAR_SIZE, height=AVATAR_SIZE)


def _get(info: dict[str, Any], key: str, default: object) -> object:
    """安全取值：None／空字符串都退化为默认值"""
    value = info.get(key)
    if value is None or value == "":
        return default
    return value
