"""界面层测试：视图构建、销毁守卫、刷新停止、状态渲染。

无显示环境下自动跳过（不触网、不弹窗）。

关于 `gc.collect()`：Tk 对象的 C 级析构如果在非主线程触发，
解释器会以 ``Tcl_AsyncDelete: async handler deleted by the wrong thread``
直接崩溃（Windows 上是 0x80000003，Python 层抓不到）。
本文件会频繁创建/销毁 Tk root，因此每次销毁后都在**主线程**强制回收，
把析构时机变确定，消除这个非确定性的测试装置竞态。
"""

from __future__ import annotations

import gc

import tkinter as tk

from bilidown.core.models import DownloadKind, DownloadStatus, DownloadTask, VideoItem

_TK_AVAILABLE = True
try:
    _probe = tk.Tk()
    _probe.withdraw()
    _probe.destroy()
    del _probe
    gc.collect()
except Exception:  # noqa: BLE001
    _TK_AVAILABLE = False


def _item(index: int = 0) -> VideoItem:
    return VideoItem(
        bvid=f"BV1xx411c7m{index}",
        cid=0,
        title=f"测试视频 {index}",
        author="UP主",
        play=100 + index,
        danmaku=7,
        duration=125,
    )


class _Rig:
    """创建一个可用后销毁的 Tk root。"""

    def __enter__(self) -> tk.Tk:
        self.root = tk.Tk()
        self.root.withdraw()
        return self.root

    def __exit__(self, *_exc: object) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        # 在主线程确定性回收 Tk 对象，避免 C 级析构落到工作线程上
        del self.root
        gc.collect()


class TestMainWindow:
    def test_builds_and_exposes_label(self) -> None:
        """B5 回归：顶栏必须是可设置 text 的 Label，而不是 Frame。"""
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.window import MainWindow

        with _Rig() as _root:
            window = MainWindow()
            window.root.withdraw()
            window.set_user_text("欢迎，测试")
            assert window.userinfo_label.cget("text") == "欢迎，测试"
            # 该控件必须真的是 Label
            assert window.userinfo_label.winfo_class() == "Label"
            window.root.destroy()

    def test_clear_content_invokes_destroy_hook(self) -> None:
        """B6 回归：清空内容前必须调用视图的销毁钩子。"""
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.window import MainWindow

        with _Rig() as _root:
            window = MainWindow()
            window.root.withdraw()
            called: list[str] = []
            child = tk.Frame(window.content_frame)
            child._on_destroy_hook = lambda: called.append("hook")  # type: ignore[attr-defined]
            child.pack()
            window.clear_content()
            assert called == ["hook"]
            window.root.destroy()

    def test_background_fallback_never_raises(self) -> None:
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.window import load_background

        image = load_background((100, 100))
        # 资源存在时必须能加载；即便失败也只返回 None，不允许抛异常
        assert image is None or image.size == (100, 100)


class TestSearchView:
    def test_render_results(self) -> None:
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.search_view import SearchView

        with _Rig() as root:
            calls: list[tuple] = []
            view = SearchView(
                root,
                on_search=lambda k, p: calls.append((k, p)),
                on_copy=lambda v: calls.append(("copy", v)),
                on_download=lambda v, k: calls.append(("dl", v, k)),
                on_batch=lambda vs, k: calls.append(("batch", len(vs), k)),
            )
            view.pack()
            view.render([_item(0), _item(1)], "测试", 2)
            assert len(view.items) == 2
            assert view.page == 2
            assert "2 条结果" in view.tip_label.cget("text")
            view.destroy()

    def test_empty_results_shows_hint(self) -> None:
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.search_view import SearchView

        with _Rig() as root:
            view = SearchView(
                root,
                on_search=lambda k, p: None,
                on_copy=lambda v: None,
                on_download=lambda v, k: None,
                on_batch=lambda vs, k: None,
            )
            view.pack()
            view.render([], "空", 1)
            assert "未找到" in view.tip_label.cget("text")
            view.destroy()

    def test_loading_toggles_button_state(self) -> None:
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.search_view import SearchView

        with _Rig() as root:
            view = SearchView(
                root,
                on_search=lambda k, p: None,
                on_copy=lambda v: None,
                on_download=lambda v, k: None,
                on_batch=lambda vs, k: None,
            )
            view.pack()
            view.set_loading(True)
            assert str(view.search_btn.cget("state")) == "disabled"
            view.set_loading(False)
            assert str(view.search_btn.cget("state")) == "normal"
            view.destroy()


class TestDownloadView:
    def test_incremental_refresh_updates_in_place(self) -> None:
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.download_view import DownloadView

        with _Rig() as root:
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
            task = DownloadTask(item=_item(), kind=DownloadKind.VIDEO)
            view.refresh([task])
            first = view.tree.item(task.task_id, "values")
            assert first[3] == "0%"

            task.set_progress(55.0)
            task.set_status(DownloadStatus.DOWNLOADING)
            view.refresh([task])
            second = view.tree.item(task.task_id, "values")
            assert second[3] == "55%"
            assert second[4] == DownloadStatus.DOWNLOADING.value
            # 不能重建行：同一个 iid 必须仍是同一行
            assert view.tree.exists(task.task_id)
            assert len(view.tree.get_children()) == 1
            view.destroy()

    def test_removed_task_disappears(self) -> None:
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.download_view import DownloadView

        with _Rig() as root:
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
            task = DownloadTask(item=_item())
            view.refresh([task])
            assert len(view.tree.get_children()) == 1
            view.refresh([])
            assert len(view.tree.get_children()) == 0
            assert "暂无任务" in view.summary_label.cget("text")
            view.destroy()

    def test_summary_counts(self) -> None:
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.download_view import DownloadView

        with _Rig() as root:
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
            done = DownloadTask(item=_item(0))
            done.set_status(DownloadStatus.COMPLETED)
            failed = DownloadTask(item=_item(1))
            failed.fail("x")
            view.refresh([done, failed])
            text = view.summary_label.cget("text")
            assert "完成 1" in text
            assert "失败 1" in text
            view.destroy()


class TestLoginView:
    def test_renders_and_updates_status(self) -> None:
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.login_view import LoginView

        with _Rig() as root:
            view = LoginView(root, on_refresh=lambda: None)
            view.pack()
            view.set_status("请扫码")
            assert view.status_label.cget("text") == "请扫码"
            view.set_refreshing(True)
            assert str(view.refresh_btn.cget("state")) == "disabled"
            view.destroy()

    def test_expired_state_is_visible(self) -> None:
        """B3 回归：二维码过期必须有明确界面反馈。"""
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.login_view import LoginView

        with _Rig() as root:
            view = LoginView(root, on_refresh=lambda: None)
            view.pack()
            view.show_qrcode_expired()
            assert "过期" in view.qrcode_text
            assert "过期" in view.status_label.cget("text")
            view.destroy()


class TestProfileView:
    def test_none_user_shows_login_prompt(self) -> None:
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.profile_view import ProfileView

        with _Rig() as root:
            view = ProfileView(root, None)
            view.pack()
            texts = [w.cget("text") for w in view.winfo_children() if isinstance(w, tk.Label)]
            assert any("尚未登录" in t for t in texts)
            view.destroy()

    def test_partial_user_info_does_not_raise(self) -> None:
        """旧 profile.py 直接下标取值，缺字段会 KeyError 导致白屏。"""
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.profile_view import ProfileView

        with _Rig() as root:
            view = ProfileView(root, {"mid": 1, "name": "只有名字"})
            view.pack()
            view.update()
            view.destroy()

    def test_full_user_info_renders(self) -> None:
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.profile_view import ProfileView

        info = {
            "mid": 42,
            "name": "测试用户",
            "sex": "男",
            "coins": 10,
            "following": 20,
            "follower": 30,
            "level": 6,
            "sign": "签名",
        }
        with _Rig() as root:
            view = ProfileView(root, info)
            view.pack()
            view.update()
            view.set_avatar(None)  # 头像失败不应抛异常
            view.destroy()
