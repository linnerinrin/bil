"""测试辅助工具（两种运行方式都可用）。

提供带确定性回收的 Tk root 上下文管理器：

Tk 对象的 C 级析构若在非主线程触发，解释器会以
``Tcl_AsyncDelete: async handler deleted by the wrong thread`` 直接崩溃
（Windows 上是 0x80000003，Python 层捕获不到）。
界面测试会频繁创建/销毁 Tk root，因此销毁后在**主线程**强制回收，
把析构时机变成确定的，消除这个测试装置竞态。
"""

from __future__ import annotations

import contextlib
import gc
from collections.abc import Iterator

try:
    import tkinter as tk

    _probe = tk.Tk()
    _probe.withdraw()
    _probe.destroy()
    del _probe
    gc.collect()
    TK_AVAILABLE = True
except Exception:  # noqa: BLE001
    TK_AVAILABLE = False


@contextlib.contextmanager
def tk_root() -> Iterator[object]:
    """创建一个隐藏的 Tk root，退出时销毁并确定性回收。"""
    root = tk.Tk()
    root.withdraw()
    try:
        yield root
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
        del root
        gc.collect()
