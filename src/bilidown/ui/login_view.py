"""扫码登录视图

渲染四种扫码状态（未扫描／已扫描待确认／已过期／成功），二维码在后台线程生成，
生成期间界面可响应；过期时提示用户点击「刷新二维码」
"""

from __future__ import annotations

import io
import tkinter as tk
from collections.abc import Callable

from PIL import Image, ImageTk

from ..config.settings import STYLE
from ..utils.logger import get_logger
from .base import BaseView
from .widgets import make_button, make_label

_log = get_logger("login_view")

QR_SIZE = 300


class LoginView(BaseView):
    """扫码登录界面（左：二维码，右：状态与操作）"""

    def __init__(
        self,
        parent: tk.Misc,
        on_refresh: Callable[[], None],
        on_logout: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_refresh = on_refresh
        self._on_logout = on_logout
        self._qr_photo: ImageTk.PhotoImage | None = None

        left = tk.Frame(self, bg=str(STYLE["frame_bg"]), width=520)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        left.pack_propagate(False)

        make_label(
            left,
            "扫码登录",
            font_key="title_font",
            anchor="center",
        ).pack(pady=(30, 12))

        #二维码占位（真实图片异步替换）
        self.qr_label = tk.Label(
            left,
            text="二维码生成中…",
            font=STYLE["normal_font"],  #type: ignore[arg-type]
            bg=str(STYLE["frame_bg"]),
            fg=str(STYLE["muted_text"]),
            width=28,
            height=13,
        )
        self.qr_label.pack(pady=(0, 16))

        self.status_label = make_label(
            left,
            "正在生成二维码，请稍候…",
            font_key="normal_font",
            fg=str(STYLE["accent_color"]),
            anchor="center",
            wraplength=380,
            justify="center",
        )
        self.status_label.pack(pady=(0, 8))

        make_label(
            left,
            "打开 bilibili 客户端，在「我的」页面左上角扫描二维码",
            font_key="small_font",
            fg=str(STYLE["muted_text"]),
            anchor="center",
            wraplength=400,
            justify="center",
        ).pack(pady=(0, 12))

        button_row = tk.Frame(left, bg=str(STYLE["frame_bg"]))
        button_row.pack(pady=(0, 20))

        self.refresh_btn = make_button(
            button_row, "刷新二维码", self._handle_refresh, padx=16, pady=6
        )
        self.refresh_btn.pack(side=tk.LEFT, padx=6)

        self.logout_btn = make_button(
            button_row, "退出登录", self._handle_logout, bg="#9e9e9e", padx=16, pady=6
        )
        self.logout_btn.pack(side=tk.LEFT, padx=6)
        self.logout_btn.pack_forget()

        #右侧说明区
        right = tk.Frame(self, bg=str(STYLE["frame_bg"]))
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        make_label(
            right,
            "登录后可以：",
            font_key="title_font",
            anchor="w",
        ).pack(anchor="w", padx=40, pady=(70, 10))
        for line in (
            "• 获取更高清晰度的视频流",
            "• 下载需要登录才能访问的内容",
            "• 查看个人资料与下载记录",
            "",
            "登录凭据会加密保存在本机，",
            "下次启动自动恢复登录状态。",
        ):
            make_label(
                right,
                line,
                font_key="normal_font",
                fg=str(STYLE["muted_text"]),
                anchor="w",
            ).pack(anchor="w", padx=44, pady=2)

    #------------------------------------------------------------- 对外接口
    @property
    def qrcode_text(self) -> str:
        """二维码区域的文字（占位/过期提示），便于断言与自动化测试"""
        try:
            return str(self.qr_label.cget("text"))
        except tk.TclError:  #pragma: no cover - 控件已销毁
            return ""

    def set_status(self, text: str, *, accent: bool = True) -> None:
        if not self.alive:
            return
        self.status_label.config(
            text=text,
            fg=str(STYLE["accent_color"]) if accent else str(STYLE["muted_text"]),
        )

    def set_qrcode(self, png_bytes: bytes) -> None:
        """用PNG字节流更新二维码图片"""
        if not self.alive:
            return
        try:
            image = Image.open(io.BytesIO(png_bytes)).resize((QR_SIZE, QR_SIZE))
            #显式master，避免多Tk root时图片注册到别的解释器上
            self._qr_photo = ImageTk.PhotoImage(image, master=self)
        except Exception:  #noqa: BLE001
            _log.warning("二维码图片渲染失败", exc_info=True)
            self.qr_label.config(text="二维码渲染失败，请点击刷新", image="")
            return
        self.qr_label.config(image=self._qr_photo, text="", width=QR_SIZE, height=QR_SIZE)

    def set_refreshing(self, refreshing: bool) -> None:
        if not self.alive:
            return
        self.refresh_btn.config(state=tk.DISABLED if refreshing else tk.NORMAL)

    def set_logged_in(self, logged_in: bool) -> None:
        if not self.alive:
            return
        if logged_in:
            self.logout_btn.pack(side=tk.LEFT, padx=6)
        else:
            self.logout_btn.pack_forget()

    def show_qrcode_expired(self) -> None:
        if not self.alive:
            return
        self.qr_label.config(image="", text="二维码已过期")
        self._qr_photo = None
        self.set_status("二维码已过期，请点击「刷新二维码」重新获取", accent=False)

    #------------------------------------------------------------- 事件
    def _handle_refresh(self) -> None:
        self.set_refreshing(True)
        self.on_refresh()

    def _handle_logout(self) -> None:
        if self._on_logout is not None:
            self._on_logout()

    def register_refresh(self, callback: Callable[[], None]) -> None:
        """允许控制器替换刷新逻辑"""
        self._on_refresh = callback

    def register_logout(self, callback: Callable[[], None]) -> None:
        self._on_logout = callback
