"""控制器状态机测试：扫码四态路由、登录/登出、线程编组守卫、搜索失败路径。

不触网，且**完全不碰 tkinter**：窗口用纯 Python 替身，
``after`` 直接在调用线程同步执行回调（因此工作线程调用它也安全）。
需要真实控件的用例各自创建自己的 Tk root（见 helpers.tk_root）并确定性回收。
"""

from __future__ import annotations

from bilidown.core.models import DownloadKind, DownloadTask, VideoItem
from bilidown.core.qrlogin import QrState

from .helpers import TK_AVAILABLE, tk_root

_TK_AVAILABLE = TK_AVAILABLE


class FakeClient:
    """记录调用、不发网络请求的客户端替身。"""

    def __init__(self, *, logged_in: bool = False) -> None:
        self.logged_in = logged_in
        self.credential = object() if logged_in else None
        self.qr_started = 0
        self.polled = 0
        self.logged_out = 0
        self.credential_taken = 0
        self.validate_result = True
        self.credential_error: Exception | None = None
        self.fetch_error: Exception | None = None
        self.user_info: dict[str, object] = {"mid": 1, "name": "测试用户", "face": ""}
        self.search_calls: list[tuple[str, int]] = []
        self.by_bvid_calls: list[str] = []
        self.raise_on_search: Exception | None = None

    # ---- 登录 ----
    def login_qrcode_start(self) -> bytes:
        self.qr_started += 1
        return b"\x89PNG\r\n\x1a\nfake"

    def login_qrcode_poll(self) -> QrState:
        self.polled += 1
        return QrState.SCAN

    def login_qrcode_credential(self) -> object:
        if self.credential_error is not None:
            raise self.credential_error
        self.credential_taken += 1
        self.logged_in = True
        self.credential = object()
        return self.credential

    def validate_credential(self) -> bool:
        return self.validate_result

    def restore_credential(self) -> bool:
        return self.logged_in

    def save_credential(self) -> None:
        pass

    def logout(self) -> None:
        self.logged_out += 1
        self.logged_in = False
        self.credential = None

    def fetch_self_info(self) -> dict[str, object]:
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.user_info

    # ---- 搜索 ----
    def search_videos(self, keyword: str, page: int = 1):
        self.search_calls.append((keyword, page))
        if self.raise_on_search is not None:
            raise self.raise_on_search
        items = [VideoItem(bvid="BV1xx411c7mD", cid=0, title="搜索结果", author="UP")]

        class _Result:
            pass

        _Result.items = items  # type: ignore[attr-defined]
        _Result.keyword = keyword  # type: ignore[attr-defined]
        return _Result()

    def fetch_by_bvid(self, bvid: str) -> VideoItem:
        self.by_bvid_calls.append(bvid)
        return VideoItem(bvid=bvid, cid=123, title="BV 直查", author="UP")


class FakeTasks:
    """任务管理器替身。"""

    def __init__(self) -> None:
        self.submitted: list[DownloadTask] = []
        self.cancelled: list[DownloadTask] = []
        self.retried: list[DownloadTask] = []
        self.removed: list[DownloadTask] = []
        self.cleared = 0
        self.download_dir = "X:\\fake"

    def add_listener(self, _cb) -> None:
        pass

    def submit(self, task: DownloadTask) -> DownloadTask:
        self.submitted.append(task)
        return task

    def tasks(self) -> list[DownloadTask]:
        return list(self.submitted)

    def cancel(self, task: DownloadTask) -> None:
        self.cancelled.append(task)

    def retry(self, task: DownloadTask) -> DownloadTask:
        self.retried.append(task)
        return task

    def remove(self, task: DownloadTask) -> None:
        self.removed.append(task)

    def clear_finished(self) -> int:
        self.cleared += 1
        return 3

    def active_count(self) -> int:
        return len(self.submitted)


class FakeWindow:
    """主窗口替身：纯 Python，不创建任何 Tk 对象。

    ``after`` 在**调用线程**同步执行回调 —— 这既模拟了「回调最终会执行」，
    又避免了在工作线程里触碰 tkinter（那样会导致 Tcl_AsyncDelete 崩溃）。
    """

    class _Root:
        def after(self, _ms: int, callback) -> str:
            callback()
            return "after#fake"

        def after_cancel(self, _id: str) -> None:
            pass

        def protocol(self, *_a, **_k) -> None:
            pass

    def __init__(self) -> None:
        self.root = self._Root()
        self.content_frame = None  # 需要真实控件的用例会自建 Tk root
        self.user_texts: list[str] = []
        self.statuses: list[str] = []
        self.nav_calls: list[str] = []
        self._navigate = None
        self._logout = None

    def on_navigate(self, cb) -> None:
        self._navigate = cb

    def on_logout(self, cb) -> None:
        self._logout = cb

    def set_user_text(self, text: str) -> None:
        self.user_texts.append(text)

    def set_logged_in(self, _flag: bool) -> None:
        pass

    def set_status(self, text: str) -> None:
        self.statuses.append(text)

    def clear_content(self) -> None:
        pass

    def after(self, ms: int, cb) -> str:
        return self.root.after(ms, cb)

    def cancel_after(self, _id) -> None:
        pass


def _bare_app(*, client=None, tasks=None, window=None, view=None):
    """构造一个不走 __init__ 的控制器（避免真实网络与窗口）。"""
    from bilidown.app import BilidownApp

    app = BilidownApp.__new__(BilidownApp)
    app.client = client or FakeClient()
    app.tasks = tasks or FakeTasks()
    app.window = window or FakeWindow()
    app.download_dir = "X:\\fake"
    app.search_view = view
    app.login_view = view
    app.profile_view = view
    app.download_view = view
    app._current_view = "login"
    app._refresh_job = None
    app._epoch = 0
    app._qr_polling = False
    app._user_info = None
    app._workers = set()
    app._worker_lock = __import__("threading").RLock()
    app._shutting_down = False
    return app


class TestQrStateRouting:
    """B3 回归：四种扫码状态必须都被显式处理。"""

    def test_all_four_states_routed_in_source(self) -> None:
        import pathlib

        source = (
            pathlib.Path(__file__).resolve().parent.parent / "src" / "bilidown" / "app.py"
        ).read_text(encoding="utf-8")
        for state in ("SCAN", "CONF", "TIMEOUT", "DONE"):
            assert f"QrState.{state}" in source, f"app.py 未路由 {state} 状态"

    def test_scan_updates_status(self) -> None:
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.login_view import LoginView

        with tk_root() as root:
            view = LoginView(root, on_refresh=lambda: None)
            app = _bare_app(view=view)
            app._on_qr_state(QrState.SCAN, epoch=0)
            assert "已扫描" in view.status_label.cget("text")

    def test_conf_updates_status(self) -> None:
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.login_view import LoginView

        with tk_root() as root:
            view = LoginView(root, on_refresh=lambda: None)
            app = _bare_app(view=view)
            app._on_qr_state(QrState.CONF, epoch=0)
            assert "已确认" in view.status_label.cget("text")

    def test_timeout_marks_expired_and_stops_polling(self) -> None:
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.login_view import LoginView

        with tk_root() as root:
            view = LoginView(root, on_refresh=lambda: None)
            app = _bare_app(view=view)
            app._qr_polling = True
            app._on_qr_state(QrState.TIMEOUT, epoch=0)
            assert app._qr_polling is False, "过期后必须停止轮询"
            assert "过期" in view.qrcode_text
            assert "过期" in view.status_label.cget("text")

    def test_done_completes_login(self) -> None:
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.login_view import LoginView

        with tk_root() as root:
            view = LoginView(root, on_refresh=lambda: None)
            client = FakeClient()
            app = _bare_app(client=client, view=view)
            app._qr_polling = True
            app._on_qr_state(QrState.DONE, epoch=0)
            assert client.credential_taken == 1
            assert client.logged_in is True

    def test_credential_failure_shows_error_and_does_not_mark_logged_in(self) -> None:
        """真实缺陷回归：解析不出凭据时必须给出可读错误，而不是静默变成已登录。"""
        from bilidown.core.client import ClientError

        if not _TK_AVAILABLE:
            return
        from bilidown.ui.login_view import LoginView

        with tk_root() as root:
            view = LoginView(root, on_refresh=lambda: None)
            client = FakeClient()
            client.credential_error = ClientError("SESSDATA 缺失")
            app = _bare_app(client=client, view=view)
            import bilidown.app as app_mod

            shown: list[tuple] = []
            original = app_mod.show_error
            app_mod.show_error = lambda *a, **k: shown.append(a)
            try:
                app._on_qr_state(QrState.DONE, epoch=0)
            finally:
                app_mod.show_error = original
            assert shown, "凭据失败时必须弹出可读错误"
            assert client.logged_in is False

    def test_stale_epoch_is_ignored(self) -> None:
        if not _TK_AVAILABLE:
            return
        from bilidown.ui.login_view import LoginView

        with tk_root() as root:
            view = LoginView(root, on_refresh=lambda: None)
            app = _bare_app(view=view)
            app._epoch = 5
            before = view.status_label.cget("text")
            app._on_qr_state(QrState.TIMEOUT, epoch=1)  # 旧世代
            assert view.status_label.cget("text") == before


class TestThreadGroupGuard:
    def test_marshal_dropped_when_shutting_down(self) -> None:
        app = _bare_app()
        app._shutting_down = True
        called: list[str] = []
        app._marshal(lambda: called.append("x"))
        assert called == [], "退出后不得再调度回调"

    def test_spawn_refused_when_shutting_down(self) -> None:
        app = _bare_app()
        app._shutting_down = True
        ran: list[str] = []
        app._spawn(lambda: ran.append("x"), "test-worker")
        assert app._workers == set()
        assert ran == []

    def test_spawn_tracks_and_removes_worker(self) -> None:
        import time

        app = _bare_app()
        done = __import__("threading").Event()
        app._spawn(lambda: done.set(), "test-worker")
        assert done.wait(5)
        deadline = time.monotonic() + 3
        while app._workers and time.monotonic() < deadline:
            time.sleep(0.02)
        assert app._workers == set(), "工作线程结束后应从集合中摘除"


class TestLoginLogout:
    def test_credential_invalid_triggers_reauth(self) -> None:
        client = FakeClient(logged_in=True)
        app = _bare_app(client=client)
        app._on_credential_invalid()
        assert client.logged_out == 1
        assert app._user_info is None
        assert any("过期" in text for text in app.window.user_texts)

    def test_logout_resets_state(self) -> None:
        import bilidown.app as app_mod

        client = FakeClient(logged_in=True)
        app = _bare_app(client=client)
        app._user_info = {"name": "x"}
        original = app_mod.ask_yes_no
        app_mod.ask_yes_no = lambda *_a, **_k: True  # 自动确认
        try:
            app.handle_logout()
        finally:
            app_mod.ask_yes_no = original
        assert client.logged_out == 1
        assert app._user_info is None
        assert app._epoch == 1, "登出必须递增世代以作废在飞回调"

    def test_apply_user_info_updates_topbar(self) -> None:
        app = _bare_app()
        app._apply_user_info({"mid": 7, "name": "小明"}, epoch=0)
        assert any("小明" in text for text in app.window.user_texts)
        assert app._user_info == {"mid": 7, "name": "小明"}

    def test_apply_user_info_ignores_stale_epoch(self) -> None:
        app = _bare_app()
        app._epoch = 3
        app._apply_user_info({"name": "旧数据"}, epoch=1)
        assert app._user_info is None


class TestUserInfoFailureRecovery:
    """真实缺陷回归：资料接口报「凭据未提供 SESSDATA」时必须能自愈。

    旧行为：只在顶栏写「已登录（资料获取失败）」，既不校验也不清理凭据，
    应用就卡在「已登录但什么都取不到」的状态。
    """

    def test_invalid_credential_triggers_logout_and_reauth(self) -> None:
        client = FakeClient(logged_in=True)
        client.fetch_error = RuntimeError(
            "Credential 类未提供 sessdata 或者为空。"
        )
        client.validate_result = False  # 校验也不通过
        app = _bare_app(client=client)

        app._fetch_user_info(epoch=0)

        assert client.logged_out == 1, "凭据失效必须清除本地凭据"
        assert app._user_info is None
        assert any("过期" in text for text in app.window.user_texts)

    def test_valid_credential_keeps_session_and_reports_reason(self) -> None:
        client = FakeClient(logged_in=True)
        client.fetch_error = RuntimeError("网络抖动")
        client.validate_result = True
        app = _bare_app(client=client)

        shown: list[tuple] = []
        import bilidown.app as app_mod

        original = app_mod.show_error
        app_mod.show_error = lambda *a, **k: shown.append(a)
        try:
            app._fetch_user_info(epoch=0)
        finally:
            app_mod.show_error = original

        assert client.logged_out == 0, "凭据仍有效时不应登出"
        assert shown, "应弹出可读的失败原因"
        assert "网络抖动" in " ".join(str(x) for x in shown[0])

    def test_successful_fetch_applies_info(self) -> None:
        client = FakeClient(logged_in=True)
        app = _bare_app(client=client)
        app._fetch_user_info(epoch=0)
        assert app._user_info == client.user_info
        assert any("测试用户" in text for text in app.window.user_texts)


class TestSearchFlow:
    def test_bvid_input_uses_direct_lookup(self) -> None:
        import time

        client = FakeClient()
        app = _bare_app(client=client)
        app._on_search("BV1xx411c7mD", 1)
        deadline = time.monotonic() + 5
        while not client.by_bvid_calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert client.by_bvid_calls == ["BV1xx411c7mD"]
        assert client.search_calls == []

    def test_keyword_uses_search_api(self) -> None:
        import time

        client = FakeClient()
        app = _bare_app(client=client)
        app._on_search("python", 2)
        deadline = time.monotonic() + 5
        while not client.search_calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert client.search_calls == [("python", 2)]

    def test_search_failure_resets_loading_state(self) -> None:
        """回归：失败时必须把界面从「加载中」恢复，否则按钮永久禁用。"""
        from bilidown.core.client import ClientError

        if not _TK_AVAILABLE:
            return
        from bilidown.ui.search_view import SearchView

        with tk_root() as root:
            view = SearchView(
                root,
                on_search=lambda k, p: None,
                on_copy=lambda v: None,
                on_download=lambda v, k: None,
                on_batch=lambda vs, k: None,
            )
            app = _bare_app(view=view)
            view.set_loading(True)
            assert str(view.search_btn.cget("state")) == "disabled"

            app._on_search_failed(str(ClientError("搜索失败：网络错误")))
            assert str(view.search_btn.cget("state")) == "normal"
            assert "搜索失败" in view.tip_label.cget("text")


class TestDownloadFlow:
    def test_enqueue_without_confirm_submits_tasks(self) -> None:
        client = FakeClient()
        tasks = FakeTasks()
        app = _bare_app(client=client, tasks=tasks)
        items = [
            VideoItem(bvid=f"BV1xx411c7m{i}", cid=0, title=f"视频{i}") for i in range(3)
        ]
        app._enqueue(items, "video", confirm=False)
        assert len(tasks.submitted) == 3
        assert all(t.kind is DownloadKind.VIDEO for t in tasks.submitted)

    def test_enqueue_audio_kind(self) -> None:
        app = _bare_app()
        tasks = app.tasks
        app._enqueue([VideoItem(bvid="BV1xx411c7mD", cid=0, title="t")], "audio", confirm=False)
        assert tasks.submitted[0].kind is DownloadKind.AUDIO

    def test_enqueue_cancelled_by_user(self) -> None:
        import bilidown.app as app_mod

        app = _bare_app()
        tasks = app.tasks
        original = app_mod.ask_yes_no
        app_mod.ask_yes_no = lambda *_a, **_k: False
        try:
            app._enqueue([VideoItem(bvid="BV1xx411c7mD", cid=0, title="t")], "video", confirm=True)
        finally:
            app_mod.ask_yes_no = original
        assert tasks.submitted == []

    def test_clear_finished_reports_count(self) -> None:
        app = _bare_app()
        app._on_clear_finished()
        assert app.tasks.cleared == 1
        assert any("3" in text for text in app.window.statuses)
