"""设置视图：背景、下载目录、并发数、完成后行为、重试次数。

设计约定
--------
- 本视图**只负责收集用户输入**，不直接落盘、不直接改运行中的组件。
  点「应用」时通过 ``on_apply(draft)`` 把一份 ``Preferences`` 草稿交给控制器，
  由控制器统一校验、保存并让设置生效（见 ``app.apply_preferences``）。
- 这样「改到一半」的状态不会被写入配置，出问题时也只有一个生效入口。
- 视图实现销毁钩子，避免已销毁后仍有回调（沿用项目既有约定）。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import filedialog, messagebox, ttk

from ..config.paths import (
    BACKGROUND_SUFFIXES,
    default_builtin_background,
    default_download_dir,
    is_builtin_ref,
    is_supported_background,
    list_builtin_backgrounds,
    make_builtin_ref,
    parse_builtin_ref,
    resource_path,
)
from ..config.settings import STYLE
from ..core.preferences import (
    BACKGROUND_FILETYPES,
    CONCURRENT_RANGE,
    RETRIES_RANGE,
    Preferences,
    background_hint_dir,
)
from ..utils.logger import get_logger
from .base import BaseView
from .widgets import make_button, make_label

_log = get_logger("settings_view")

THUMB_SIZE = (220, 124)


class SettingsView(BaseView):
    """设置页面。"""

    def __init__(
        self,
        parent: tk.Misc,
        prefs: Preferences,
        *,
        on_apply: Callable[[Preferences], None],
        on_restore_defaults: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self._prefs = prefs
        self._on_apply = on_apply
        self._on_restore_defaults = on_restore_defaults

        # 草稿：界面上的当前值，只有点「应用」才会真正生效
        self._background = prefs.background_path
        self._download_dir = prefs.download_dir
        self._thumb_photo: object | None = None

        # 内置背景清单（放进 resources/ 的图片会自动出现在这里）
        self._builtin_names = list_builtin_backgrounds()
        default_name = default_builtin_background()
        self._builtin_default = default_name

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._build_header()
        self._build_body()
        self._build_footer()
        self._refresh_background_preview()

    # ------------------------------------------------------------- 顶部
    def _build_header(self) -> None:
        header = tk.Frame(self, bg=str(STYLE["frame_bg"]))
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(14, 6))
        make_label(
            header, "设置", font_key="title_font", bg=str(STYLE["frame_bg"])
        ).pack(side=tk.LEFT)
        make_label(
            header,
            "修改后请点击「应用」，设置会保存并在下次启动时自动恢复",
            font_key="small_font",
            fg=str(STYLE["muted_text"]),
            bg=str(STYLE["frame_bg"]),
        ).pack(side=tk.LEFT, padx=(12, 0))

    # ------------------------------------------------------------- 主体
    def _build_body(self) -> None:
        # 内容较多，放进可滚动区域，避免小窗口下被截断
        wrapper = tk.Frame(self, bg=str(STYLE["frame_bg"]))
        wrapper.grid(row=1, column=0, sticky="nsew", padx=24)

        canvas = tk.Canvas(wrapper, highlightthickness=0, bg=str(STYLE["frame_bg"]))
        scrollbar = ttk.Scrollbar(wrapper, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=str(STYLE["frame_bg"]))
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        inner.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind(
            "<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width)
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._scroll_canvas = canvas
        self._scroll_inner = inner

        self._build_background_section(inner)
        self._build_directory_section(inner)
        self._build_runtime_section(inner)

        # 滚轮绑定必须在**内容创建完成之后**：bind_mousewheel 递归绑定的是
        # 「当前已存在的后代」，如果提前绑定，后面创建的卡片就收不到事件
        # （这正是设置页滚不动的根因）。Tk 事件不冒泡，所以卡片与卡片内的
        # 标签/按钮/输入框都必须各自绑定。
        from .widgets import bind_mousewheel_for_canvas, unbind_mousewheel

        bind_mousewheel_for_canvas(wrapper, canvas, include=inner)
        self.add_destroy_hook(lambda: unbind_mousewheel(wrapper))

    # ---------------------------------------------------- ① 设置背景
    def _build_background_section(self, parent: tk.Misc) -> None:
        card = self._card(
            parent,
            "设置背景",
            "选内置背景或自选图片；图片会等比铺满窗口并裁掉多余边缘",
        )

        row = tk.Frame(card, bg=str(STYLE["frame_bg"]))
        row.pack(fill=tk.X, padx=14, pady=(4, 10))

        # 预览：**固定尺寸**，切换背景时不再改变自身大小
        # （上一版在显示缩略图后残留了 width/height，恢复内置背景时被撑得很大）
        self.preview_label = tk.Label(
            row,
            text="",
            font=STYLE["small_font"],  # type: ignore[arg-type]
            bg=str(STYLE["frame_bg"]),
            fg=str(STYLE["muted_text"]),
            width=THUMB_SIZE[0],
            height=THUMB_SIZE[1],
            relief=tk.GROOVE,
            bd=1,
        )
        self.preview_label.pack(side=tk.LEFT, anchor="n")

        right = tk.Frame(row, bg=str(STYLE["frame_bg"]))
        right.pack(side=tk.LEFT, padx=16, fill=tk.BOTH, expand=True)

        # ---- 内置背景：列表 + 文件名 + 各自的缩略图预览
        make_label(
            right,
            "内置背景",
            font_key="normal_font",
            bg=str(STYLE["frame_bg"]),
        ).pack(anchor="w")

        builtin_box = tk.Frame(right, bg=str(STYLE["frame_bg"]))
        builtin_box.pack(anchor="w", pady=(4, 8))

        # 宽度按最长文件名自适应（用字符数单位），避免只有一项时被拉满整行
        longest = max((len(name) for name in self._builtin_names), default=12)
        list_width = max(18, min(46, longest + 6))

        self.builtin_list = tk.Listbox(
            builtin_box,
            height=min(4, max(1, len(self._builtin_names))),
            width=list_width,
            font=STYLE["small_font"],  # type: ignore[arg-type]
            bg="white",
            fg=str(STYLE["text_color"]),
            activestyle="none",
            relief=tk.FLAT,
            highlightthickness=1,
            highlightcolor="#00a1d6",
            exportselection=False,
        )
        self.builtin_list.pack(side=tk.LEFT)
        for name in self._builtin_names:
            self.builtin_list.insert(tk.END, name)
        self.builtin_list.bind("<<ListboxSelect>>", self._on_builtin_selected)
        self._select_builtin_in_list()

        make_button(
            builtin_box, "使用选中", self._use_selected_builtin, bg="#4CAF50",
            font_key="small_font", padx=10, pady=4,
        ).pack(side=tk.LEFT, padx=(8, 0), anchor="n")

        if not self._builtin_names:
            make_label(
                right,
                "（资源目录里没有内置背景图）",
                font_key="small_font",
                fg=str(STYLE["muted_text"]),
                bg=str(STYLE["frame_bg"]),
            ).pack(anchor="w")

        # ---- 自定义背景
        make_label(
            right,
            "自定义背景",
            font_key="normal_font",
            bg=str(STYLE["frame_bg"]),
        ).pack(anchor="w", pady=(6, 0))

        self.bg_path_label = make_label(
            right,
            "",
            font_key="small_font",
            fg=str(STYLE["muted_text"]),
            bg=str(STYLE["frame_bg"]),
            wraplength=560,
        )
        self.bg_path_label.pack(anchor="w", pady=(2, 6))

        buttons = tk.Frame(right, bg=str(STYLE["frame_bg"]))
        buttons.pack(anchor="w")
        make_button(
            buttons, "选择图片…", self._choose_background, padx=14, pady=6
        ).pack(side=tk.LEFT, padx=(0, 8))
        make_button(
            buttons,
            "清除自定义",
            self._clear_background,
            bg="#9e9e9e",
            padx=14,
            pady=6,
        ).pack(side=tk.LEFT)

        make_label(
            right,
            "支持格式：" + "、".join(sorted(BACKGROUND_SUFFIXES)),
            font_key="small_font",
            fg=str(STYLE["muted_text"]),
            bg=str(STYLE["frame_bg"]),
        ).pack(anchor="w", pady=(8, 0))

    # ----------------------------------------------------- 内置背景操作
    def _select_builtin_in_list(self) -> None:
        """让列表选中当前生效的内置背景（没有则选第一项）。"""
        if not self._builtin_names:
            return
        current = parse_builtin_ref(self._background) or self._builtin_default
        if self._background and not is_builtin_ref(self._background):
            # 当前用的是自定义背景，列表不选中任何项
            try:
                self.builtin_list.selection_clear(0, tk.END)
            except tk.TclError:  # pragma: no cover
                pass
            return
        if current in self._builtin_names:
            index = self._builtin_names.index(current)
        else:
            index = 0
        try:
            self.builtin_list.selection_clear(0, tk.END)
            self.builtin_list.selection_set(index)
            self.builtin_list.see(index)
        except tk.TclError:  # pragma: no cover
            pass

    def _selected_builtin(self) -> str:
        try:
            selection = self.builtin_list.curselection()
        except tk.TclError:  # pragma: no cover
            return ""
        if not selection:
            return ""
        index = int(selection[0])
        if 0 <= index < len(self._builtin_names):
            return self._builtin_names[index]
        return ""

    def _on_builtin_selected(self, _event: object = None) -> None:
        """在列表里点选内置背景：立即刷新缩略图，点「应用」才真正生效。"""
        name = self._selected_builtin()
        if not name:
            return
        self._background = make_builtin_ref(name)
        self._refresh_background_preview()
        self._set_hint(f"已选择内置背景「{name}」，点击「应用」生效")

    def _use_selected_builtin(self) -> None:
        name = self._selected_builtin()
        if not name:
            self._set_hint("请先在上面的列表里选择一个内置背景")
            return
        self._on_builtin_selected()

    # ---------------------------------------------------- ② 设置下载目录
    def _build_directory_section(self, parent: tk.Misc) -> None:
        card = self._card(
            parent, "设置下载目录", "已开始下载的任务会继续写入原目录，新任务使用新目录"
        )

        row = tk.Frame(card, bg=str(STYLE["frame_bg"]))
        row.pack(fill=tk.X, padx=14, pady=(4, 10))

        self.dir_var = tk.StringVar(value=self._download_dir or str(default_download_dir()))
        entry = tk.Entry(
            row,
            textvariable=self.dir_var,
            font=STYLE["normal_font"],  # type: ignore[arg-type]
            bg="white",
            fg=str(STYLE["text_color"]),
            relief=tk.FLAT,
            highlightthickness=1,
            highlightcolor="#00a1d6",
        )
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=(0, 8))

        make_button(row, "浏览…", self._choose_directory, padx=12, pady=6).pack(side=tk.LEFT)
        make_button(
            row, "打开", self._open_directory, bg="#4CAF50", padx=12, pady=6
        ).pack(side=tk.LEFT, padx=8)
        make_button(
            row,
            "恢复默认",
            self._reset_directory,
            bg="#9e9e9e",
            padx=12,
            pady=6,
        ).pack(side=tk.LEFT)

        self.dir_hint = make_label(
            card,
            f"默认目录：{default_download_dir()}",
            font_key="small_font",
            fg=str(STYLE["muted_text"]),
            bg=str(STYLE["frame_bg"]),
        )
        self.dir_hint.pack(anchor="w", padx=14, pady=(0, 10))

    # ---------------------------------------------------- ③④⑤ 运行设置
    def _build_runtime_section(self, parent: tk.Misc) -> None:
        card = self._card(parent, "下载行为", "影响之后新建的下载任务")

        # 并发数
        row1 = tk.Frame(card, bg=str(STYLE["frame_bg"]))
        row1.pack(fill=tk.X, padx=14, pady=(4, 8))
        make_label(
            row1,
            "最大并发下载数",
            font_key="normal_font",
            bg=str(STYLE["frame_bg"]),
        ).pack(side=tk.LEFT)
        self.concurrent_var = tk.IntVar(value=self._prefs.max_concurrent)
        ttk.Spinbox(
            row1,
            from_=CONCURRENT_RANGE[0],
            to=CONCURRENT_RANGE[1],
            textvariable=self.concurrent_var,
            width=5,
        ).pack(side=tk.LEFT, padx=12)
        make_label(
            row1,
            f"（{CONCURRENT_RANGE[0]}–{CONCURRENT_RANGE[1]}，越大越快但更占带宽）",
            font_key="small_font",
            fg=str(STYLE["muted_text"]),
            bg=str(STYLE["frame_bg"]),
        ).pack(side=tk.LEFT)

        # 重试次数
        row2 = tk.Frame(card, bg=str(STYLE["frame_bg"]))
        row2.pack(fill=tk.X, padx=14, pady=(0, 8))
        make_label(
            row2,
            "下载失败重试次数",
            font_key="normal_font",
            bg=str(STYLE["frame_bg"]),
        ).pack(side=tk.LEFT)
        self.retries_var = tk.IntVar(value=self._prefs.download_retries)
        ttk.Spinbox(
            row2,
            from_=RETRIES_RANGE[0],
            to=RETRIES_RANGE[1],
            textvariable=self.retries_var,
            width=5,
        ).pack(side=tk.LEFT, padx=12)
        make_label(
            row2,
            f"（{RETRIES_RANGE[0]}–{RETRIES_RANGE[1]}，网络不稳时可调高）",
            font_key="small_font",
            fg=str(STYLE["muted_text"]),
            bg=str(STYLE["frame_bg"]),
        ).pack(side=tk.LEFT)

        # 完成后打开文件夹
        row3 = tk.Frame(card, bg=str(STYLE["frame_bg"]))
        row3.pack(fill=tk.X, padx=14, pady=(0, 12))
        self.open_folder_var = tk.BooleanVar(value=self._prefs.open_folder_when_done)
        tk.Checkbutton(
            row3,
            text="下载完成后自动打开所在文件夹",
            variable=self.open_folder_var,
            font=STYLE["normal_font"],  # type: ignore[arg-type]
            bg=str(STYLE["frame_bg"]),
            fg=str(STYLE["text_color"]),
            activebackground=str(STYLE["frame_bg"]),
            selectcolor=str(STYLE["frame_bg"]),
            anchor="w",
        ).pack(side=tk.LEFT)

    # ------------------------------------------------------------- 底部
    def _build_footer(self) -> None:
        footer = tk.Frame(self, bg=str(STYLE["frame_bg"]))
        footer.grid(row=2, column=0, sticky="ew", padx=24, pady=(8, 14))

        self.hint_label = make_label(
            footer,
            "",
            font_key="small_font",
            fg=str(STYLE["accent_color"]),
            bg=str(STYLE["frame_bg"]),
        )
        self.hint_label.pack(side=tk.LEFT)

        make_button(
            footer,
            "恢复默认",
            self._handle_restore,
            bg="#9e9e9e",
            padx=16,
            pady=7,
        ).pack(side=tk.RIGHT)
        make_button(footer, "应用", self._handle_apply, padx=22, pady=7).pack(
            side=tk.RIGHT, padx=10
        )

    # ------------------------------------------------------------- 工具
    def _card(self, parent: tk.Misc, title: str, subtitle: str) -> tk.Frame:
        """创建一张设置卡片。

        滚轮绑定**不在这里做**：统一由 _build_body 在全部内容创建完成后
        递归绑定一次，否则会重复绑定导致滚动速度不一致。
        """
        card = tk.Frame(parent, bg=str(STYLE["frame_bg"]), bd=1, relief=tk.GROOVE)
        card.pack(fill=tk.X, pady=(0, 12))
        head = tk.Frame(card, bg=str(STYLE["frame_bg"]))
        head.pack(fill=tk.X, padx=14, pady=(10, 2))
        make_label(
            head, title, font_key="title_font", bg=str(STYLE["frame_bg"])
        ).pack(side=tk.LEFT)
        make_label(
            head,
            subtitle,
            font_key="small_font",
            fg=str(STYLE["muted_text"]),
            bg=str(STYLE["frame_bg"]),
        ).pack(side=tk.LEFT, padx=(10, 0))
        return card

    def _set_hint(self, text: str, *, accent: bool = True) -> None:
        if not self.alive:
            return
        self.hint_label.config(
            text=text,
            fg=str(STYLE["accent_color"]) if accent else str(STYLE["muted_text"]),
        )

    # ------------------------------------------------------------- 背景
    def _choose_background(self) -> None:
        path = filedialog.askopenfilename(
            title="选择背景图片",
            initialdir=background_hint_dir(),
            filetypes=list(BACKGROUND_FILETYPES),
        )
        if not path:
            return
        if not is_supported_background(path):
            self._set_hint("该文件格式不受支持，请选择 jpg/png/bmp/webp 图片")
            return
        # 先试解码，坏文件在应用前就拦住
        try:
            from PIL import Image

            with Image.open(path) as opened:
                opened.verify()
        except Exception as exc:  # noqa: BLE001
            _log.warning("背景图片无法解析: %s", path, exc_info=True)
            messagebox.showerror("无法使用该图片", f"图片打不开或已损坏：\n{exc}")
            return

        self._background = path
        self._select_builtin_in_list()  # 自定义背景时列表不选中任何项
        self._refresh_background_preview()
        self._set_hint("已选择新背景，点击「应用」生效")

    def _clear_background(self) -> None:
        """清除自定义背景，回到默认内置背景。"""
        self._background = (
            make_builtin_ref(self._builtin_default) if self._builtin_default else ""
        )
        self._select_builtin_in_list()
        self._refresh_background_preview()
        self._set_hint("已切回内置背景，点击「应用」生效")

    def _refresh_background_preview(self) -> None:
        """刷新缩略图。

        预览标签的 width/height 是**创建时固定**的（字符数单位），这里绝不再改，
        否则显示过图片之后再切回内置背景，标签会被撑得很大（真实 bug）。
        """
        if not self.alive:
            return

        source = self._background
        description = ""

        if not source:
            source = make_builtin_ref(self._builtin_default) if self._builtin_default else ""
        if is_builtin_ref(source):
            name = parse_builtin_ref(source)
            description = f"当前：内置背景「{name}」"
            image_path = str(resource_path(name))
        else:
            description = f"当前：自定义背景\n{source}"
            image_path = source

        if not image_path:
            self._thumb_photo = None
            self.preview_label.config(image="", text="无可用背景")
            self.bg_path_label.config(text="未找到任何背景图资源")
            return

        try:
            from PIL import Image, ImageOps, ImageTk

            with Image.open(image_path) as opened:
                image = opened.convert("RGB")
            thumb = ImageOps.fit(image, THUMB_SIZE, method=Image.LANCZOS)
            # 显式 master：避免多 Tk root 时图片注册到别的解释器上
            self._thumb_photo = ImageTk.PhotoImage(thumb, master=self)
        except Exception:  # noqa: BLE001 - 预览失败不影响已保存的选择
            _log.warning("背景缩略图生成失败: %s", image_path, exc_info=True)
            self._thumb_photo = None
            self.preview_label.config(image="", text="预览失败")
            self.bg_path_label.config(text=description)
            return

        # 只改 image 与 text，不动 width/height：保持预览区尺寸恒定
        self.preview_label.config(image=self._thumb_photo, text="")
        self.bg_path_label.config(text=description)

    # ------------------------------------------------------------- 目录
    def _choose_directory(self) -> None:
        initial = self.dir_var.get().strip() or str(default_download_dir())
        path = filedialog.askdirectory(title="选择下载目录", initialdir=initial)
        if path:
            self.dir_var.set(path)
            self._set_hint("已选择新目录，点击「应用」生效")

    def _reset_directory(self) -> None:
        self.dir_var.set(str(default_download_dir()))
        self._set_hint("已恢复默认目录，点击「应用」生效")

    def _open_directory(self) -> None:
        from .download_view import DownloadView

        target = self.dir_var.get().strip()
        if not target:
            self._set_hint("目录为空")
            return
        try:
            from ..config.paths import ensure_dir

            resolved = ensure_dir(target)
            if resolved is None:
                messagebox.showwarning("无法打开", f"目录不存在且无法创建：\n{target}")
                return
            DownloadView.open_path(str(resolved))
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("无法打开", f"打开目录失败：\n{exc}")

    # ------------------------------------------------------------- 提交
    def collect(self) -> Preferences:
        """把界面上的值汇总成一份偏好草稿（已做范围校验）。"""
        dir_text = self.dir_var.get().strip()
        # 用户填的就是默认目录时，存空串表示「跟随默认」，便于将来默认路径变化
        if dir_text and dir_text == str(default_download_dir()):
            dir_text = ""
        return self._prefs.copy_with(
            background_path=self._background,
            download_dir=dir_text,
            max_concurrent=self.concurrent_var.get(),
            download_retries=self.retries_var.get(),
            open_folder_when_done=self.open_folder_var.get(),
        )

    def _handle_apply(self) -> None:
        self._set_hint("正在应用…", accent=False)
        self._on_apply(self.collect())

    def _handle_restore(self) -> None:
        if not messagebox.askyesno(
            "恢复默认设置", "将把背景、下载目录与下载行为恢复为默认值，确定吗？"
        ):
            return
        self._on_restore_defaults()

    # ------------------------------------------------------------- 外部刷新
    def show_saved(self, prefs: Preferences, message: str) -> None:
        """控制器保存成功后回填界面。"""
        if not self.alive:
            return
        self._prefs = prefs
        self._background = prefs.background_path
        self.dir_var.set(prefs.download_dir or str(default_download_dir()))
        self.concurrent_var.set(prefs.max_concurrent)
        self.retries_var.set(prefs.download_retries)
        self.open_folder_var.set(prefs.open_folder_when_done)
        self._select_builtin_in_list()
        self._refresh_background_preview()
        self._set_hint(message)
