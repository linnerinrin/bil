"""应用控制器：装配各层、编排异步流程、驱动视图。

生命周期：
    BilidownApp()  -> 构建窗口与视图，恢复上次登录
    app.run()      -> 进入主循环
    app.shutdown() -> 关闭时取消轮询、停止任务、关闭事件循环
"""

from __future__ import annotations

import io
import sys
import threading
import tkinter as tk
from typing import Any

from PIL import Image

from pathlib import Path

from .config.paths import default_download_dir, ensure_dir, ensure_download_dir
from .config.settings import (
    DOWNLOAD_TIMEOUT,
    UI_REFRESH_INTERVAL_MS,
    USER_AGENT,
)
from .core import (
    BackgroundLoop,
    BiliClient,
    ClientError,
    DownloadKind,
    DownloadTask,
    TaskManager,
    VideoItem,
    parse_bv,
)
from .core.qrlogin import QrState
from .core.network import (
    apply_library_settings,
    create_session,
    describe,
    load_network_settings,
)
from .core.preferences import (
    Preferences,
    load as load_preferences,
    save as save_preferences,
)
from .ui.settings_view import SettingsView
from .utils.logger import get_logger, setup_logging
from .ui.download_view import DownloadView
from .ui.login_view import LoginView
from .ui.profile_view import ProfileView
from .ui.search_view import SearchView
from .ui.widgets import ask_yes_no, show_error
from .ui.window import MainWindow

_log = get_logger("app")

# 二维码轮询间隔（秒）
_QR_POLL_INTERVAL = 1.2
# 二维码有效期（秒），到期后主动停止轮询
_QR_LIFETIME = 180


class BilidownApp:
    """应用主控制器。"""

    def __init__(self) -> None:
        setup_logging()
        _log.info("启动中…（frozen=%s）", getattr(sys, "frozen", False))

        # 统一网络策略：默认绕过失效的系统代理，可按需在 config.json 中开启代理
        net_settings = load_network_settings()
        apply_library_settings(net_settings)
        _log.info("网络策略: %s", describe(net_settings))

        self.loop = BackgroundLoop.instance()
        self.client = BiliClient(self.loop)

        # 偏好：下载目录/背景等用户设置（首次运行会自动生成配置文件）
        self.prefs: Preferences = load_preferences()
        self.download_dir = self._resolve_download_dir(self.prefs)
        self.tasks = TaskManager(
            self.client, str(self.download_dir), bridge=self.loop,
            max_workers=self.prefs.max_concurrent,
        )
        _log.debug("核心层就绪，下载目录=%s", self.download_dir)

        self.window = MainWindow()
        self.window.on_navigate(self.navigate)
        self.window.on_logout(self.handle_logout)
        if self.prefs.background_path:
            self.window.set_background(self.prefs.background_path)
        _log.debug("主窗口构建完成")

        self.search_view: SearchView | None = None
        self.login_view: LoginView | None = None
        self.profile_view: ProfileView | None = None
        self.download_view: DownloadView | None = None
        self.settings_view: SettingsView | None = None

        self._current_view = ""
        self._refresh_job: str | None = None

        # 会话/轮询状态
        self._epoch = 0
        self._qr_polling = False
        self._user_info: dict[str, Any] | None = None
        self._workers: set[threading.Thread] = set()
        self._worker_lock = threading.Lock()
        self._shutting_down = False

        self.tasks.add_listener(lambda _task: self._schedule_download_refresh())

        self._restore_session()
        self.navigate("search")
        _log.info("启动完成，界面已就绪")

    # =============================================================== 导航
    def navigate(self, key: str) -> None:
        """切换视图。"""
        builders = {
            "search": self._build_search_view,
            "login": self._build_login_view,
            "profile": self._build_profile_view,
            "settings": self._build_settings_view,
            "downloads": self._build_download_view,
        }
        builder = builders.get(key)
        if builder is None:
            return

        self._stop_qr_polling()
        self.window.clear_content()
        self._current_view = key
        builder()
        self._update_status()

    @staticmethod
    def _resolve_download_dir(prefs: Preferences) -> Path:
        """把偏好里的下载目录解析成真实可用目录。

        用户指定了不存在/无权限的目录时，回退到默认目录，保证下载不会因为
        一个错误的设置而彻底不可用。
        """
        target = prefs.effective_download_dir()
        resolved = ensure_dir(target)
        if resolved is not None:
            return resolved

        _log.warning("下载目录不可用，回退到默认目录: %s", target)
        fallback = ensure_dir(default_download_dir())
        if fallback is not None:
            return fallback
        return ensure_download_dir()

    def _update_status(self) -> None:
        who = "已登录" if self.client.logged_in else "未登录"
        self.window.set_status(f"{who}｜下载目录：{self.download_dir}")

    # ------------------------------------------------------------ 视图构建
    def _build_search_view(self) -> None:
        view = SearchView(
            self.window.content_frame,
            on_search=self._on_search,
            on_copy=self._on_copy,
            on_download=self._on_download,
            on_batch=self._on_batch,
        )
        view.pack(fill=tk.BOTH, expand=True)
        self.search_view = view

    def _build_login_view(self) -> None:
        view = LoginView(
            self.window.content_frame,
            on_refresh=self.start_qr_login,
            on_logout=self.handle_logout,
        )
        view.pack(fill=tk.BOTH, expand=True)
        view.set_logged_in(self.client.logged_in)
        if self.client.logged_in:
            name = (self._user_info or {}).get("name") or "已登录用户"
            view.set_status(f"已登录：{name}。如需切换账号请先退出登录。")
        self.login_view = view
        if not self.client.logged_in:
            self.start_qr_login()

    def _build_profile_view(self) -> None:
        view = ProfileView(
            self.window.content_frame,
            self._user_info,
            on_refresh=self.refresh_user_info,
            on_login=lambda: self.navigate("login"),
        )
        view.pack(fill=tk.BOTH, expand=True)
        self.profile_view = view
        if self._user_info:
            self._load_avatar(self._user_info)

    def _build_download_view(self) -> None:
        view = DownloadView(
            self.window.content_frame,
            on_cancel=self.tasks.cancel,
            on_retry=self._on_retry,
            on_remove=self.tasks.remove,
            on_clear_finished=self._on_clear_finished,
            on_open_folder=lambda: DownloadView.open_path(str(self.download_dir)),
            on_open_file=self._on_open_file,
        )
        view.pack(fill=tk.BOTH, expand=True)
        self.download_view = view
        view.refresh(self.tasks.tasks())
        self._start_download_refresh()

    def _build_settings_view(self) -> None:
        view = SettingsView(
            self.window.content_frame,
            self.prefs,
            on_apply=self.apply_preferences,
            on_restore_defaults=self.restore_default_preferences,
        )
        view.pack(fill=tk.BOTH, expand=True)
        self.settings_view = view

    # =============================================================== 设置
    def apply_preferences(self, draft: Preferences) -> None:
        """应用设置页提交的草稿：校验 → 保存 → 让设置立即生效。"""
        prefs = draft  # SettingsView 已做范围校验，normalize 再兜一层
        prefs = Preferences.normalize(prefs.to_dict())

        saved = save_preferences(prefs)
        self.prefs = prefs

        self._apply_download_dir(prefs)
        self._apply_runtime_options(prefs)
        self.window.set_background(prefs.background_path)

        if self.settings_view is not None and self.settings_view.alive:
            message = "设置已保存" if saved else "设置已在本次运行生效，但写入配置文件失败（详见日志）"
            self.settings_view.show_saved(prefs, message)
        self._update_status()
        _log.info(
            "设置已应用: 背景=%s 目录=%s 并发=%d 重试=%d 完成后打开=%s",
            prefs.background_path or "（内置）",
            self.download_dir,
            prefs.max_concurrent,
            prefs.download_retries,
            prefs.open_folder_when_done,
        )

    def restore_default_preferences(self) -> None:
        """恢复默认设置（保留代理等未在界面暴露的字段）。"""
        defaults = Preferences.normalize(
            {
                # 网络设置属于用户在配置文件里的选择，恢复默认时不动它
                "use_system_proxy": self.prefs.use_system_proxy,
                "proxy": self.prefs.proxy,
            }
        )
        self.apply_preferences(defaults)

    def _apply_download_dir(self, prefs: Preferences) -> None:
        """让新的下载目录生效（只影响之后新建的任务）。"""
        resolved = self._resolve_download_dir(prefs)
        if resolved == self.download_dir:
            return
        self.download_dir = resolved
        self.tasks.set_download_dir(str(resolved))
        if self.download_view is not None and self.download_view.alive:
            self.download_view.refresh(self.tasks.tasks())

    def _apply_runtime_options(self, prefs: Preferences) -> None:
        """并发上限立即生效；重试次数由下载器在每次下载时读取偏好。"""
        self.tasks.set_max_workers(prefs.max_concurrent)

    # =============================================================== 登录
    def _restore_session(self) -> None:
        """启动时恢复上次登录状态并在后台校验。"""
        if not self.client.restore_credential():
            return
        self.window.set_logged_in(True)
        self.window.set_user_text("正在恢复登录…")

        def worker() -> None:
            valid = self.client.validate_credential()
            if not valid:
                _log.info("本地凭据已失效，需要重新登录")
                self._marshal(self._on_credential_invalid)
                return
            self._fetch_user_info(self._epoch)

        self._spawn(worker, "bilidown-restore")

    def _on_credential_invalid(self) -> None:
        self.client.logout()
        self._user_info = None
        self.window.set_logged_in(False)
        self.window.set_user_text("登录已过期，请重新扫码")
        if self._current_view == "login" and self.login_view is not None:
            self.login_view.set_logged_in(False)
            self.start_qr_login()

    def start_qr_login(self) -> None:
        """生成二维码并开始轮询（修复 B3：完整处理四种状态）。"""
        if self._shutting_down:
            return
        self._stop_qr_polling()
        epoch = self._epoch

        if self.login_view is not None:
            self.login_view.set_refreshing(True)
            self.login_view.set_status("正在生成二维码…")

        def worker() -> None:
            try:
                png = self.client.login_qrcode_start()
            except BaseException as exc:  # noqa: BLE001 - 含 CancelledError（退出时取消）
                _log.warning("生成二维码失败: %r", exc)
                self._marshal(lambda m=str(exc) or type(exc).__name__: self._on_qr_failed(m))
                return
            self._marshal(lambda: self._on_qr_ready(png, epoch))

        self._spawn(worker, "bilidown-qr")

    def _on_qr_ready(self, png: bytes, epoch: int) -> None:
        if epoch != self._epoch or self.login_view is None or self._shutting_down:
            return
        self.login_view.set_refreshing(False)
        self.login_view.set_qrcode(png)
        self.login_view.set_status("请使用哔哩哔哩客户端扫码")
        self._start_qr_polling(epoch)

    def _on_qr_failed(self, message: str) -> None:
        if self._shutting_down:
            return
        if self.login_view is not None and self.login_view.alive:
            self.login_view.set_refreshing(False)
            self.login_view.set_status(f"二维码生成失败：{message}", accent=False)
            show_error("登录失败", f"无法生成登录二维码：\n{message}")

    def _start_qr_polling(self, epoch: int) -> None:
        self._qr_polling = True

        def worker() -> None:
            import time

            deadline = time.monotonic() + _QR_LIFETIME
            while (
                self._qr_polling
                and epoch == self._epoch
                and not self._shutting_down
                and time.monotonic() < deadline
            ):
                try:
                    state = self.client.login_qrcode_poll()
                except BaseException as exc:  # noqa: BLE001
                    _log.warning("二维码状态查询失败: %r", exc)
                    time.sleep(_QR_POLL_INTERVAL)
                    continue
                self._marshal(lambda s=state: self._on_qr_state(s, epoch))
                if state in (QrState.DONE, QrState.TIMEOUT):
                    return
                time.sleep(_QR_POLL_INTERVAL)

        self._spawn(worker, "bilidown-qrpoll")

    def _stop_qr_polling(self) -> None:
        self._qr_polling = False

    def _on_qr_state(self, state: Any, epoch: int) -> None:
        if epoch != self._epoch:
            return
        view = self.login_view
        if view is None:
            return

        if state is QrState.SCAN:
            view.set_status("二维码已扫描，请在手机上确认登录", accent=False)
        elif state is QrState.CONF:
            view.set_status("已确认，正在完成登录…")
        elif state is QrState.TIMEOUT:
            # 关键修复 B3：明确处理过期，而不是无限轮询同一张失效二维码
            self._stop_qr_polling()
            view.set_refreshing(False)
            view.show_qrcode_expired()
        elif state is QrState.DONE:
            self._stop_qr_polling()
            view.set_status("登录成功！")
            self._complete_login(epoch)

    def _complete_login(self, epoch: int) -> None:
        try:
            self.client.login_qrcode_credential()
        except Exception as exc:  # noqa: BLE001
            _log.exception("获取登录凭据失败")
            show_error("登录失败", f"无法获取登录凭据：{exc}")
            return

        self.window.set_logged_in(True)
        self.window.set_user_text("登录成功，正在获取资料…")
        if self.login_view is not None:
            self.login_view.set_logged_in(True)
        self._epoch += 1
        self._sync_login_views_epoch()

        def worker() -> None:
            self._fetch_user_info(epoch + 1)

        self._spawn(worker, "bilidown-userinfo")

    def _sync_login_views_epoch(self) -> None:
        """登录成功后让已存在的视图退出轮询状态。"""
        if self.login_view is not None:
            self.login_view.set_logged_in(True)

    def handle_logout(self) -> None:
        if self.client.logged_in and not ask_yes_no("退出登录", "确定要退出当前账号吗？"):
            return
        self._epoch += 1
        self._stop_qr_polling()
        self.client.logout()
        self._user_info = None
        self.window.set_logged_in(False)
        self.window.set_user_text("未登录")
        if self.login_view is not None:
            self.login_view.set_logged_in(False)
            self.login_view.set_status("已退出登录，请重新扫描二维码")
        self._update_status()

    # =============================================================== 用户资料
    def _fetch_user_info(self, epoch: int) -> None:
        try:
            info = self.client.fetch_self_info()
        except Exception as exc:  # noqa: BLE001
            _log.warning("获取用户信息失败: %s", exc)
            # 不要停在「已登录但什么都取不到」的状态：
            # 主动校验一次凭据，失效就清掉并引导重新扫码。
            if not self.client.validate_credential():
                _log.info("凭据校验不通过，将清除本地凭据并要求重新登录")
                self._marshal(self._on_credential_invalid)
                return
            reason = str(exc).splitlines()[0][:120] if str(exc) else exc.__class__.__name__
            self._marshal(lambda r=reason: self._on_user_info_failed(r))
            return
        self._marshal(lambda: self._apply_user_info(info, epoch))

    def _on_user_info_failed(self, reason: str) -> None:
        if self._shutting_down:
            return
        self.window.set_user_text("已登录（资料获取失败）")
        self.window.set_status(f"资料获取失败：{reason}")
        show_error(
            "获取资料失败",
            "已登录，但获取用户资料失败：\n"
            f"{reason}\n\n"
            "可点击「我的」页面的「刷新资料」重试。",
        )

    def _apply_user_info(self, info: dict[str, Any], epoch: int) -> None:
        if epoch != self._epoch:
            return
        self._user_info = info
        name = info.get("name") or "未设置昵称"
        self.window.set_user_text(f"欢迎，{name}")
        self.window.set_logged_in(True)
        self._update_status()
        if self._current_view == "profile":
            self._build_profile_view()
        self._load_avatar(info)

    def refresh_user_info(self) -> None:
        if not self.client.logged_in:
            show_error("未登录", "请先扫码登录")
            return
        epoch = self._epoch
        self._spawn(lambda: self._fetch_user_info(epoch), "bilidown-userinfo")

    def _load_avatar(self, info: dict[str, Any]) -> None:
        url = info.get("face")
        if not url or self.profile_view is None:
            return
        epoch = self._epoch

        def worker() -> None:
            session = create_session()
            try:
                resp = session.get(url, timeout=DOWNLOAD_TIMEOUT)
                resp.raise_for_status()
                image = Image.open(io.BytesIO(resp.content)).convert("RGB")
            except Exception:  # noqa: BLE001 - 头像失败不影响主流程
                _log.info("头像加载失败", exc_info=True)
                self._marshal(lambda: self._set_avatar(None, epoch))
                return
            finally:
                session.close()
            self._marshal(lambda: self._set_avatar(image, epoch))

        self._spawn(worker, "bilidown-avatar")

    def _set_avatar(self, image: Image.Image | None, epoch: int) -> None:
        if epoch != self._epoch or self.profile_view is None:
            return
        self.profile_view.set_avatar(image)

    # =============================================================== 搜索
    def _on_search(self, keyword: str, page: int) -> None:
        epoch = self._epoch

        def worker() -> None:
            try:
                bvid = parse_bv(keyword)
                if bvid:
                    item = self.client.fetch_by_bvid(bvid)
                    result_items = [item]
                    used_keyword = bvid
                else:
                    result = self.client.search_videos(keyword, page)
                    result_items = result.items
                    used_keyword = result.keyword
            except ClientError as exc:
                self._marshal(lambda m=str(exc): self._on_search_failed(m))
                return
            except Exception as exc:  # noqa: BLE001
                _log.exception("搜索异常")
                self._marshal(lambda m=f"搜索失败：{exc}": self._on_search_failed(m))
                return
            self._marshal(
                lambda: self._render_search(result_items, used_keyword, page)
            )

        view = self.search_view
        if view is not None:
            view.set_loading(True)
        self._spawn(worker, "bilidown-search")

    def _on_search_failed(self, message: str) -> None:
        view = self.search_view
        if view is None:
            return
        view.set_loading(False)
        view.set_tip(message)
        view.clear_canvas()

    def _render_search(self, items: list[VideoItem], keyword: str, page: int) -> None:
        view = self.search_view
        if view is None:
            return
        view.render(items, keyword, page)

    def _on_copy(self, item: VideoItem) -> None:
        try:
            import pyperclip

            pyperclip.copy(item.copy_text())
            self.window.set_status(f"已复制：{item.title}")
        except Exception as exc:  # noqa: BLE001
            show_error("复制失败", f"无法写入剪贴板：{exc}")

    # =============================================================== 下载
    def _on_download(self, item: VideoItem, kind: str) -> None:
        self._enqueue([item], kind, confirm=True)

    def _on_batch(self, items: list[VideoItem], kind: str) -> None:
        self._enqueue(items, kind, confirm=True)

    def _enqueue(self, items: list[VideoItem], kind: str, *, confirm: bool) -> None:
        if not items:
            return
        if kind == "video":
            download_kind = DownloadKind.VIDEO
            label = "视频"
        else:
            download_kind = DownloadKind.AUDIO
            label = "音频"

        if confirm:
            title = items[0].title if len(items) == 1 else f"{len(items)} 个视频"
            if len(title) > 60:
                title = title[:60] + "…"
            if not ask_yes_no("确认下载", f"是否下载{label}：\n{title}"):
                return

        created = [
            self.tasks.submit(DownloadTask(item=item, kind=download_kind))
            for item in items
        ]
        self.window.set_status(
            f"已加入 {len(created)} 个{label}下载任务，可在「下载列表」查看进度"
        )
        self._schedule_download_refresh()

    def _on_retry(self, task: DownloadTask) -> None:
        self.tasks.retry(task)
        self._schedule_download_refresh()

    def _on_remove(self, task: DownloadTask) -> None:
        self.tasks.remove(task)
        self._schedule_download_refresh()

    def _on_clear_finished(self) -> None:
        removed = self.tasks.clear_finished()
        self.window.set_status(f"已清理 {removed} 条已结束的任务")
        self._schedule_download_refresh()

    def _on_open_file(self, task: DownloadTask) -> None:
        path = task.save_path
        if not path:
            show_error("无法打开", "该任务还没有产出文件")
            return
        try:
            DownloadView.open_path(path)
        except Exception as exc:  # noqa: BLE001
            show_error("无法打开", f"打开文件失败：{exc}")

    def _schedule_download_refresh(self) -> None:
        """从任意线程请求一次界面刷新。"""
        self._marshal(self._refresh_download_view)

    def _refresh_download_view(self) -> None:
        if self.download_view is not None and self.download_view.alive:
            self.download_view.refresh(self.tasks.tasks())

    def _start_download_refresh(self) -> None:
        """启动周期性刷新（仅当下载列表视图存在时）。"""
        self._stop_download_refresh()
        self._tick_download_refresh()

    def _tick_download_refresh(self) -> None:
        if self.download_view is None or not self.download_view.alive:
            self._refresh_job = None
            return
        self.download_view.refresh(self.tasks.tasks())
        self._refresh_job = self.window.after(
            UI_REFRESH_INTERVAL_MS, self._tick_download_refresh
        )

    def _stop_download_refresh(self) -> None:
        self.window.cancel_after(self._refresh_job)
        self._refresh_job = None

    # =============================================================== 线程编排
    def _spawn(self, target: Any, name: str) -> None:
        """启动并跟踪一个守护工作线程（退出时不再接受新线程）。"""
        if self._shutting_down:
            _log.debug("应用正在退出，忽略新的工作线程 %s", name)
            return

        def _wrapped() -> None:
            try:
                target()
            finally:
                with self._worker_lock:
                    self._workers.discard(thread)

        thread = threading.Thread(target=_wrapped, name=name, daemon=True)
        with self._worker_lock:
            self._workers.add(thread)
        thread.start()

    def _marshal(self, callback: Any) -> None:
        """把回调切回 Tk 主线程执行（线程安全）。"""
        if self._shutting_down:
            return
        try:
            self.window.root.after(0, callback)
        except (RuntimeError, tk.TclError):
            _log.debug("主窗口已关闭，丢弃回调")

    # =============================================================== 生命周期
    def run(self) -> None:
        self.window.root.protocol("WM_DELETE_WINDOW", self.shutdown)
        try:
            self.window.mainloop()
        finally:
            self._teardown()

    def shutdown(self) -> None:
        """用户关闭窗口：先清理资源，再销毁窗口。"""
        self._teardown()
        self.window.destroy()

    def _teardown(self) -> None:
        if getattr(self, "_torn_down", False):
            return
        self._torn_down = True
        self._shutting_down = True  # 先阻止新的回调与工作线程
        _log.info("正在退出…")
        self._stop_qr_polling()
        self._stop_download_refresh()
        self._join_workers(timeout=1.5)
        try:
            self.tasks.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            _log.debug("关闭任务管理器失败", exc_info=True)
        try:
            self.loop.shutdown(timeout=3.0)
        except Exception:  # noqa: BLE001
            _log.debug("关闭事件循环失败", exc_info=True)
        BackgroundLoop._instance = None  # noqa: SLF001 - 允许下次重建
        _log.info("已退出")

    def _join_workers(self, timeout: float) -> None:
        """短暂等待在飞的工作线程结束，避免它们撞上已关闭的事件循环。"""
        import time

        with self._worker_lock:
            workers = [t for t in self._workers if t.is_alive()]
        if not workers:
            return
        deadline = time.monotonic() + timeout
        for thread in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        with self._worker_lock:
            still = [t.name for t in self._workers if t.is_alive()]
        if still:
            _log.debug("仍有工作线程在运行（守护线程，随进程退出）: %s", still)


def main() -> None:
    """控制台入口（python -m bilidown）。

    环境变量 BILIDOWN_SELFTEST=1 时：构建界面但不进入主循环，打印结果后退出。
    用于打包产物的自动化冒烟验证（无需人工点关窗口）。
    """
    import os

    self_test = os.environ.get("BILIDOWN_SELFTEST") == "1"
    app = BilidownApp()
    if self_test:
        try:
            app.window.root.update_idletasks()
            app.window.root.update()
            print("SELFTEST_OK 视图=", app._current_view)
            print("SELFTEST_LOG ", _log.handlers)
            _log.info("打包自检通过：界面已成功构建")
        finally:
            app.shutdown()
        print("SELFTEST_DONE")
        return
    app.run()


if __name__ == "__main__":
    main()
