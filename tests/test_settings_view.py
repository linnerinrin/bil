"""设置视图与导航按钮测试。

无显示环境自动跳过 Tk 相关用例；不触网、不弹真实对话框（对话框被替换）。
"""

from __future__ import annotations

import gc
import pathlib
import tempfile

import tkinter as tk

from bilidown.core.preferences import (
    CONCURRENT_RANGE,
    RETRIES_RANGE,
    Preferences,
)

from .helpers import TK_AVAILABLE, tk_root


def _prefs() -> Preferences:
    return Preferences()


class TestNavigationHasSettingsButton:
    """用户要求：在「我的」与「下载列表」之间新增「设置」按钮。"""

    def test_button_order(self) -> None:
        if not TK_AVAILABLE:
            return
        from bilidown.ui.window import MainWindow

        with tk_root() as _root:
            window = MainWindow()
            window.root.withdraw()
            try:
                keys = list(window._nav_buttons)  # noqa: SLF001
                assert keys == ["login", "search", "profile", "settings", "downloads"], keys
                assert window._nav_buttons["settings"].cget("text") == "设置"  # noqa: SLF001
            finally:
                window.root.destroy()
                gc.collect()

    def test_settings_button_style_matches_others(self) -> None:
        """「设置」按钮必须与其他按钮同款（同样的底色与字体）。"""
        if not TK_AVAILABLE:
            return
        from bilidown.ui.window import MainWindow

        with tk_root() as _root:
            window = MainWindow()
            window.root.withdraw()
            try:
                buttons = window._nav_buttons  # noqa: SLF001
                reference = buttons["profile"]
                candidate = buttons["settings"]
                assert candidate.cget("bg") == reference.cget("bg")
                assert candidate.cget("font") == reference.cget("font")
                assert candidate.cget("cursor") == reference.cget("cursor")
                assert candidate.cget("relief") == reference.cget("relief")
            finally:
                window.root.destroy()
                gc.collect()

    def test_navigate_emits_settings_key(self) -> None:
        if not TK_AVAILABLE:
            return
        from bilidown.ui.window import MainWindow

        with tk_root() as _root:
            window = MainWindow()
            window.root.withdraw()
            received: list[str] = []
            window.on_navigate(received.append)
            try:
                window._nav_buttons["settings"].invoke()  # noqa: SLF001
                assert received == ["settings"]
            finally:
                window.root.destroy()
                gc.collect()


class TestBackgroundSwitching:
    def test_default_background_is_used(self) -> None:
        if not TK_AVAILABLE:
            return
        from bilidown.ui.window import MainWindow

        with tk_root() as _root:
            window = MainWindow()
            window.root.withdraw()
            try:
                # 内置背景存在，应成功渲染
                assert window._bg_photo is not None  # noqa: SLF001
            finally:
                window.root.destroy()
                gc.collect()

    def test_set_background_with_invalid_path_falls_back(self) -> None:
        """自定义背景不存在时必须安全回退，不能抛异常。"""
        if not TK_AVAILABLE:
            return
        from bilidown.ui.window import MainWindow

        with tk_root() as _root:
            window = MainWindow()
            window.root.withdraw()
            try:
                result = window.set_background("Z:/nope/missing.jpg")
                assert result is False, "坏路径不应报告成功"
                # 回退后仍应有可用背景（内置图）
                assert window._bg_fallback is not None or window._bg_photo is not None  # noqa: SLF001
            finally:
                window.root.destroy()
                gc.collect()

    def test_set_background_with_real_image(self) -> None:
        if not TK_AVAILABLE:
            return
        from PIL import Image

        from bilidown.ui.window import MainWindow

        tmp = pathlib.Path(tempfile.mkdtemp(prefix="bilidown-bg-"))
        try:
            # 造一张尺寸与窗口不同的图，验证等比填充不会报错
            image_path = tmp / "custom.png"
            Image.new("RGB", (640, 300), (10, 120, 200)).save(image_path)

            with tk_root() as _root:
                window = MainWindow()
                window.root.withdraw()
                try:
                    assert window.set_background(str(image_path)) is True
                    assert window._background_path == str(image_path)  # noqa: SLF001
                    # 恢复内置
                    assert window.set_background("") is True
                finally:
                    window.root.destroy()
                    gc.collect()
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class TestSettingsView:
    def _view(self, root, prefs: Preferences | None = None, applied: list | None = None):
        from bilidown.ui.settings_view import SettingsView

        collected = applied if applied is not None else []
        view = SettingsView(
            root,
            prefs or _prefs(),
            on_apply=collected.append,
            on_restore_defaults=lambda: collected.append("RESTORE"),
        )
        view.pack()
        return view, collected

    def test_builds_and_shows_current_values(self) -> None:
        if not TK_AVAILABLE:
            return
        with tk_root() as root:
            prefs = Preferences(max_concurrent=3, download_retries=4, open_folder_when_done=True)
            view, _ = self._view(root, prefs)
            try:
                assert view.concurrent_var.get() == 3
                assert view.retries_var.get() == 4
                assert view.open_folder_var.get() is True
                assert view.dir_var.get()  # 默认目录已回填
            finally:
                view.destroy()

    def test_apply_collects_changes(self) -> None:
        if not TK_AVAILABLE:
            return
        with tk_root() as root:
            view, collected = self._view(root)
            try:
                view.concurrent_var.set(5)
                view.retries_var.set(1)
                view.open_folder_var.set(True)
                view.dir_var.set("D:/my-downloads")
                view._handle_apply()  # noqa: SLF001

                assert len(collected) == 1
                draft = collected[0]
                assert isinstance(draft, Preferences)
                assert draft.max_concurrent == 5
                assert draft.download_retries == 1
                assert draft.open_folder_when_done is True
                assert draft.download_dir == "D:/my-downloads"
            finally:
                view.destroy()

    def test_apply_clamps_out_of_range_values(self) -> None:
        """界面传入越界值也必须被夹紧（不能把 99 并发写进配置）。"""
        if not TK_AVAILABLE:
            return
        with tk_root() as root:
            view, collected = self._view(root)
            try:
                view.concurrent_var.set(99)
                view.retries_var.set(0)
                view._handle_apply()  # noqa: SLF001
                draft = collected[0]
                assert draft.max_concurrent == CONCURRENT_RANGE[1]
                assert draft.download_retries == RETRIES_RANGE[0]
            finally:
                view.destroy()

    def test_default_directory_is_stored_as_empty(self) -> None:
        """填的就是默认目录时存空串，便于将来默认路径变化时自动跟随。"""
        if not TK_AVAILABLE:
            return
        from bilidown.config.paths import default_download_dir

        with tk_root() as root:
            view, collected = self._view(root)
            try:
                view.dir_var.set(str(default_download_dir()))
                view._handle_apply()  # noqa: SLF001
                assert collected[0].download_dir == ""
            finally:
                view.destroy()

    def test_invalid_background_is_rejected(self) -> None:
        """选择不支持的格式时应提示且不改变选择。"""
        if not TK_AVAILABLE:
            return
        from tkinter import filedialog

        with tk_root() as root:
            view, _ = self._view(root)
            original = filedialog.askopenfilename
            filedialog.askopenfilename = lambda **_kw: "C:/some/file.txt"
            try:
                view._choose_background()  # noqa: SLF001
                assert view._background == ""  # noqa: SLF001
                assert "不受支持" in view.hint_label.cget("text")
            finally:
                filedialog.askopenfilename = original
                view.destroy()

    def test_valid_background_sets_preview(self) -> None:
        if not TK_AVAILABLE:
            return
        from tkinter import filedialog

        from PIL import Image

        tmp = pathlib.Path(tempfile.mkdtemp(prefix="bilidown-set-"))
        try:
            image_path = tmp / "bg.png"
            Image.new("RGB", (800, 600), (200, 30, 60)).save(image_path)

            with tk_root() as root:
                view, _ = self._view(root)
                original = filedialog.askopenfilename
                filedialog.askopenfilename = lambda **_kw: str(image_path)
                try:
                    view._choose_background()  # noqa: SLF001
                    assert view._background == str(image_path)  # noqa: SLF001
                    assert view._thumb_photo is not None, "应生成缩略图"  # noqa: SLF001
                finally:
                    filedialog.askopenfilename = original
                    view.destroy()
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def test_corrupt_image_is_rejected(self) -> None:
        """后缀合法但内容损坏时也要拦住（避免应用后才崩）。"""
        if not TK_AVAILABLE:
            return
        from tkinter import filedialog, messagebox

        tmp = pathlib.Path(tempfile.mkdtemp(prefix="bilidown-bad-"))
        try:
            bad = tmp / "broken.png"
            bad.write_bytes(b"this is not an image")

            with tk_root() as root:
                view, _ = self._view(root)
                original_open = filedialog.askopenfilename
                original_error = messagebox.showerror
                shown: list[tuple] = []
                filedialog.askopenfilename = lambda **_kw: str(bad)
                messagebox.showerror = lambda *a, **k: shown.append(a)
                try:
                    view._choose_background()  # noqa: SLF001
                    assert view._background == ""  # noqa: SLF001
                    assert shown, "坏图应弹出错误提示"
                finally:
                    filedialog.askopenfilename = original_open
                    messagebox.showerror = original_error
                    view.destroy()
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def test_clear_background_restores_builtin(self) -> None:
        if not TK_AVAILABLE:
            return
        from bilidown.config.paths import is_builtin_ref

        with tk_root() as root:
            view, collected = self._view(root)
            try:
                view._background = "D:/x.png"  # noqa: SLF001
                view._clear_background()  # noqa: SLF001
                # 新语义：切回「默认内置背景」引用，而不是空串
                assert is_builtin_ref(view._background)  # noqa: SLF001
                assert "内置" in view.bg_path_label.cget("text")
                view._handle_apply()  # noqa: SLF001
                assert is_builtin_ref(collected[0].background_path)
            finally:
                view.destroy()

    def test_reset_directory_button(self) -> None:
        if not TK_AVAILABLE:
            return
        from bilidown.config.paths import default_download_dir

        with tk_root() as root:
            view, _ = self._view(root)
            try:
                view.dir_var.set("D:/somewhere-else")
                view._reset_directory()  # noqa: SLF001
                assert view.dir_var.get() == str(default_download_dir())
            finally:
                view.destroy()

    def test_show_saved_reflects_controller_state(self) -> None:
        if not TK_AVAILABLE:
            return
        with tk_root() as root:
            view, _ = self._view(root)
            try:
                saved = Preferences(
                    download_dir="E:/final",
                    max_concurrent=4,
                    download_retries=2,
                    open_folder_when_done=True,
                )
                view.show_saved(saved, "设置已保存")
                assert view.concurrent_var.get() == 4
                assert view.retries_var.get() == 2
                assert view.open_folder_var.get() is True
                assert view.dir_var.get() == "E:/final"
                assert "已保存" in view.hint_label.cget("text")
            finally:
                view.destroy()

    def test_restore_asks_for_confirmation(self) -> None:
        if not TK_AVAILABLE:
            return
        from tkinter import messagebox

        with tk_root() as root:
            view, collected = self._view(root)
            original = messagebox.askyesno
            messagebox.askyesno = lambda *_a, **_k: False  # 用户取消
            try:
                view._handle_restore()  # noqa: SLF001
                assert collected == [], "取消时不应触发恢复"
                messagebox.askyesno = lambda *_a, **_k: True
                view._handle_restore()  # noqa: SLF001
                assert collected == ["RESTORE"]
            finally:
                messagebox.askyesno = original
                view.destroy()

    def test_destroy_is_safe(self) -> None:
        if not TK_AVAILABLE:
            return
        with tk_root() as root:
            view, _ = self._view(root)
            view.destroy()
            view.destroy()  # 幂等，不应抛异常


class TestBuiltinBackgroundSelection:
    """用户要求：内置背景以「文件名列表」呈现，并且自身可预览。"""

    def _view(self, root, prefs=None):
        from bilidown.ui.settings_view import SettingsView

        collected: list = []
        view = SettingsView(
            root,
            prefs or _prefs(),
            on_apply=collected.append,
            on_restore_defaults=lambda: collected.append("RESTORE"),
        )
        view.pack()
        root.update()
        return view, collected

    def test_builtin_names_listed_with_filename(self) -> None:
        if not TK_AVAILABLE:
            return
        from bilidown.config.paths import list_builtin_backgrounds

        with tk_root() as root:
            view, _ = self._view(root)
            try:
                names = list_builtin_backgrounds()
                assert names, "应至少有一个内置背景资源"
                shown = list(view.builtin_list.get(0, "end"))
                assert shown == names, f"列表应显示文件名: {shown} vs {names}"
                # 显示的就是文件名本身
                assert all(name.endswith((".jpg", ".png", ".jpeg", ".bmp", ".webp"))
                           for name in shown)
            finally:
                view.destroy()

    def test_default_selection_is_the_active_builtin(self) -> None:
        if not TK_AVAILABLE:
            return
        from bilidown.config.paths import default_builtin_background

        with tk_root() as root:
            view, _ = self._view(root)
            try:
                selection = view.builtin_list.curselection()
                assert selection, "应默认选中一个内置背景"
                assert view.builtin_list.get(selection[0]) == default_builtin_background()
            finally:
                view.destroy()

    def test_selecting_builtin_sets_ref_and_preview(self) -> None:
        if not TK_AVAILABLE:
            return
        from bilidown.config.paths import is_builtin_ref, parse_builtin_ref

        with tk_root() as root:
            view, collected = self._view(root)
            try:
                # 先切到自定义背景，再回到内置
                view._background = "D:/custom.png"  # noqa: SLF001
                view.builtin_list.selection_clear(0, tk.END)
                view.builtin_list.selection_set(0)
                view._on_builtin_selected()  # noqa: SLF001

                assert is_builtin_ref(view._background)  # noqa: SLF001
                assert parse_builtin_ref(view._background)  # noqa: SLF001
                # 预览必须真的生成了缩略图（内置背景自身可预览）
                assert view._thumb_photo is not None, "内置背景应能预览"  # noqa: SLF001
                assert "内置背景" in view.bg_path_label.cget("text")

                view._handle_apply()  # noqa: SLF001
                assert is_builtin_ref(collected[0].background_path)
            finally:
                view.destroy()

    def test_use_selected_button(self) -> None:
        if not TK_AVAILABLE:
            return
        from bilidown.config.paths import is_builtin_ref

        with tk_root() as root:
            view, _ = self._view(root)
            try:
                view._background = "D:/custom.png"  # noqa: SLF001
                view.builtin_list.selection_clear(0, tk.END)
                view.builtin_list.selection_set(0)
                view._use_selected_builtin()  # noqa: SLF001
                assert is_builtin_ref(view._background)  # noqa: SLF001
            finally:
                view.destroy()

    def test_custom_background_clears_list_selection(self) -> None:
        if not TK_AVAILABLE:
            return
        with tk_root() as root:
            view, _ = self._view(root)
            try:
                view._background = "D:/custom.png"  # noqa: SLF001
                view._select_builtin_in_list()  # noqa: SLF001
                assert not view.builtin_list.curselection(), (
                    "用自定义背景时列表不应选中任何内置项"
                )
            finally:
                view.destroy()

    def test_clear_background_returns_to_default_builtin(self) -> None:
        if not TK_AVAILABLE:
            return
        from bilidown.config.paths import (
            default_builtin_background,
            parse_builtin_ref,
        )

        with tk_root() as root:
            view, collected = self._view(root)
            try:
                view._background = "D:/custom.png"  # noqa: SLF001
                view._clear_background()  # noqa: SLF001
                assert parse_builtin_ref(view._background) == default_builtin_background()  # noqa: SLF001
                assert view.builtin_list.curselection(), "应重新选中内置背景"
                view._handle_apply()  # noqa: SLF001
                assert collected[0].background_path.startswith("builtin:")
            finally:
                view.destroy()

    def test_preview_label_size_is_stable(self) -> None:
        """回归：显示过图片再切回内置背景，预览区不能变得巨大。

        上一版的 bug 是在显示缩略图时设置了 width/height（像素），
        切回内置背景时没有复位，于是标签被撑得非常大。
        """
        if not TK_AVAILABLE:
            return
        from PIL import Image

        tmp = pathlib.Path(tempfile.mkdtemp(prefix="bilidown-size-"))
        try:
            image_path = tmp / "big.png"
            Image.new("RGB", (1600, 900), (200, 60, 60)).save(image_path)

            with tk_root() as root:
                view, _ = self._view(root)
                try:
                    base_w = int(view.preview_label.cget("width"))
                    base_h = int(view.preview_label.cget("height"))
                    assert base_w > 1 and base_h > 1, "预览区应有固定尺寸"

                    # 1) 切到自定义图片（会生成缩略图）
                    view._background = str(image_path)  # noqa: SLF001
                    view._refresh_background_preview()  # noqa: SLF001
                    assert view._thumb_photo is not None  # noqa: SLF001
                    assert int(view.preview_label.cget("width")) == base_w
                    assert int(view.preview_label.cget("height")) == base_h

                    # 2) 切回内置背景 —— 关键断言：尺寸不变
                    view._clear_background()  # noqa: SLF001
                    assert int(view.preview_label.cget("width")) == base_w, (
                        "切回内置背景后预览宽度变了（会显示成巨大）"
                    )
                    assert int(view.preview_label.cget("height")) == base_h, (
                        "切回内置背景后预览高度变了（会显示成巨大）"
                    )
                    assert view._thumb_photo is not None, "内置背景也应有预览图"  # noqa: SLF001
                finally:
                    view.destroy()
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def test_builtin_preview_shows_thumbnail_not_placeholder(self) -> None:
        """内置背景也必须显示缩略图，而不是一行文字占位。"""
        if not TK_AVAILABLE:
            return
        with tk_root() as root:
            view, _ = self._view(root)
            try:
                view._background = ""  # noqa: SLF001 - 空值即默认内置背景
                view._refresh_background_preview()  # noqa: SLF001
                assert view._thumb_photo is not None, "内置背景应显示缩略图"  # noqa: SLF001
                assert view.preview_label.cget("text") == ""
            finally:
                view.destroy()


class TestSettingsWheelScrolling:
    """回归：设置页曾经完全无法用滚轮滚动。

    根因是滚轮在**内容创建之前**绑定，而 bind_mousewheel 递归的是
    「当前已存在的后代」，于是后建的卡片收不到事件。Tk 事件不冒泡，
    鼠标停在卡片或其内部控件上时就完全滚不动。
    """

    def _view(self, root):
        from bilidown.ui.settings_view import SettingsView

        view = SettingsView(
            root,
            _prefs(),
            on_apply=lambda _p: None,
            on_restore_defaults=lambda: None,
        )
        view.pack()
        root.update()
        return view

    def _has_wheel(self, widget) -> bool:
        try:
            bindings = widget.bind()
        except Exception:  # noqa: BLE001
            return False
        return any(("MouseWheel" in str(b)) or ("Button-4" in str(b)) for b in bindings)

    def test_canvas_is_bound(self) -> None:
        if not TK_AVAILABLE:
            return
        with tk_root() as root:
            view = self._view(root)
            try:
                assert self._has_wheel(view._scroll_canvas), "canvas 未绑定滚轮"  # noqa: SLF001
            finally:
                view.destroy()

    def test_cards_are_bound(self) -> None:
        """核心回归：三张设置卡片必须各自绑定滚轮。"""
        if not TK_AVAILABLE:
            return
        with tk_root() as root:
            view = self._view(root)
            try:
                cards = [
                    w
                    for w in view._scroll_inner.winfo_children()  # noqa: SLF001
                    if w.winfo_class() == "Frame"
                ]
                assert len(cards) >= 3, f"应有至少 3 张设置卡片，实际 {len(cards)}"
                unbound = [i for i, c in enumerate(cards) if not self._has_wheel(c)]
                assert not unbound, f"第 {unbound} 张卡片未绑定滚轮"
            finally:
                view.destroy()

    def test_nested_widgets_inside_cards_are_bound(self) -> None:
        """鼠标停在卡片**内部**的控件上（标签/输入框/按钮）也必须能滚。"""
        if not TK_AVAILABLE:
            return
        with tk_root() as root:
            view = self._view(root)
            try:
                def walk(widget):
                    yield widget
                    for child in widget.winfo_children():
                        yield from walk(child)

                unbound: list[str] = []
                for card in view._scroll_inner.winfo_children():  # noqa: SLF001
                    for node in walk(card):
                        # 跳过滚动条：它有自己的拖动行为，不需要也不应绑滚轮
                        if "scrollbar" in node.winfo_class().lower():
                            continue
                        if not self._has_wheel(node):
                            unbound.append(f"{node.winfo_class()}")
                assert not unbound, f"卡片内未绑定滚轮的控件: {unbound}"
            finally:
                view.destroy()

    def test_wheel_event_on_card_dispatches_scroll(self) -> None:
        """端到端：把滚轮事件发给卡片（及其内部控件），必须**恰好**触发一次滚动。

        这里拦截 canvas.yview_scroll 来断言「事件确实被处理并派发」，
        而不是比较 yview 数值——撤回的窗口没有真实布局高度，canvas 滚不动，
        那样测的是布局而不是绑定。

        同时断言**次数为 1**：曾经因为卡片被绑定两次，实测滚动 3 次而空白区
        只 1 次，导致滚轮速度忽快忽慢。
        """
        if not TK_AVAILABLE:
            return
        with tk_root() as root:
            view = self._view(root)
            try:
                canvas = view._scroll_canvas  # noqa: SLF001
                calls: list[tuple] = []
                original = canvas.yview_scroll

                def spy(*args):
                    calls.append(args)
                    return original(*args)

                canvas.yview_scroll = spy  # type: ignore[method-assign]

                def fire(widget, label: str) -> int:
                    before = len(calls)
                    # 真实 Windows 滚轮一格是 delta=120（units 由它换算）
                    widget.event_generate("<MouseWheel>", delta=120)
                    root.update()
                    count = len(calls) - before
                    assert count == 1, f"{label} 触发了 {count} 次滚动（应为 1 次）"
                    return count

                cards = [
                    w
                    for w in view._scroll_inner.winfo_children()  # noqa: SLF001
                    if w.winfo_class() == "Frame"
                ]
                assert cards, "应有设置卡片"

                # 1) 卡片本身
                fire(cards[0], "卡片")

                # 2) 卡片内部控件（标签/输入框等），这才是最常见的落点
                inner_widgets = [
                    node
                    for child in cards[0].winfo_children()
                    for node in child.winfo_children()
                ]
                assert inner_widgets, "卡片内部应有控件"
                for node in inner_widgets[:4]:
                    fire(node, f"卡片内 {node.winfo_class()}")

                # 方向也必须正确：向前滚 -> yview_scroll 收到负的 units
                units = [c[0] for c in calls if c]
                assert all(u == -1 for u in units), f"滚动方向/幅度不对: {units}"
            finally:
                view.destroy()

    def test_no_duplicate_binding_accumulation(self) -> None:
        """反复进出设置页不应叠加滚轮绑定（否则滚动速度会越来越快）。"""
        if not TK_AVAILABLE:
            return
        with tk_root() as root:
            counts: list[int] = []
            for _ in range(3):
                view = self._view(root)
                bindings = view._scroll_canvas.bind()  # noqa: SLF001
                counts.append(len(bindings))
                view.destroy()
            assert len(set(counts)) == 1, f"多次打开后绑定数量不一致: {counts}"
