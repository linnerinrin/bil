"""任务管理器测试：解析→排队→执行→回调的完整链路，以及取消/重试/清理。"""

from __future__ import annotations

import contextlib
import pathlib
import shutil
import tempfile
import time
from collections.abc import Iterator
from typing import Any

from bilidown.core import downloader as dl_mod
from bilidown.core.bridge import BackgroundLoop
from bilidown.core.client import StreamSelection
from bilidown.core.downloader import DownloadError
from bilidown.core.models import (
    DownloadKind,
    DownloadStatus,
    DownloadTask,
    VideoItem,
)
from bilidown.core.tasks import TaskManager

from .test_downloader import FakeResponse, patched


@contextlib.contextmanager
def temp_download_dir() -> Iterator[pathlib.Path]:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bilidown-tasks-"))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class OkClient:
    """总是成功解析出音频流。"""

    async def select_streams_async(self, item: VideoItem, kind: DownloadKind) -> StreamSelection:
        return StreamSelection(video_url="https://x/v", audio_url="https://x/a")


class AudioOnlyClient(OkClient):
    async def select_streams_async(self, item: VideoItem, kind: DownloadKind) -> StreamSelection:
        return StreamSelection(audio_url="https://x/a")


class FailingClient:
    async def select_streams_async(self, item: VideoItem, kind: DownloadKind) -> StreamSelection:
        raise DownloadError("模拟解析失败")


class FakeUrlClient(OkClient):
    """解析成功但 URL 不可达 —— 会走真实 requests（仅用于必须避免的场合，勿用）。"""


def _instant_client() -> FailingClient:
    """解析阶段即失败：让任务立刻 settle，绝不触发真实网络请求。"""
    return FailingClient()


def _item(bvid: str = "BV1xx411c7mD") -> VideoItem:
    return VideoItem(bvid=bvid, cid=1, title="任务测试", author="UP")


def _wait(predicate: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TestTaskManagerFlow:
    def test_successful_audio_task_settles(self) -> None:
        loop = BackgroundLoop()
        try:
            with temp_download_dir() as tmp:
                manager = TaskManager(
                    client=AudioOnlyClient(),  # type: ignore[arg-type]
                    download_dir=str(tmp),
                    bridge=loop,
                )
                settled: list[DownloadTask] = []
                manager.add_listener(settled.append)

                # 下载器走自己的会话，测试应在会话层替换，避免真实网络请求
                from .test_downloader import SessionStub

                manager.downloader._session = SessionStub(FakeResponse(b"data"))  # noqa: SLF001

                task = manager.submit(DownloadTask(item=_item(), kind=DownloadKind.AUDIO))
                assert _wait(lambda: bool(settled)), "任务未在超时内结束"

                assert task.status is DownloadStatus.COMPLETED
                assert pathlib.Path(task.save_path).exists()
                assert manager.active_count() == 0
                assert manager.snapshot_counts().get("完成") == 1
                manager.shutdown(wait=True)
        finally:
            loop.shutdown()

    def test_prepare_failure_marks_task_failed(self) -> None:
        """解析阶段失败也必须走统一失败路径并触发回调（修复 B9 的静默失败）。"""
        loop = BackgroundLoop()
        try:
            with temp_download_dir() as tmp:
                manager = TaskManager(
                    client=FailingClient(),  # type: ignore[arg-type]
                    download_dir=str(tmp),
                    bridge=loop,
                )
                settled: list[DownloadTask] = []
                manager.add_listener(settled.append)
                task = manager.submit(DownloadTask(item=_item(), kind=DownloadKind.VIDEO))

                assert _wait(lambda: bool(settled)), "失败回调未触发"
                assert task.status is DownloadStatus.FAILED
                assert "模拟解析失败" in task.error
                assert task.progress == -1.0
                manager.shutdown(wait=True)
        finally:
            loop.shutdown()

    def test_cancel_before_start(self) -> None:
        loop = BackgroundLoop()
        try:
            with temp_download_dir() as tmp:
                manager = TaskManager(
                    client=OkClient(),  # type: ignore[arg-type]
                    download_dir=str(tmp),
                    bridge=loop,
                )
                task = DownloadTask(item=_item(), kind=DownloadKind.AUDIO)
                task.cancel()
                manager.submit(task)
                assert task.status is DownloadStatus.CANCELLED
                manager.shutdown(wait=True)
        finally:
            loop.shutdown()

    def test_retry_creates_new_task(self) -> None:
        loop = BackgroundLoop()
        try:
            with temp_download_dir() as tmp:
                manager = TaskManager(
                    client=FailingClient(),  # type: ignore[arg-type]
                    download_dir=str(tmp),
                    bridge=loop,
                )
                original = DownloadTask(item=_item(), kind=DownloadKind.VIDEO)
                manager.submit(original)
                fresh = manager.retry(original)
                assert fresh.task_id != original.task_id
                assert fresh.item is original.item
                assert len(manager.tasks()) == 2
                manager.shutdown(wait=True)
        finally:
            loop.shutdown()

    def test_clear_finished(self) -> None:
        """使用「解析即失败」的客户端，避免任何真实网络请求。"""
        loop = BackgroundLoop()
        try:
            with temp_download_dir() as tmp:
                manager = TaskManager(
                    client=FailingClient(),  # type: ignore[arg-type]
                    download_dir=str(tmp),
                    bridge=loop,
                )
                first = DownloadTask(item=_item("BV1xx411c7mE"), kind=DownloadKind.AUDIO)
                second = DownloadTask(item=_item("BV1xx411c7mF"), kind=DownloadKind.AUDIO)
                manager.submit(first)
                manager.submit(second)
                assert _wait(lambda: manager.active_count() == 0), "任务未及时结束"
                assert len(manager.tasks()) == 2

                removed = manager.clear_finished()
                assert removed == 2
                assert manager.tasks() == []
                manager.shutdown(wait=True)
        finally:
            loop.shutdown()

    def test_remove_task(self) -> None:
        loop = BackgroundLoop()
        try:
            with temp_download_dir() as tmp:
                manager = TaskManager(
                    client=FailingClient(),  # type: ignore[arg-type]
                    download_dir=str(tmp),
                    bridge=loop,
                )
                task = DownloadTask(item=_item(), kind=DownloadKind.AUDIO)
                manager.submit(task)
                manager.remove(task)
                assert task not in manager.tasks()
                manager.shutdown(wait=True)
        finally:
            loop.shutdown()

    def test_submit_after_shutdown_raises(self) -> None:
        loop = BackgroundLoop()
        try:
            with temp_download_dir() as tmp:
                manager = TaskManager(
                    client=OkClient(),  # type: ignore[arg-type]
                    download_dir=str(tmp),
                    bridge=loop,
                )
                manager.shutdown(wait=True)
                try:
                    manager.submit(DownloadTask(item=_item()))
                except RuntimeError:
                    pass
                else:  # pragma: no cover
                    raise AssertionError("关闭后提交应当抛 RuntimeError")
        finally:
            loop.shutdown()
