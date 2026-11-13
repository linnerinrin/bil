"""主窗口外壳：背景、顶栏、导航栏、内容容器"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from typing import TYPE_CHECKING

from PIL import Image, ImageTk

from ..config.paths import (
    is_builtin_ref,
    load_background_image,
    load_builtin_background,
    load_image_fitted,
    parse_builtin_ref,
    resource_path,
)
from ..config.settings import (
    STYLE,
    WINDOW_HEIGHT,
    WINDOW_TITLE,
    WINDOW_WIDTH,
)
from ..utils.logger import get_logger
from .widgets import apply_acrylic_style, make_button

if TYPE_CHECKING:
    from PIL.ImageTk import PhotoImage

_log = get_logger("window")

#窗口图标（随resources/一起打包）
ICON_NAME = "Amedown.png"


def center_window(window: tk.Misc, width: int, height: int) -> None:
    screen_w = window.winfo_screenwidth()
    screen_h = window.winfo_screenheight()
    x = max(0, (screen_w - width) // 2)
    y = max(0, (screen_h - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


def load_background(size: tuple[int, int] = (WINDOW_WIDTH, WINDOW_HEIGHT)) -> Image.Image | None:
    """加载内置背景图

    自定义背景请用:func:`bilidown.config.paths.load_background_image`
    """
    result = load_background_image(size)
    return result if isinstance(result, Image.Image) else None


class MainWindow:
    """主窗口与固定区域"""

    def __init__(self, title: str = WINDOW_TITLE) -> None:
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        center_window(self.root, WINDOW_WIDTH, WINDOW_HEIGHT)
        self.root.resizable(False, False)
        self.root.configure(bg=str(STYLE["main_bg"]))

        self._apply_ttk_theme()
        self._maximized = False
        self._normal_geometry = ""
        self._drag_offset: tuple[int, int] | None = None
        self._close_callback: Callable[[], None] | None = None

        self._bg_photo: PhotoImage | None = None
        self._icon_photo: object | None = None
        self._bg_label: tk.Label | None = None
        self._bg_fallback: tk.Frame | None = None
        self._background_path: str = ""
        self._used_custom_background: bool = False
        self._set_window_icon()
        self._build_background()
        self._build_topbar()
        self._build_navbar()
        self._build_content()
        self._build_statusbar()

    def _apply_ttk_theme(self) -> None:
        """给ttk控件套一层现代主题（sv-ttk可选，未安装时保持默认）

        只影响ttk族控件，tk.Button为自绘配色不受影响
        """
        try:
            import sv_ttk  #type: ignore[import-not-found]

            sv_ttk.set_theme("light")
            _log.info("已启用 sv-ttk 主题")
        except Exception:  #noqa: BLE001 - 主题只是锦上添花，缺了也要能启动
            _log.debug("sv-ttk 不可用，沿用默认 ttk 主题", exc_info=True)

    def _set_window_icon(self) -> None:
        """给窗口设置图标（任务栏/Alt-Tab共用）

        PNG格式，失败只记日志不影响功能
        """
        try:
            icon_path = resource_path(ICON_NAME)
            if not icon_path.is_file():
                return
            self._icon_photo = tk.PhotoImage(file=str(icon_path), master=self.root)
            self.root.iconphoto(True, self._icon_photo)
        except Exception:  #noqa: BLE001 - 图标失败不影响启动
            _log.debug("设置窗口图标失败", exc_info=True)

    #--------------------------------------------------------- 自绘标题栏
    def _build_titlebar(self) -> None:
        """隐藏系统标题栏后自绘一条（可拖动+最小化/最大化/关闭）"""
        bar = tk.Frame(self.root, bg="#ffffff", height=34)
        bar.pack(fill=tk.X, side=tk.TOP)
        bar.pack_propagate(False)
        self.titlebar = bar

        self.title_label = tk.Label(
            bar,
            text=WINDOW_TITLE,
            bg="#ffffff",
            fg=str(STYLE["text_color"]),
            font=("Microsoft YaHei", 10, "bold"),
        )
        self.title_label.pack(side=tk.LEFT, padx=(14, 0))

        self._title_button(bar, "✕", self._close, hover="#e81123", hover_fg="#ffffff")
        self._title_button(bar, "▢", self._toggle_max)
        self._title_button(bar, "—", self._minimize)

        for widget in (bar, self.title_label):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_drag)
            widget.bind("<Double-Button-1>", lambda _e: self._toggle_max())

        self.root.bind("<Map>", self._restore_frameless)

    def _title_button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        hover: str = "#f0f0f0",
        hover_fg: str = "#222222",
    ) -> tk.Button:
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bd=0,
            relief=tk.FLAT,
            bg="#ffffff",
            fg="#555555",
            activebackground=hover,
            activeforeground=hover_fg,
            font=("Segoe UI", 11),
            width=4,
            cursor="hand2",
        )
        btn.pack(side=tk.RIGHT, fill=tk.Y)
        return btn

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_offset = (
            event.x_root - self.root.winfo_x(),
            event.y_root - self.root.winfo_y(),
        )

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag_offset is None or self._maximized:
            return
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        try:
            self.root.geometry(f"+{x}+{y}")
        except tk.TclError:  #pragma: no cover
            pass

    def _toggle_max(self) -> None:
        try:
            if self._maximized:
                if self._normal_geometry:
                    self.root.geometry(self._normal_geometry)
            else:
                self._normal_geometry = self.root.geometry()
                width = self.root.winfo_screenwidth()
                height = self.root.winfo_screenheight()
                self.root.geometry(f"{width}x{max(400, height - 48)}+0+0")
            self._maximized = not self._maximized
        except tk.TclError:  #pragma: no cover
            pass

    def _minimize(self) -> None:
        try:
            #Windows上先恢复系统装饰再最小化，回到前台时切回无边框
            self.root.overrideredirect(False)
            self.root.iconify()
        except tk.TclError:  #pragma: no cover
            pass

    def _restore_frameless(self, _event: object = None) -> None:
        try:
            if self.root.state() == "normal" and not self.root.overrideredirect():
                self.root.overrideredirect(True)
        except tk.TclError:  #pragma: no cover
            pass

    def _close(self) -> None:
        if self._close_callback is not None:
            self._close_callback()
        else:  #pragma: no cover - 兜底
            self.root.destroy()

    def on_close(self, callback: Callable[[], None]) -> None:
        """注册关闭按钮回调（交给控制器做任务收尾）"""
        self._close_callback = callback

    #------------------------------------------------------------- 背景
    def _build_background(self) -> None:
        """构建背景，任一步失败退化为纯色，不阻断窗口创建"""
        self._apply_background_image(self._background_path)
        if self._bg_label is None and self._bg_fallback is None:
            self._show_solid_background()
            return
        #背景必须垫在最底层
        for widget in (self._bg_label, self._bg_fallback):
            if widget is not None:
                widget.lower()

    def _apply_background_image(self, custom_path: str) -> None:
        """尝试用图片作为背景，失败则回退内置图或纯色"""
        self._used_custom_background = False

        #分开加载以区分用户自定义图与内置回退图
        background = None
        builtin_name = ""
        if custom_path and not is_builtin_ref(custom_path):
            background = load_image_fitted(custom_path, (WINDOW_WIDTH, WINDOW_HEIGHT))
            if background is None:
                _log.warning("自定义背景不可用，回退内置背景: %s", custom_path)
            used_custom = background is not None
        else:
            #空值或builtin:xxx时使用内置背景
            builtin_name = parse_builtin_ref(custom_path)
            used_custom = False

        if background is None:
            background = load_builtin_background(
                (WINDOW_WIDTH, WINDOW_HEIGHT), builtin_name
            )

        if background is None:
            self._show_solid_background()
            return
        try:
            #显式指定master，避免多个Tk解释器时图片注册到别的解释器上
            photo = ImageTk.PhotoImage(background, master=self.root)  #type: ignore[arg-type]
        except Exception:  #noqa: BLE001 - 无显示环境/图片被回收
            _log.warning("背景图渲染失败，使用纯色背景", exc_info=True)
            self._show_solid_background()
            return

        #先隐藏纯色兜底，再更新（或新建）图片标签，避免出现空档
        if self._bg_fallback is not None:
            self._bg_fallback.place_forget()
        self._bg_photo = photo
        if self._bg_label is None:
            self._bg_label = tk.Label(self.root, image=photo)
            self._bg_label.place(x=0, y=0, relwidth=1, relheight=1)
        else:
            self._bg_label.config(image=photo)
        self._bg_label.lower()
        self._used_custom_background = used_custom

    def _show_solid_background(self) -> None:
        self._bg_photo = None
        if self._bg_fallback is None:
            self._bg_fallback = tk.Frame(self.root, bg=str(STYLE["main_bg"]))
            self._bg_fallback.place(x=0, y=0, relwidth=1, relheight=1)
        self._bg_fallback.lower()

    def set_background(self, custom_path: str) -> bool:
        """切换背景

        支持三类取值（与偏好里的background_path一致）：
        - ""：默认内置背景
        - "builtin:文件名"：指定的内置背景
        - 其它：自定义图片的绝对路径

        返回请求是否被满足：自定义图片不可用时返回False并回退内置背景
        无论返回值如何，窗口始终有可用背景
        """
        self._background_path = custom_path or ""
        self._apply_background_image(self._background_path)
        if not self._background_path or is_builtin_ref(self._background_path):
            #内置背景：只要有图或有纯色兜底即视为成功
            return self._bg_photo is not None or self._bg_fallback is not None
        return self._used_custom_background

    #------------------------------------------------------------- 顶栏
    def _build_topbar(self) -> None:
        self.top_frame = tk.Frame(self.root, bg=str(STYLE["frame_bg"]), height=80)
        self.top_frame.pack(fill=tk.X, padx=20, pady=(20, 10))
        self.top_frame.pack_propagate(False)
        apply_acrylic_style(self.top_frame, float(STYLE["bg_image_opacity"]))  #type: ignore[arg-type]

        self.userinfo_label = tk.Label(
            self.top_frame,
            text="未登录",
            font=STYLE["title_font"],  #type: ignore[arg-type]
            fg=str(STYLE["accent_color"]),
            bg=str(STYLE["frame_bg"]),
            anchor="w",
        )
        self.userinfo_label.pack(side=tk.LEFT, padx=20, pady=20)

        self.logout_btn = make_button(
            self.top_frame, "退出登录", self._on_logout_click, padx=14, pady=6
        )
        self.logout_btn.pack(side=tk.RIGHT, padx=20, pady=20)
        self.logout_btn.pack_forget()  #未登录时隐藏

        self._on_logout: Callable[[], None] | None = None

    def on_logout(self, callback: Callable[[], None]) -> None:
        self._on_logout = callback

    def _on_logout_click(self) -> None:
        if self._on_logout is not None:
            self._on_logout()

    def set_user_text(self, text: str) -> None:
        """更新顶栏用户信息"""
        self.userinfo_label.config(text=text)

    def set_logged_in(self, logged_in: bool) -> None:
        if logged_in:
            self.logout_btn.pack(side=tk.RIGHT, padx=20, pady=20)
        else:
            self.logout_btn.pack_forget()

    #------------------------------------------------------------- 导航栏
    def _build_navbar(self) -> None:
        self.nav_frame = tk.Frame(self.root, bg=str(STYLE["frame_bg"]), height=90)
        self.nav_frame.pack(fill=tk.X, padx=20, pady=(0, 10))
        self.nav_frame.pack_propagate(False)
        apply_acrylic_style(self.nav_frame, float(STYLE["bg_image_opacity"]))  #type: ignore[arg-type]

        self._nav_buttons: dict[str, tk.Button] = {}
        #导航按钮顺序
        specs = (
            ("login", "登录"),
            ("search", "搜索"),
            ("profile", "我的"),
            ("settings", "设置"),
            ("downloads", "下载列表"),
        )
        for index, (key, text) in enumerate(specs):
            btn = make_button(self.nav_frame, text, lambda k=key: self._navigate(k))
            btn.grid(row=0, column=index, padx=20, pady=24, sticky="ew")
            self.nav_frame.grid_columnconfigure(index, weight=1)
            self._nav_buttons[key] = btn

        self._on_navigate: Callable[[str], None] | None = None

    def on_navigate(self, callback: Callable[[str], None]) -> None:
        self._on_navigate = callback

    def _navigate(self, key: str) -> None:
        if self._on_navigate is not None:
            self._on_navigate(key)

    def set_nav_enabled(self, key: str, enabled: bool) -> None:
        btn = self._nav_buttons.get(key)
        if btn is not None:
            btn.config(state=tk.NORMAL if enabled else tk.DISABLED)

    #------------------------------------------------------------- 内容区
    def _build_content(self) -> None:
        self.content_frame = tk.Frame(self.root, bg=str(STYLE["frame_bg"]))
        self.content_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 10))
        apply_acrylic_style(self.content_frame, float(STYLE["bg_image_opacity"]))  #type: ignore[arg-type]

    def clear_content(self) -> None:
        """清空内容容器，先调用子控件清理回调再销毁"""
        for child in self.content_frame.winfo_children():
            hook = getattr(child, "_on_destroy_hook", None)
            if callable(hook):
                try:
                    hook()
                except Exception:  #noqa: BLE001
                    _log.debug("视图清理回调失败", exc_info=True)
            child.destroy()

    #------------------------------------------------------------- 状态栏
    def _build_statusbar(self) -> None:
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(
            self.root,
            textvariable=self.status_var,
            font=STYLE["small_font"],  #type: ignore[arg-type]
            fg=str(STYLE["muted_text"]),
            bg=str(STYLE["frame_bg"]),
            anchor="w",
            padx=12,
            pady=4,
        ).pack(fill=tk.X, side=tk.BOTTOM)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    #------------------------------------------------------------- 生命周期
    def after(self, ms: int, callback: Callable[[], None]) -> str:
        return self.root.after(ms, callback)

    def cancel_after(self, identifier: str | None) -> None:
        if not identifier:
            return
        try:
            self.root.after_cancel(identifier)
        except Exception:  #noqa: BLE001 - 已触发或窗口已销毁
            pass

    def mainloop(self) -> None:
        self.root.mainloop()

    def destroy(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass

