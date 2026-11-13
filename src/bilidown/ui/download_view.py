"""下载任务列表视图，由控制器按固定间隔调用 ``refresh()`` 增量更新单元格"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ..config.settings import STYLE
from ..core.models import DownloadStatus, DownloadTask
from .base import BaseView
from .widgets import bind_mousewheel, make_button, make_label, make_treeview, unbind_mousewheel

COLUMNS = (
    ("bvid", "BV号", 110),
    ("title", "视频标题", 330),
    ("kind", "类型", 70),
    ("progress", "进度", 80),
    ("status", "状态", 90),
    ("message", "说明", 200),
)

#类型列显示产物格式（封面任务也是音频kind，直接显示kind会误导）
_KIND_LABELS = {
    "mp4": "视频",
    "m4a": "音频",
    "mp3": "MP3",
    "jpg": "封面",
}


class DownloadView(BaseView):
    """下载任务表格 + 操作按钮"""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        on_cancel: Callable[[DownloadTask], None],
        on_retry: Callable[[DownloadTask], None],
        on_remove: Callable[[DownloadTask], None],
        on_clear_finished: Callable[[], None],
        on_open_folder: Callable[[], None],
        on_open_file: Callable[[DownloadTask], None],
    ) -> None:
        super().__init__(parent)
        self._on_cancel = on_cancel
        self._on_retry = on_retry
        self._on_remove = on_remove
        self._on_clear_finished = on_clear_finished
        self._on_open_folder = on_open_folder
        self._on_open_file = on_open_file

        self._rows: dict[str, str] = {}  #task_id -> tree iid
        self._task_by_iid: dict[str, DownloadTask] = {}

        header = tk.Frame(self, bg=str(STYLE["frame_bg"]))
        header.pack(fill=tk.X, padx=20, pady=(14, 6))
        make_label(
            header, "下载任务列表", font_key="title_font", bg=str(STYLE["frame_bg"])
        ).pack(side=tk.LEFT)

        self.summary_label = make_label(
            header, "暂无任务", font_key="small_font", fg=str(STYLE["muted_text"]),
            bg=str(STYLE["frame_bg"]),
        )
        self.summary_label.pack(side=tk.RIGHT)

        body = tk.Frame(self, bg=str(STYLE["frame_bg"]))
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 6))

        self.tree = make_treeview(body, COLUMNS, height=12)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Double-1>", self._on_double_click)

        #绑定滚轮事件
        bind_mousewheel(self.tree)
        bind_mousewheel(body, self.tree)
        bind_mousewheel(self, self.tree)
        self.add_destroy_hook(self._unbind_wheel)

        self._build_toolbar()

    def _unbind_wheel(self) -> None:
        for widget in (getattr(self, "tree", None), self):
            if widget is not None:
                unbind_mousewheel(widget)

    def _build_toolbar(self) -> None:
        bar = tk.Frame(self, bg=str(STYLE["frame_bg"]))
        bar.pack(fill=tk.X, padx=20, pady=(0, 14))

        make_button(bar, "取消选中", self._cancel_selected, bg="#e57373",
                    font_key="small_font", padx=12, pady=5).pack(side=tk.LEFT, padx=(0, 6))
        make_button(bar, "重试选中", self._retry_selected, bg="#64b5f6",
                    font_key="small_font", padx=12, pady=5).pack(side=tk.LEFT, padx=6)
        make_button(bar, "移除选中", self._remove_selected, bg="#9e9e9e",
                    font_key="small_font", padx=12, pady=5).pack(side=tk.LEFT, padx=6)
        make_button(bar, "清理已结束", self._on_clear_finished, bg="#9e9e9e",
                    font_key="small_font", padx=12, pady=5).pack(side=tk.LEFT, padx=6)
        make_button(bar, "打开下载目录", self._on_open_folder, bg="#00a1d6",
                    font_key="small_font", padx=12, pady=5).pack(side=tk.RIGHT)

    #------------------------------------------------------------- 刷新
    def refresh(self, tasks: list[DownloadTask]) -> None:
        """按task_id增量更新表格"""
        try:
            if not self.alive:
                return
        except tk.TclError:  #pragma: no cover - 控件已销毁
            return

        seen: set[str] = set()
        for index, task in enumerate(tasks):
            seen.add(task.task_id)
            values = self._values_for(task)
            iid = self._rows.get(task.task_id)
            if iid is None or not self.tree.exists(iid):
                iid = self.tree.insert("", "end", iid=task.task_id, values=values)
                self._rows[task.task_id] = iid
            else:
                if self.tree.item(iid, "values") != values:
                    self.tree.item(iid, values=values)
                position = self.tree.index(iid)
                if position != index:
                    self.tree.move(iid, "", index)
            self._task_by_iid[iid] = task

        for task_id in list(self._rows):
            if task_id not in seen:
                iid = self._rows.pop(task_id)
                self._task_by_iid.pop(iid, None)
                if self.tree.exists(iid):
                    self.tree.delete(iid)

        self._update_summary(tasks)

    @staticmethod
    def _values_for(task: DownloadTask) -> tuple[str, ...]:
        kind = _KIND_LABELS.get(task.format.value, "未知")
        if len(task.item.pages) > 1:
            kind = f"{kind}·P{task.page_index}"
        return (
            task.item.bvid,
            task.item.title,
            kind,
            task.progress_text(),
            task.status.value,
            task.message,
        )

    def _update_summary(self, tasks: list[DownloadTask]) -> None:
        if not tasks:
            self.summary_label.config(text="暂无任务")
            return
        done = sum(1 for t in tasks if t.status is DownloadStatus.COMPLETED)
        failed = sum(1 for t in tasks if t.status is DownloadStatus.FAILED)
        active = sum(1 for t in tasks if not t.status.is_terminal)
        self.summary_label.config(
            text=f"共 {len(tasks)} 个任务 · 进行中 {active} · 完成 {done} · 失败 {failed}"
        )

    #------------------------------------------------------------- 选择
    def _selected_tasks(self) -> list[DownloadTask]:
        result = []
        for iid in self.tree.selection():
            task = self._task_by_iid.get(iid)
            if task is not None:
                result.append(task)
        return result

    def _cancel_selected(self) -> None:
        for task in self._selected_tasks():
            if not task.status.is_terminal:
                self._on_cancel(task)

    def _retry_selected(self) -> None:
        for task in self._selected_tasks():
            self._on_retry(task)

    def _remove_selected(self) -> None:
        for task in self._selected_tasks():
            iid = self._rows.pop(task.task_id, None)
            if iid:
                self._task_by_iid.pop(iid, None)
                if self.tree.exists(iid):
                    self.tree.delete(iid)
            self._on_remove(task)

    def _on_double_click(self, _event: tk.Event) -> None:
        tasks = self._selected_tasks()
        if tasks:
            self._on_open_file(tasks[0])

    #------------------------------------------------------------- 工具
    @staticmethod
    def open_path(path: str) -> None:
        """用系统默认方式打开文件或目录"""
        if not path or not os.path.exists(path):
            return
        if sys.platform == "win32":
            os.startfile(path)  #type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])  #noqa: S603,S607
        else:
            subprocess.Popen(["xdg-open", path])  #noqa: S603,S607
