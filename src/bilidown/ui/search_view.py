"""搜索视图：关键词/BV 搜索、结果列表、单条与批量下载。

修复审计缺陷 B7/B8：
  B7  搜索不再在 Tk 主线程 asyncio.run 阻塞界面；由控制器提交后台事件循环，
      结果通过回调切回主线程渲染。
  B8  滚轮事件绑定在 canvas 上（而非 content_frame 全局覆盖绑定），
      且视图销毁时自动解绑，不再影响其他页面、也不会打到已销毁控件。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from ..config.settings import STYLE
from ..core.models import VideoItem
from .base import BaseView
from .widgets import bind_mousewheel, make_button, make_label, make_scrollable, unbind_mousewheel


class SearchView(BaseView):
    """搜索结果界面。"""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        on_search: Callable[[str, int], None],
        on_copy: Callable[[VideoItem], None],
        on_download: Callable[[VideoItem, str], None],
        on_batch: Callable[[list[VideoItem], str], None],
    ) -> None:
        super().__init__(parent)
        self._on_search = on_search
        self._on_copy = on_copy
        self._on_download = on_download
        self._on_batch = on_batch
        self._items: list[VideoItem] = []
        self._page = 1
        self._keyword = ""

        self._build_toolbar()
        self._build_result_area()
        self._build_footer()

        # 滚轮绑定：固定容器只绑一次；
        # 结果卡片是每次渲染新建的，所以要在 _render_item 里逐个绑定
        # （Tk 事件不冒泡，只绑容器收不到子控件上的滚轮）
        bind_mousewheel(self.canvas, self.canvas)
        bind_mousewheel(self.results_frame, self.canvas)
        self.add_destroy_hook(self._unbind_wheel)

    # ------------------------------------------------------------- 构建
    def _build_toolbar(self) -> None:
        bar = tk.Frame(self, bg=str(STYLE["frame_bg"]))
        bar.pack(fill=tk.X, padx=30, pady=(16, 8))

        self.search_var = tk.StringVar()
        self.entry = tk.Entry(
            bar,
            textvariable=self.search_var,
            font=STYLE["normal_font"],  # type: ignore[arg-type]
            bg="white",
            fg=str(STYLE["text_color"]),
            bd=2,
            relief=tk.FLAT,
            insertbackground=str(STYLE["text_color"]),
            highlightthickness=1,
            highlightcolor="#00a1d6",
        )
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=8, padx=(0, 10))
        self.entry.bind("<Return>", lambda _e: self._trigger_search(1))

        self.search_btn = make_button(bar, "搜索", lambda: self._trigger_search(1))
        self.search_btn.pack(side=tk.RIGHT)

        self.tip_label = make_label(
            self,
            "",
            font_key="small_font",
            fg=str(STYLE["accent_color"]),
            bg=str(STYLE["frame_bg"]),
        )
        self.tip_label.pack(fill=tk.X, padx=34)

    def _build_result_area(self) -> None:
        container = tk.Frame(self, bg=str(STYLE["frame_bg"]))
        container.pack(fill=tk.BOTH, expand=True, padx=30, pady=(4, 0))
        self.canvas, self.results_frame, self.scrollbar = make_scrollable(
            container, str(STYLE["frame_bg"])
        )
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_footer(self) -> None:
        footer = tk.Frame(self, bg=str(STYLE["frame_bg"]))
        footer.pack(fill=tk.X, padx=30, pady=(6, 12))

        self.prev_btn = make_button(
            footer, "上一页", lambda: self._trigger_search(max(1, self._page - 1)), padx=12, pady=5
        )
        self.prev_btn.pack(side=tk.LEFT)

        self.page_label = make_label(
            footer, "第 1 页", font_key="small_font", bg=str(STYLE["frame_bg"])
        )
        self.page_label.pack(side=tk.LEFT, padx=12)

        self.next_btn = make_button(
            footer, "下一页", lambda: self._trigger_search(self._page + 1), padx=12, pady=5
        )
        self.next_btn.pack(side=tk.LEFT)

        self.batch_video_btn = make_button(
            footer,
            "批量下载本页视频",
            lambda: self._batch("video"),
            bg="#ff9800",
            padx=12,
            pady=5,
        )
        self.batch_video_btn.pack(side=tk.RIGHT)

        self.batch_audio_btn = make_button(
            footer, "批量下载本页音频", lambda: self._batch("audio"), bg="#aa9800", padx=12, pady=5
        )
        self.batch_audio_btn.pack(side=tk.RIGHT, padx=8)

    # ------------------------------------------------------------- 事件
    def _unbind_wheel(self) -> None:
        for widget in (getattr(self, "canvas", None), getattr(self, "results_frame", None)):
            if widget is not None:
                unbind_mousewheel(widget)

    def _trigger_search(self, page: int) -> None:
        keyword = self.search_var.get().strip()
        if not keyword:
            self.set_tip("请输入 BV 号、视频链接或搜索关键词")
            return
        self.set_tip(f"正在搜索「{keyword}」…")
        self.set_loading(True)
        self._on_search(keyword, max(1, page))

    def _batch(self, kind: str) -> None:
        if not self._items:
            self.set_tip("当前没有可下载的结果")
            return
        self._on_batch(list(self._items), kind)

    # ------------------------------------------------------------- 渲染
    def set_loading(self, loading: bool) -> None:
        if not self.alive:
            return
        state = tk.DISABLED if loading else tk.NORMAL
        self.search_btn.config(state=state)
        self.entry.config(state=state)
        self.prev_btn.config(state=state)
        self.next_btn.config(state=state)
        self.batch_video_btn.config(state=state)
        self.batch_audio_btn.config(state=state)

    def set_tip(self, text: str) -> None:
        if not self.alive:
            return
        self.tip_label.config(text=text)

    def clear_canvas(self) -> None:
        if not self.alive:
            return
        for widget in self.results_frame.winfo_children():
            widget.destroy()

    def render(self, items: list[VideoItem], keyword: str, page: int) -> None:
        """渲染一页搜索结果。"""
        if not self.alive:
            return
        self._items = list(items)
        self._keyword = keyword
        self._page = page
        self.page_label.config(text=f"第 {page} 页")
        self.set_loading(False)
        self.clear_canvas()

        if not items:
            self.set_tip("未找到相关视频")
            self._refresh_scrollregion()
            return

        self.set_tip(f"共 {len(items)} 条结果（点击按钮下载，或使用底部批量下载）")
        for index, video in enumerate(items, start=1):
            self._render_item(index, video)
        self._refresh_scrollregion()

    def _render_item(self, index: int, video: VideoItem) -> None:
        card = tk.Frame(
            self.results_frame,
            bg=str(STYLE["frame_bg"]),
            bd=1,
            relief=tk.GROOVE,
        )
        card.pack(fill=tk.X, padx=6, pady=5)

        info = tk.Frame(card, bg=str(STYLE["frame_bg"]))
        info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=12, pady=8)

        make_label(
            info,
            f"{index}. {video.title}",
            font_key="title_font",
            wraplength=640,
            bg=str(STYLE["frame_bg"]),
        ).pack(anchor="w")

        duration = _format_duration(video.duration)
        meta = (
            f"BV号: {video.bvid}    UP主: {video.author or '未知'}"
            f"    播放: {video.play}    弹幕: {video.danmaku}"
        )
        if duration:
            meta += f"    时长: {duration}"
        make_label(
            info,
            meta,
            font_key="small_font",
            fg="#00a1d6",
            bg=str(STYLE["frame_bg"]),
        ).pack(anchor="w", pady=(4, 0))

        actions = tk.Frame(card, bg=str(STYLE["frame_bg"]))
        actions.pack(side=tk.RIGHT, padx=12, pady=8)

        make_button(
            actions,
            "复制信息",
            lambda v=video: self._on_copy(v),
            bg="#4CAF50",
            font_key="small_font",
            padx=10,
            pady=5,
        ).pack(fill=tk.X, pady=2)
        make_button(
            actions,
            "下载视频",
            lambda v=video: self._on_download(v, "video"),
            bg="#ff9800",
            font_key="small_font",
            padx=10,
            pady=5,
        ).pack(fill=tk.X, pady=2)
        make_button(
            actions,
            "下载音频",
            lambda v=video: self._on_download(v, "audio"),
            bg="#aa9800",
            font_key="small_font",
            padx=10,
            pady=5,
        ).pack(fill=tk.X, pady=2)

        # Tk 事件不冒泡，卡片是子控件：必须把这张卡片（含其子控件）也绑上滚轮，
        # 否则鼠标停在结果上时无法滚动
        bind_mousewheel(card, self.canvas)

    def _refresh_scrollregion(self) -> None:
        if not self.alive:
            return
        self.canvas.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    @property
    def page(self) -> int:
        return self._page

    @property
    def keyword(self) -> str:
        return self._keyword

    @property
    def items(self) -> list[VideoItem]:
        return list(self._items)


def _format_duration(seconds: object) -> str:
    """把时长渲染成 ``M:SS`` / ``H:MM:SS``。

    接口返回值类型并不稳定（可能是 ``"7:49"`` 这样的字符串），
    这里先做一次解析兜底，绝不抛异常。
    """
    from ..core.client import parse_duration

    total = parse_duration(seconds)
    if total <= 0:
        # 已经是形如 "7:49" 的字符串就原样显示，解析不出来才留空
        text = str(seconds or "").strip()
        return text if ":" in text else ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
