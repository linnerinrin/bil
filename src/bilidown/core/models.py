"""领域模型：搜索结果、下载任务、状态枚举。

把原先散落在 core.py 里的裸 dict 收敛为带类型的数据结构，
修复「任务字典被多线程无锁改写」「状态靠中文魔法字符串表示」等问题。
"""

from __future__ import annotations

import enum
import threading
import time
import uuid
from dataclasses import dataclass, field


class DownloadStatus(enum.Enum):
    """下载任务状态机。"""

    PENDING = "排队中"
    DOWNLOADING = "下载中"
    MERGING = "合并中"
    COMPLETED = "完成"
    FAILED = "失败"
    CANCELLED = "已取消"

    @property
    def is_terminal(self) -> bool:
        return self in (
            DownloadStatus.COMPLETED,
            DownloadStatus.FAILED,
            DownloadStatus.CANCELLED,
        )


class DownloadKind(enum.Enum):
    """下载类型。"""

    VIDEO = "video"  # 视频+音频，合并为 mp4
    AUDIO = "audio"  # 仅音频


@dataclass(slots=True)
class VideoItem:
    """一条视频信息（搜索结果或 BV 直查结果）。"""

    bvid: str
    cid: int
    title: str
    author: str = ""
    play: int = 0
    danmaku: int = 0
    duration: int = 0
    cover: str = ""

    def copy_text(self) -> str:
        return (
            f"BV号：{self.bvid}\n"
            f"标题：{self.title}\n"
            f"UP主：{self.author}\n"
            f"播放量：{self.play}\n"
            f"弹幕数：{self.danmaku}"
        )


@dataclass
class DownloadTask:
    """一个下载任务。

    进度由工作线程写入、UI 线程读取，因此所有可变字段的访问都走锁。
    """

    item: VideoItem
    kind: DownloadKind = DownloadKind.VIDEO
    save_path: str = ""
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)

    _status: DownloadStatus = field(default=DownloadStatus.PENDING, repr=False)
    _progress: float = field(default=0.0, repr=False)
    _message: str = field(default="", repr=False)
    _error: str = field(default="", repr=False)
    _lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False
    )
    _cancel: threading.Event = field(
        default_factory=threading.Event, repr=False, compare=False
    )

    # ------------------------------------------------------------- 只读属性
    @property
    def status(self) -> DownloadStatus:
        with self._lock:
            return self._status

    @property
    def progress(self) -> float:
        """0-100；失败时为 -1。"""
        with self._lock:
            return self._progress

    @property
    def message(self) -> str:
        with self._lock:
            return self._message

    @property
    def error(self) -> str:
        with self._lock:
            return self._error

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # --------------------------------------------------------------- 写方法
    def set_status(self, status: DownloadStatus, message: str = "") -> None:
        with self._lock:
            self._status = status
            if message:
                self._message = message

    def set_progress(self, value: float) -> None:
        with self._lock:
            self._progress = max(0.0, min(100.0, float(value)))

    def fail(self, error: str) -> None:
        with self._lock:
            self._status = DownloadStatus.FAILED
            self._error = error
            self._message = error
            self._progress = -1.0

    def cancel(self) -> None:
        """请求取消。工作线程在分块循环中检查该标志。"""
        self._cancel.set()
        with self._lock:
            if not self._status.is_terminal:
                self._status = DownloadStatus.CANCELLED
                self._message = "用户取消"

    def check_cancelled(self) -> None:
        """供工作线程调用；已取消则抛出 DownloadCancelled。"""
        if self._cancel.is_set():
            raise DownloadCancelled(self.task_id)

    def progress_text(self) -> str:
        p = self.progress
        if p < 0:
            return "失败"
        if self.status is DownloadStatus.MERGING:
            return "合并中"
        return f"{p:.0f}%"

    def __hash__(self) -> int:
        return hash(self.task_id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DownloadTask) and other.task_id == self.task_id


class DownloadCancelled(Exception):
    """任务被取消（内部控制流）。"""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"任务 {task_id} 已取消")
        self.task_id = task_id
