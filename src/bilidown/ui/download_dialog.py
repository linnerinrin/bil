"""下载设置弹窗：选分P（可多选）、选格式、选清晰度

样式沿用主界面（白底卡片 + 蓝色扁平按钮 + 微软雅黑），
分P用可滚动的勾选卡片，绑定滚轮，支持一次勾多个批量下载
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from tkinter import messagebox, ttk

from ..config.settings import STYLE
from ..core.client import QUALITY_ORDER, quality_label, quality_permission
from ..core.models import DownloadFormat, VideoItem, VideoPage
from .widgets import bind_mousewheel_for_canvas, make_button, make_label

#音频档位：B站dash音频id（30280=192k，30232=132k，30216=64k）
AUDIO_TIERS: tuple[tuple[str, int], ...] = (
    ("高（约 192 kbps）", 30280),
    ("中（约 132 kbps）", 30232),
    ("低（约 64 kbps）", 30216),
)

FORMAT_ORDER: tuple[DownloadFormat, ...] = (
    DownloadFormat.VIDEO,
    DownloadFormat.AUDIO,
    DownloadFormat.AUDIO_MP3,
    DownloadFormat.AUDIO_WAV,
    DownloadFormat.COVER,
)


@dataclass(slots=True)
class DownloadChoice:
    """用户在弹窗里确认的下载参数"""

    format: DownloadFormat
    qn: int = 0
    audio_qn: int = 0
    page_indices: list[int] = field(default_factory=lambda: [1])


class _DownloadDialog:
    def __init__(
        self,
        parent: tk.Misc,
        item: VideoItem,
        pages: list[VideoPage],
        logged_in: bool,
        prefill_format: DownloadFormat = DownloadFormat.VIDEO,
    ) -> None:
        self.item = item
        self.pages = pages or [VideoPage(index=1, cid=item.cid, title="")]
        self.logged_in = logged_in
        self.prefill = prefill_format
        self.result: DownloadChoice | None = None
        self.page_vars: list[tuple[int, tk.BooleanVar]] = []

        self.top = tk.Toplevel(parent)
        self.top.title("下载设置")
        self.top.configure(bg=str(STYLE["frame_bg"]))
        self.top.resizable(False, False)
        #显式拉回不透明，避免子窗口继承主窗口的透明度设置
        try:
            self.top.attributes("-alpha", 1.0)
        except tk.TclError:  #pragma: no cover
            pass
        self.top.transient(parent.winfo_toplevel())
        self.top.grab_set()

        self._build()
        self.top.update_idletasks()
        self._center(parent)

    #------------------------------------------------------------------ 构建
    def _build(self) -> None:
        wrap = tk.Frame(self.top, bg=str(STYLE["frame_bg"]), padx=22, pady=18)
        wrap.pack(fill=tk.BOTH, expand=True)

        title = self.item.title if len(self.item.title) <= 40 else self.item.title[:40] + "…"
        make_label(wrap, title, font_key="title_font", wraplength=440).pack(anchor="w")
        make_label(
            wrap,
            f"{self.item.author}  ·  {self.item.bvid}",
            fg=str(STYLE["muted_text"]),
        ).pack(anchor="w", pady=(2, 10))

        self._build_pages(wrap)

        #---- 格式
        make_label(wrap, "下载内容", font_key="normal_font").pack(anchor="w", pady=(10, 0))
        self.format_var = tk.StringVar()
        self.format_box = ttk.Combobox(
            wrap,
            textvariable=self.format_var,
            state="readonly",
            values=[f.label for f in FORMAT_ORDER],
            width=48,
            font=STYLE["normal_font"],  #type: ignore[arg-type]
        )
        labels = [f.label for f in FORMAT_ORDER]
        index = labels.index(self.prefill.label) if self.prefill.label in labels else 0
        self.format_box.current(index)
        self.format_box.pack(anchor="w", pady=(4, 10))
        self.format_box.bind("<<ComboboxSelected>>", lambda _e: self._refresh_quality())

        #---- 质量
        make_label(wrap, "质量", font_key="normal_font").pack(anchor="w")
        self.quality_var = tk.StringVar()
        self.quality_box = ttk.Combobox(
            wrap,
            textvariable=self.quality_var,
            state="readonly",
            width=48,
            font=STYLE["normal_font"],  #type: ignore[arg-type]
        )
        self.quality_box.pack(anchor="w", pady=(4, 6))

        self.hint = make_label(wrap, "", fg=str(STYLE["muted_text"]), wraplength=440)
        self.hint.pack(anchor="w", pady=(0, 12))
        self._refresh_quality()

        #---- 按钮
        bar = tk.Frame(wrap, bg=str(STYLE["frame_bg"]))
        bar.pack(fill=tk.X, pady=(2, 0))
        make_button(bar, "取消", self._cancel, bg="#9aa0a6").pack(side=tk.RIGHT, padx=(8, 0))
        make_button(bar, "开始下载", self._confirm).pack(side=tk.RIGHT)

        self.top.bind("<Escape>", lambda _e: self._cancel())

    def _build_pages(self, wrap: tk.Misc) -> None:
        """分P列表：可滚动卡片 + 勾选，支持多选批量"""
        header = tk.Frame(wrap, bg=str(STYLE["frame_bg"]))
        header.pack(fill=tk.X)
        make_label(
            header,
            "选择分 P（可多选，勾几个就下一个，仅限视频/音频）",
            font_key="normal_font",
        ).pack(side=tk.LEFT)

        if len(self.pages) <= 1:
            single = self.pages[0]
            make_label(
                wrap,
                f"单 P 视频：{single.title or '（无分 P 标题）'}",
                fg=str(STYLE["muted_text"]),
            ).pack(anchor="w", pady=(4, 0))
            self.page_vars = [(single.index, _TrueVar())]
            return

        make_button(
            header, "全选", self._select_all, font_key="small_font", padx=8, pady=2
        ).pack(side=tk.RIGHT, padx=(4, 0))
        make_button(
            header, "清空", self._clear_all, bg="#9aa0a6",
            font_key="small_font", padx=8, pady=2,
        ).pack(side=tk.RIGHT)

        box = tk.Frame(
            wrap,
            bg="white",
            highlightthickness=1,
            highlightbackground="#d7dbe0",
        )
        box.pack(fill=tk.X, pady=(4, 0))

        canvas = tk.Canvas(
            box, bg="white", height=132, highlightthickness=0, bd=0
        )
        scroll = ttk.Scrollbar(box, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg="white")
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        inner.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfigure(window_id, width=e.width),
        )

        for page in self.pages:
            var = tk.BooleanVar(value=False)
            self.page_vars.append((page.index, var))
            tk.Checkbutton(
                inner,
                text=page.display(),
                variable=var,
                bg="white",
                fg=str(STYLE["text_color"]),
                activebackground="white",
                font=STYLE["normal_font"],  #type: ignore[arg-type]
                anchor="w",
                padx=8,
                pady=2,
            ).pack(fill=tk.X)

        self.page_vars[0][1].set(True)  #默认勾第一个
        bind_mousewheel_for_canvas(inner, canvas, include=canvas)

    #------------------------------------------------------------- 分P操作
    def _select_all(self) -> None:
        for _index, var in self.page_vars:
            var.set(True)

    def _clear_all(self) -> None:
        for _index, var in self.page_vars:
            var.set(False)

    def _selected_pages(self) -> list[int]:
        return sorted(index for index, var in self.page_vars if var.get())

    #------------------------------------------------------------- 质量联动
    def _current_format(self) -> DownloadFormat:
        label = self.format_var.get()
        for fmt in FORMAT_ORDER:
            if fmt.label == label:
                return fmt
        return DownloadFormat.VIDEO

    def _refresh_quality(self) -> None:
        fmt = self._current_format()
        if fmt is DownloadFormat.VIDEO:
            values = ["自动（该视频最高）", *[quality_label(q) for q in QUALITY_ORDER]]
            hint = (
                "720P 及以上需要登录 B 站账号；1080P 高码率 / 4K / HDR 需要大会员。"
                if not self.logged_in
                else "1080P 以上是否可用取决于账号权限，没有该档会自动回退。"
            )
        elif fmt.is_audio:
            values = [name for name, _ in AUDIO_TIERS]
            hint = (
                "音频取自 DASH 音轨；M4A 不转码，MP3 / WAV 会用 ffmpeg 转一次，"
                "勾多个分 P 会依次入队。"
            )
        else:
            values = ["原图（最高分辨率）"]
            hint = "封面保存为 cover.jpg；封面只能选一个分 P。"
        self.quality_box.configure(values=values)
        self.quality_box.current(0)
        self.hint.configure(text=hint)

    #---------------------------------------------------------------- 动作
    def _confirm(self) -> None:
        fmt = self._current_format()
        pages = self._selected_pages()
        if not pages:
            messagebox.showwarning("没选分 P", "请至少勾选一个分 P。", parent=self.top)
            return
        if fmt is DownloadFormat.COVER and len(pages) > 1:
            messagebox.showwarning(
                "封面只能单个", "封面没有分 P 概念，请只勾选一个分 P。", parent=self.top
            )
            return
        if fmt is DownloadFormat.COVER:
            pages = pages[:1]

        qn = 0
        audio_qn = 0
        if fmt is DownloadFormat.VIDEO:
            idx = self.quality_box.current()
            if idx > 0:
                qn = QUALITY_ORDER[idx - 1]
                need = quality_permission(qn)
                if need and not self.logged_in:
                    reason = "登录 B 站账号" if need == "login" else "开通大会员"
                    messagebox.showwarning(
                        "权限不足",
                        f"{quality_label(qn)} 需要先{reason}。\n可以去「登录」页扫码，或改选低一档。",
                        parent=self.top,
                    )
                    return
        elif fmt.is_audio:
            audio_qn = AUDIO_TIERS[max(0, self.quality_box.current())][1]

        self.result = DownloadChoice(
            format=fmt, qn=qn, audio_qn=audio_qn, page_indices=pages
        )
        self.top.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.top.destroy()

    def _center(self, parent: tk.Misc) -> None:
        try:
            root = parent.winfo_toplevel()
            x = root.winfo_rootx() + (root.winfo_width() - self.top.winfo_width()) // 2
            y = root.winfo_rooty() + (root.winfo_height() - self.top.winfo_height()) // 2
            self.top.geometry(f"+{max(0, x)}+{max(0, y)}")
        except tk.TclError:  #pragma: no cover
            pass


def _TrueVar() -> tk.BooleanVar:
    """单P场景用的“永远为真”勾选变量"""
    var = tk.BooleanVar(value=True)
    return var


def ask_download_options(
    parent: tk.Misc,
    item: VideoItem,
    *,
    pages: list[VideoPage] | None = None,
    logged_in: bool = False,
    prefill_format: DownloadFormat = DownloadFormat.VIDEO,
) -> DownloadChoice | None:
    """弹出下载设置；用户取消返回None"""
    dialog = _DownloadDialog(
        #pages为空列表时不再回退item.pages：批量模式据此只展示单P
        parent,
        item,
        list(pages) if pages is not None else list(item.pages),
        logged_in,
        prefill_format,
    )
    parent.wait_window(dialog.top)
    return dialog.result
