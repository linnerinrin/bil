"""界面层基础类型。"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from ..config.settings import STYLE


class BaseView(tk.Frame):
    """所有视图的基类。

    注册 ``_on_destroy_hook``，让 MainWindow.clear_content() 在销毁控件前
    有机会取消定时器／轮询，避免「对已销毁控件调用 after」这类报错（审计缺陷 B6）。
    """

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, bg=str(STYLE["frame_bg"]))
        self._destroy_hooks: list[Callable[[], None]] = []
        self._on_destroy_hook: Callable[[], None] = self._run_destroy_hooks
        self._alive = True

    # ---------------------------------------------------------- 生命周期
    def add_destroy_hook(self, hook: Callable[[], None]) -> None:
        self._destroy_hooks.append(hook)

    def _run_destroy_hooks(self) -> None:
        self._alive = False
        for hook in self._destroy_hooks:
            try:
                hook()
            except Exception:  # noqa: BLE001 - 清理阶段不应再抛错
                pass
        self._destroy_hooks.clear()

    @property
    def alive(self) -> bool:
        """控件是否仍然存在（定时回调里用它做守卫）。"""
        if not self._alive:
            return False
        try:
            return bool(self.winfo_exists())
        except tk.TclError:  # pragma: no cover - 解释器/控件已销毁
            return False

    def destroy(self) -> None:
        """销毁前先跑清理钩子，保证排定的 after 任务一定被取消（修复 B6）。"""
        if self._alive:
            self._run_destroy_hooks()
        try:
            super().destroy()
        except tk.TclError:  # pragma: no cover - 已被父级递归销毁
            pass
