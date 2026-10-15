"""滚轮滚动测试：纯逻辑 + 真实 Tk 控件绑定验证。

不触网。无显示环境自动跳过 Tk 部分。
"""

from __future__ import annotations

from bilidown.ui.widgets import scroll_widget, wheel_delta_units

from .helpers import TK_AVAILABLE, tk_root


class _FakeWheelEvent:
    def __init__(self, delta: int = 0, num: int | None = None) -> None:
        self.delta = delta
        if num is not None:
            self.num = num


class TestWheelDeltaUnits:
    """Windows delta 是 ±120 的倍数；方向必须正确（向前滚看更靠后内容）。"""

    def test_windows_scroll_forward(self) -> None:
        # 向前滚一格：delta=+120 -> yview_scroll(-1)
        assert wheel_delta_units(_FakeWheelEvent(delta=120)) == -1

    def test_windows_scroll_backward(self) -> None:
        assert wheel_delta_units(_FakeWheelEvent(delta=-120)) == 1

    def test_windows_multiple_notches(self) -> None:
        assert wheel_delta_units(_FakeWheelEvent(delta=360)) == -3
        assert wheel_delta_units(_FakeWheelEvent(delta=-240)) == 2

    def test_macos_small_delta(self) -> None:
        # macOS 的 delta 较小，符号约定与 Windows 相同
        assert wheel_delta_units(_FakeWheelEvent(delta=3)) == -3
        assert wheel_delta_units(_FakeWheelEvent(delta=-1)) == 1

    def test_linux_button_events(self) -> None:
        assert wheel_delta_units(_FakeWheelEvent(num=4)) == -1
        assert wheel_delta_units(_FakeWheelEvent(num=5)) == 1

    def test_zero_delta_is_noop(self) -> None:
        assert wheel_delta_units(_FakeWheelEvent(delta=0)) == 0
        assert wheel_delta_units(_FakeWheelEvent()) == 0


class TestScrollWidget:
    def test_units_preferred_when_supported(self) -> None:
        """支持 "units" 的控件应使用 "units"（更顺滑）。"""
        calls: list[tuple] = []

        class Stub:
            def yview_scroll(self, *args):
                calls.append(args)

        scroll_widget(Stub(), 3)  # type: ignore[arg-type]
        assert calls == [(3, "units")], calls

    def test_falls_back_to_pages_when_units_rejected(self) -> None:
        """关键回归：ttk.Treeview 传 "units" 会抛 TclError，必须退到 "pages"。"""
        import tkinter as tk

        calls: list[tuple] = []

        class Stub:
            def yview_scroll(self, *args):
                calls.append(args)
                if args[1] == "units":
                    raise tk.TclError('bad argument "units"')

        scroll_widget(Stub(), 2)  # type: ignore[arg-type]
        assert calls == [(2, "units"), (2, "pages")], calls

    def test_swallows_errors_from_destroyed_widget(self) -> None:
        """控件销毁后不能把异常抛出去打断工作线程。"""

        class AlwaysBoom:
            def yview_scroll(self, *args):
                raise RuntimeError("widget destroyed")

        scroll_widget(AlwaysBoom(), 1)  # type: ignore[arg-type]

    def test_treeview_only_supports_pages(self) -> None:
        """真实 ttk.Treeview 上不能抛异常（这是修复的核心）。"""
        if not TK_AVAILABLE:
            return
        from tkinter import ttk

        with tk_root() as root:
            tree = ttk.Treeview(root, columns=("a",), show="headings")
            for i in range(60):
                tree.insert("", "end", values=(f"row{i}",))
            tree.pack()
            root.update()
            # 不应抛异常
            scroll_widget(tree, 1)
            scroll_widget(tree, -1)
            view = tree.yview()
            assert isinstance(view, tuple) and len(view) == 2

    def test_scroll_on_destroyed_widget_is_silent(self) -> None:
        if not TK_AVAILABLE:
            return
        from tkinter import ttk

        with tk_root() as root:
            tree = ttk.Treeview(root, columns=("a",), show="headings")
            tree.pack()
            tree.destroy()
            scroll_widget(tree, 1)

    def test_canvas_binding_scrolls_and_is_reversible(self) -> None:
        """在真实 Canvas 上验证滚轮方向。

        Tk 的 ``yview()`` 分数：0.0 表示视图在**最顶部**，1.0 表示在最底部。
        因此「向下滚」应该让分数**变小**。
        """
        if not TK_AVAILABLE:
            return
        import tkinter as tk

        with tk_root() as root:
            root.geometry("300x200")
            canvas = tk.Canvas(root, height=150)
            canvas.pack(fill=tk.BOTH, expand=True)
            for i in range(60):
                canvas.create_text(10, i * 30, text=f"line {i}", anchor="nw")
            canvas.configure(scrollregion=(0, 0, 280, 1800))
            root.update()

            start = canvas.yview()[0]
            scroll_widget(canvas, 5)  # units > 0 = 向下
            root.update()
            down = canvas.yview()[0]
            assert down < start, f"向下滚后分数应减小: {start} -> {down}"

            scroll_widget(canvas, -5)  # units < 0 = 向上
            root.update()
            up = canvas.yview()[0]
            assert up > down, f"向上滚后分数应增大: {down} -> {up}"

    def test_wheel_event_direction_end_to_end(self) -> None:
        """把「滚轮事件」和「实际滚动方向」串起来验证，防止符号写反。

        注意：``event_generate`` 在**撤回(withdraw)的窗口**上不会派发，
        所以这里显式建一个可见窗口。
        """
        if not TK_AVAILABLE:
            return
        import tkinter as tk

        from bilidown.ui.widgets import bind_mousewheel

        root = tk.Tk()
        root.geometry("320x240+50+50")
        try:
            root.update()  # 让窗口真正映射
            canvas = tk.Canvas(root, height=150)
            canvas.pack(fill=tk.BOTH, expand=True)
            for i in range(60):
                canvas.create_text(10, i * 30, text=f"line {i}", anchor="nw")
            canvas.configure(scrollregion=(0, 0, 280, 1800))
            bind_mousewheel(canvas, canvas)
            root.update()

            start = canvas.yview()[0]
            canvas.event_generate("<MouseWheel>", delta=120)
            root.update()
            after_forward = canvas.yview()[0]
            assert after_forward < start, (
                f"滚轮向前应向下滚动（分数减小）: {start} -> {after_forward}"
            )

            canvas.event_generate("<MouseWheel>", delta=-120)
            root.update()
            after_back = canvas.yview()[0]
            assert after_back > after_forward, (
                f"滚轮向后应向上滚动（分数增大）: {after_forward} -> {after_back}"
            )
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass
            import gc

            gc.collect()


class TestMousewheelBinding:
    def test_download_view_has_wheel_binding(self) -> None:
        """修复「下载列表无法用滚轮滚动」：必须真的绑上了事件。"""
        if not TK_AVAILABLE:
            return
        from bilidown.ui.download_view import DownloadView

        with tk_root() as root:
            view = DownloadView(
                root,
                on_cancel=lambda t: None,
                on_retry=lambda t: None,
                on_remove=lambda t: None,
                on_clear_finished=lambda: None,
                on_open_folder=lambda: None,
                on_open_file=lambda t: None,
            )
            view.pack()
            root.update()
            bindings = view.tree.bind()
            assert any("MouseWheel" in b for b in bindings), f"tree 未绑定滚轮: {bindings}"
            assert any("Button-4" in b for b in bindings) or any(
                "Button-5" in b for b in bindings
            ), "缺少 Linux 滚轮绑定"
            view.destroy()

    def test_download_view_unbinds_on_destroy(self) -> None:
        if not TK_AVAILABLE:
            return
        from bilidown.ui.download_view import DownloadView

        with tk_root() as root:
            view = DownloadView(
                root,
                on_cancel=lambda t: None,
                on_retry=lambda t: None,
                on_remove=lambda t: None,
                on_clear_finished=lambda: None,
                on_open_folder=lambda: None,
                on_open_file=lambda t: None,
            )
            view.pack()
            root.update()
            view.destroy()  # 不应抛异常

    def test_search_view_cards_are_bound(self) -> None:
        """鼠标停在结果卡片（子控件）上也要能滚动 —— Tk 事件不冒泡。"""
        if not TK_AVAILABLE:
            return
        from bilidown.core.models import VideoItem
        from bilidown.ui.search_view import SearchView

        with tk_root() as root:
            view = SearchView(
                root,
                on_search=lambda k, p: None,
                on_copy=lambda v: None,
                on_download=lambda v, k: None,
                on_batch=lambda vs, k: None,
            )
            view.pack()
            view.render(
                [VideoItem(bvid="BV1xx411c7mD", cid=0, title="标题", author="UP")],
                "kw",
                1,
            )
            root.update()

            cards = list(view.results_frame.winfo_children())
            assert cards, "应渲染出结果卡片"
            card = cards[0]
            assert any("MouseWheel" in b for b in card.bind()), (
                f"结果卡片未绑定滚轮: {card.bind()}"
            )
            # 卡片内部的标题 Label 等子控件也必须绑定（事件不冒泡）
            nested_bound = any(
                any("MouseWheel" in b for b in grand.bind())
                for child in card.winfo_children()
                for grand in child.winfo_children()
            )
            assert nested_bound, "卡片的子控件未绑定滚轮"
            view.destroy()
