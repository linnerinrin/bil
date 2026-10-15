"""界面层的通用小部件与辅助函数（仅布局，不含业务逻辑）。"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ..config.settings import STYLE


def apply_acrylic_style(widget: tk.Misc, value: float = 0.9) -> bool:
    """给控件设置透明度（仅 Windows 且安装 pywinstyles 时生效）。

    旧代码直接调用 pywinstyles.set_opacity，非 Windows 平台或该库 API 变化时
    会直接抛异常导致整个界面构建失败；这里做成「尽力而为」。
    """
    try:
        import pywinstyles  # type: ignore[import-untyped]

        pywinstyles.set_opacity(widget, value=value)
        return True
    except Exception:  # noqa: BLE001 - 视觉增强失败不应影响功能
        return False


def make_button(
    parent: tk.Misc,
    text: str,
    command: Callable[[], None],
    *,
    bg: str | None = None,
    fg: str = "white",
    font_key: str = "normal_font",
    padx: int = 20,
    pady: int = 8,
) -> tk.Button:
    """统一的扁平按钮。"""
    return tk.Button(
        parent,
        text=text,
        command=command,
        font=STYLE[font_key],  # type: ignore[arg-type]
        bg=bg or str(STYLE["btn_bg"]),
        fg=fg,
        bd=0,
        relief=tk.FLAT,
        padx=padx,
        pady=pady,
        cursor="hand2",
        activebackground=str(STYLE["btn_hover_bg"]),
        activeforeground=fg,
    )


def make_label(
    parent: tk.Misc,
    text: str,
    *,
    font_key: str = "small_font",
    fg: str | None = None,
    bg: str | None = None,
    wraplength: int = 0,
    anchor: str = "w",
    justify: str = "left",
) -> tk.Label:
    """统一的信息标签。"""
    return tk.Label(
        parent,
        text=text,
        font=STYLE[font_key],  # type: ignore[arg-type]
        fg=fg or str(STYLE["text_color"]),
        bg=bg or str(STYLE["frame_bg"]),
        wraplength=wraplength,
        anchor=anchor,
        justify=justify,
    )


def make_treeview(
    parent: tk.Misc,
    columns: tuple[tuple[str, str, int], ...],
    *,
    height: int = 12,
) -> ttk.Treeview:
    """带列定义的 Treeview。columns 为 (列名, 表头, 宽度)。"""
    names = tuple(c[0] for c in columns)
    tree = ttk.Treeview(parent, columns=names, show="headings", height=height)
    for name, heading, width in columns:
        tree.heading(name, text=heading)
        tree.column(name, width=width, anchor="w")
    return tree


def make_scrollable(parent: tk.Misc, bg: str) -> tuple[tk.Canvas, tk.Frame, tk.Scrollbar]:
    """创建可滚动区域，返回 (canvas, 内部 Frame, scrollbar)。

    注意：这里**不**绑定滚轮事件，由调用方通过 bind_mousewheel() 绑定，
    以便在视图销毁时能可靠解绑（否则会打到已销毁的控件上）。
    """
    canvas = tk.Canvas(parent, highlightthickness=0, bg=bg)
    scrollbar = tk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=bg)
    window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _on_inner_configure(_event: tk.Event) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_canvas_configure(event: tk.Event) -> None:
        canvas.itemconfigure(window_id, width=event.width)

    inner.bind("<Configure>", _on_inner_configure)
    canvas.bind("<Configure>", _on_canvas_configure)
    canvas.configure(yscrollcommand=scrollbar.set)
    return canvas, inner, scrollbar


def wheel_delta_units(event: tk.Event) -> int:
    """把滚轮事件换算成「向上为正的滚动行数」。

    约定：返回值 > 0 表示内容上移（看更靠后的内容）。
    - Windows: delta 为 ±120 的倍数，向前滚一格是 +120
    - macOS:   delta 为较小的整数，符号约定与 Windows 相同
    - Linux:   用 Button-4（上）/ Button-5（下）
    """
    delta = getattr(event, "delta", 0)
    if delta:
        # 先把 delta 归一成 ±1（Windows 是 ±120），再取反得到「行数」
        steps = delta // 120 if abs(delta) >= 120 else delta
        return -int(steps) if steps else 0
    num = getattr(event, "num", None)
    if num == 4:
        return -1
    if num == 5:
        return 1
    return 0


def scroll_widget(widget: tk.Misc, units: int) -> None:
    """滚动一个控件，自动适配它支持的单位。

    ``ttk.Treeview`` **只支持 "pages"**，传 "units" 会抛 TclError；
    ``Canvas`` / ``Listbox`` / ``Text`` 支持 "units"。先试 "units"，失败退到 "pages"。
    """
    try:
        widget.yview_scroll(units, "units")
        return
    except Exception:  # noqa: BLE001 - TclError 或控件签名不同
        pass
    try:
        widget.yview_scroll(units, "pages")
    except Exception:  # noqa: BLE001 - 控件已销毁等
        pass


def bind_mousewheel(widget: tk.Misc, target: tk.Misc | None = None) -> None:
    """让 widget 区域内的滚轮事件驱动 target 纵向滚动。

    - ``target`` 缺省为 ``widget`` 自身（适用于 Treeview / Listbox / Text）；
    - 对 Canvas 传 ``target=canvas``；
    - 同时绑定 ``<MouseWheel>``（Windows/macOS）与 ``<Button-4>/<Button-5>``（Linux）。

    Tk 的事件**不会冒泡**，所以只绑容器是不够的：鼠标停在子控件上时事件会发给
    子控件。这里递归绑定 widget 的**全部现有后代**。

    ⚠️ 因为是「现有后代」，所以必须在**内容创建完成之后**再调用；
    否则后建的卡片收不到事件（这正是设置页滚不动的根因）。
    若内容会动态增减，请改用 :func:`bind_mousewheel_for_canvas` 的思路，
    或在每次创建子控件后再调一次。
    """
    scrollable = target if target is not None else widget

    def _on_wheel(event: tk.Event) -> None:
        units = wheel_delta_units(event)
        if units:
            scroll_widget(scrollable, units)

    _bind_tree(widget, _on_wheel)


def bind_mousewheel_for_canvas(
    container: tk.Misc,
    canvas: tk.Canvas,
    *,
    include: tk.Misc | None = None,
) -> None:
    """给「可滚动 canvas + 容器 Frame」这种结构绑定滚轮。

    从 container 起整树遍历，**每个控件只绑一次**（含 canvas；跳过滚动条），
    因此不会因为重复遍历造成「一次滚轮触发多次滚动、速度忽快忽慢」。

    必须在内容创建完毕之后调用（遍历的是当前已存在的后代）。
    """
    targets = [container]
    if include is not None and include is not container:
        targets.append(include)

    seen: set[str] = set()

    def _on_wheel(event: tk.Event) -> None:
        units = wheel_delta_units(event)
        if units:
            scroll_widget(canvas, units)

    for root_widget in targets:
        for node in _walk(root_widget):
            if "scrollbar" in node.winfo_class().lower():
                continue  # 滚动条有自己的拖动行为
            key = str(node)
            if key in seen:
                continue
            seen.add(key)
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                try:
                    node.bind(sequence, _on_wheel, add="+")
                except tk.TclError:  # pragma: no cover
                    pass


def _walk(widget: tk.Misc):
    """深度优先遍历控件树（含自身）。"""
    try:
        children = widget.winfo_children()
    except tk.TclError:  # pragma: no cover
        children = []
    for child in children:
        yield from _walk(child)
    yield widget


def _bind_tree(widget: tk.Misc, handler: Any) -> None:
    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        try:
            widget.bind(sequence, handler, add="+")
        except tk.TclError:  # pragma: no cover
            pass
    try:
        children = widget.winfo_children()
    except tk.TclError:  # pragma: no cover
        return
    for child in children:
        _bind_tree(child, handler)


def unbind_mousewheel(widget: tk.Misc) -> None:
    """递归解绑滚轮事件（视图销毁时调用，避免打到已销毁控件）。"""
    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        try:
            widget.unbind(sequence)
        except tk.TclError:  # pragma: no cover
            pass
    try:
        children = widget.winfo_children()
    except tk.TclError:  # pragma: no cover
        return
    for child in children:
        unbind_mousewheel(child)


def show_error(title: str, message: str) -> None:
    """统一错误弹窗（延迟导入，便于无显示环境下导入本模块）。"""
    from tkinter import messagebox

    messagebox.showerror(title, message)


def ask_yes_no(title: str, message: str) -> bool:
    from tkinter import messagebox

    return bool(messagebox.askyesno(title, message))
