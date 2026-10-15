"""下载任务编排：异步解析 + 信号量限流 + 取消/重试 + 完成回调。

修复审计缺陷 C10：旧代码「一次下载 = 一个裸 threading.Thread」，
没有并发上限、没有队列、无法取消、无法重试。

执行流程（两段式，避免在 UI 线程做网络解析）：
    1) prepare_async  在后台事件循环里解析 cid / playurl；
    2) _execute       在下载线程池中受限流并发执行真正的下载与合并。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from ..config.settings import MAX_CONCURRENT_DOWNLOADS
from ..utils.logger import get_logger
from .bridge import BackgroundLoop
from .client import BiliClient
from .downloader import DownloadError, MediaDownloader
from .models import DownloadCancelled, DownloadStatus, DownloadTask

_log = get_logger("tasks")

# 任务结束后调用：on_settled(task)。在**工作线程/后台线程**中触发，
# UI 层需自行切回主线程。
SettleCallback = Callable[[DownloadTask], None]


class TaskManager:
    """管理下载任务的生命周期。"""

    def __init__(
        self,
        client: BiliClient,
        download_dir: str | None = None,
        max_workers: int = MAX_CONCURRENT_DOWNLOADS,
        bridge: BackgroundLoop | None = None,
    ) -> None:
        self.client = client
        self.downloader = MediaDownloader(client, download_dir)
        self.download_dir = self.downloader.download_dir
        self._bridge = bridge or BackgroundLoop.instance()
        self._slots = threading.BoundedSemaphore(max(1, max_workers))
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers), thread_name_prefix="bilidown-dl"
        )
        self._tasks: list[DownloadTask] = []
        self._prep_futures: dict[str, Future[object]] = {}
        self._listeners: list[SettleCallback] = []
        self._lock = threading.RLock()
        self._shutdown = False

    # ------------------------------------------------------------- 观察者
    def add_listener(self, callback: SettleCallback) -> None:
        """注册「任务结束」回调（完成/失败/取消都会触发）。"""
        with self._lock:
            self._listeners.append(callback)

    def _notify(self, task: DownloadTask) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(task)
            except Exception:  # noqa: BLE001
                _log.exception("任务监听器抛出异常")

    # ------------------------------------------------------------- 任务操作
    def submit(self, task: DownloadTask) -> DownloadTask:
        """登记并排队一个任务（解析阶段即开始，下载阶段受限流）。"""
        with self._lock:
            if self._shutdown:
                raise RuntimeError("任务管理器已关闭")
            self._tasks.append(task)

        if task.cancelled:
            task.set_status(DownloadStatus.CANCELLED, "已取消（未开始）")
            self._notify(task)
            return task

        future = self._bridge.submit(
            self.downloader.prepare_async(task),
            on_success=lambda streams, t=task: self._execute(t, streams),
            on_error=lambda exc, t=task: self._fail_prepare(t, exc),
        )
        with self._lock:
            self._prep_futures[task.task_id] = future
        return task

    def _execute(self, task: DownloadTask, streams: object) -> None:
        """解析成功后进入限流队列，在工作线程中执行下载。"""
        with self._lock:
            self._prep_futures.pop(task.task_id, None)
        if self._shutdown or task.cancelled:
            task.set_status(DownloadStatus.CANCELLED, "已取消")
            self._notify(task)
            return
        self._executor.submit(self._run, task, streams)

    def _fail_prepare(self, task: DownloadTask, exc: BaseException) -> None:
        with self._lock:
            self._prep_futures.pop(task.task_id, None)
        task.fail(str(exc) or exc.__class__.__name__)
        _log.error("解析下载地址失败 %s: %s", task.item.bvid, exc)
        self._notify(task)

    def _run(self, task: DownloadTask, streams: object) -> None:
        with self._slots:
            try:
                path = self.downloader.download(task, streams)  # type: ignore[arg-type]
                _log.info("任务完成 %s -> %s", task.item.bvid, path)
            except DownloadCancelled:
                task.set_status(DownloadStatus.CANCELLED, "已取消")
                _log.info("任务已取消 %s", task.item.bvid)
            except DownloadError as exc:
                task.fail(str(exc))
                _log.error("任务失败 %s: %s", task.item.bvid, exc)
            except Exception as exc:  # noqa: BLE001 - 兜底，避免线程静默死亡（修复 B9）
                task.fail(f"未预期的错误：{exc}")
                _log.exception("任务异常 %s", task.item.bvid)
        self._notify(task)

    def cancel(self, task: DownloadTask) -> None:
        """请求取消。若尚未开始解析则直接取消。"""
        task.cancel()
        with self._lock:
            future = self._prep_futures.get(task.task_id)
        if future is not None and future.cancel():
            with self._lock:
                self._prep_futures.pop(task.task_id, None)
            self._notify(task)
        else:
            _log.info("已请求取消任务 %s", task.item.bvid)

    def retry(self, task: DownloadTask) -> DownloadTask:
        """重新入队一个新任务（相同视频/类型），保留历史记录。"""
        fresh = DownloadTask(item=task.item, kind=task.kind)
        return self.submit(fresh)

    def remove(self, task: DownloadTask) -> None:
        """从列表中移除记录（不删除已下载文件）。"""
        self.cancel(task)
        with self._lock:
            if task in self._tasks:
                self._tasks.remove(task)

    def clear_finished(self) -> int:
        """移除所有已结束的任务，返回移除数量。"""
        with self._lock:
            done = [t for t in self._tasks if t.status.is_terminal]
            for t in done:
                self._tasks.remove(t)
        return len(done)

    # ------------------------------------------------------------- 查询
    def tasks(self) -> list[DownloadTask]:
        """返回任务快照（拷贝，避免并发修改）。"""
        with self._lock:
            return list(self._tasks)

    def snapshot_counts(self) -> dict[str, int]:
        """各类状态计数，供状态栏显示。"""
        counts: dict[str, int] = {}
        for t in self.tasks():
            key = t.status.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    def active_count(self) -> int:
        return sum(1 for t in self.tasks() if not t.status.is_terminal)

    # ------------------------------------------------------------- 运行时配置
    def set_download_dir(self, download_dir: str) -> None:
        """更新下载目录。

        只影响**之后新建**的任务；已在下载或排队中的任务保留原目录，
        避免同一个任务写到一半换目录导致文件分散。
        """
        self.downloader.download_dir = Path(download_dir)
        self.download_dir = self.downloader.download_dir

    def set_max_workers(self, count: int) -> None:
        """调整并发上限。

        通过重建信号量实现；已在执行的任务继续跑完，新任务按新上限排队。
        （线程池的线程数保持创建时的值，这里限制的是同时在跑的流数量。）
        """
        safe = max(1, int(count))
        with self._lock:
            self._slots = threading.BoundedSemaphore(safe)
        _log.info("并发上限已调整为 %d", safe)

    # ------------------------------------------------------------- 生命周期
    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            tasks = list(self._tasks)
        for t in tasks:
            if not t.status.is_terminal:
                t.cancel()
        self._executor.shutdown(wait=wait, cancel_futures=True)
        try:
            self.downloader.close()
        except Exception:  # noqa: BLE001
            _log.debug("关闭下载会话失败", exc_info=True)
        _log.debug("任务管理器已关闭")
