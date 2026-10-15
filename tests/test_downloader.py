"""下载器测试：临时文件隔离、断点续传、重试、取消、合并占位。

全部使用假的 HTTP 响应，不触网。
"""

from __future__ import annotations

import contextlib
import pathlib
import shutil
import tempfile
from collections.abc import Iterator
from typing import Any

import requests

from bilidown.core import downloader as dl_mod
from bilidown.core.client import StreamSelection
from bilidown.core.downloader import DownloadError, MediaDownloader
from bilidown.core.models import (
    DownloadCancelled,
    DownloadKind,
    DownloadStatus,
    DownloadTask,
    VideoItem,
)


@contextlib.contextmanager
def patched(obj: Any, **attrs: Any) -> Iterator[None]:
    """临时替换模块/对象属性。"""
    original = {name: getattr(obj, name) for name in attrs}
    for name, value in attrs.items():
        setattr(obj, name, value)
    try:
        yield
    finally:
        for name, value in original.items():
            setattr(obj, name, value)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status_code = status
        self.headers = {"Content-Length": str(len(body))}

    def raise_for_status(self) -> None:
        # 必须抛真实异常类型（requests.HTTPError ⊂ RequestException），
        # 否则重试逻辑测不到真实行为
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 1024, **_kw: object):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class SessionStub:
    """替身会话：记录每次 get() 的入参并返回预设响应。

    下载器现在走自己的会话（trust_env=False，避免被失效的系统代理拦住），
    所以测试应打在**会话**这一层，而不是 requests.get 这个全局函数上。
    """

    def __init__(self, responder: Any) -> None:
        """responder: 可调用 (url, **kwargs) -> FakeResponse，或直接是响应对象。"""
        self._responder = responder
        self.calls: list[dict[str, Any]] = []
        self.trust_env = False
        self.proxies: dict[str, str] = {}
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if callable(self._responder):
            return self._responder(url, **kwargs)
        return self._responder

    def close(self) -> None:
        self.closed = True

    @property
    def last_headers(self) -> dict[str, str]:
        return dict(self.calls[-1].get("headers") or {}) if self.calls else {}


def _use_session(downloader: MediaDownloader, responder: Any) -> SessionStub:
    """把替身会话装进下载器。"""
    stub = SessionStub(responder)
    downloader._session = stub  # noqa: SLF001
    return stub


class FakeClient:
    """固定返回预设的流地址（同时提供异步与同步两个门面）。"""

    def __init__(self, select: StreamSelection | None = None) -> None:
        self.select = select or StreamSelection(
            video_url="https://example.invalid/video.m4s",
            audio_url="https://example.invalid/audio.m4a",
            quality_label="1080P",
            audio_label="_192K",
        )
        self.calls = 0

    def _pick(self, kind: DownloadKind) -> StreamSelection:
        if kind is DownloadKind.AUDIO:
            return StreamSelection(audio_url=self.select.audio_url, audio_label="192K")
        return self.select

    async def select_streams_async(self, item: VideoItem, kind: DownloadKind) -> StreamSelection:
        self.calls += 1
        return self._pick(kind)

    def select_streams(self, item: VideoItem, kind: DownloadKind) -> StreamSelection:
        """同步门面：download(streams=None) 会走到这里。"""
        self.calls += 1
        return self._pick(kind)


def _item() -> VideoItem:
    return VideoItem(bvid="BV1xx411c7mD", cid=1, title="测试/标题:非法*字符?", author="UP")


def _tmpdir() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix="bilidown-dl-"))


class TestTempFileIsolation:
    def test_temp_paths_use_task_id(self) -> None:
        """B2 回归：不同任务绝不能共用临时文件。"""
        tmp = _tmpdir()
        try:
            client = FakeClient()
            downloader = MediaDownloader(client, str(tmp))  # type: ignore[arg-type]
            a = DownloadTask(item=_item())
            b = DownloadTask(item=_item())
            pa = downloader._tmp_path(a, "audio.m4a")
            pb = downloader._tmp_path(b, "audio.m4a")
            assert pa != pb
            assert a.task_id in pa.name
            assert pa.name.startswith(".")
            assert pa.name.endswith(".part")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_output_name_is_sanitized_and_unique(self) -> None:
        tmp = _tmpdir()
        try:
            client = FakeClient()
            downloader = MediaDownloader(client, str(tmp))  # type: ignore[arg-type]
            task = DownloadTask(item=_item(), kind=DownloadKind.AUDIO)
            body = b"x" * 2048
            _use_session(downloader, FakeResponse(body))
            out = downloader.download(task)
            path = pathlib.Path(out)
            assert path.exists()
            assert path.suffix == ".m4a"  # B10 回归：不是 .mp3
            assert "BV1xx411c7mD" in path.name
            assert not any(ch in path.name for ch in '<>:"/\\|?*')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestAudioDownload:
    def test_download_writes_file_and_marks_complete(self) -> None:
        tmp = _tmpdir()
        try:
            client = FakeClient()
            downloader = MediaDownloader(client, str(tmp))  # type: ignore[arg-type]
            task = DownloadTask(item=_item(), kind=DownloadKind.AUDIO)
            body = b"a" * 4096
            _use_session(downloader, FakeResponse(body))
            out = downloader.download(task)
            assert pathlib.Path(out).read_bytes() == body
            assert task.status is DownloadStatus.COMPLETED
            assert task.progress == 100.0
            assert task.save_path == out
            # 临时文件必须已被清理
            assert not list(tmp.glob("*.part"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_resume_uses_range_header(self) -> None:
        """断点续传：已存在部分文件时应带 Range 且以追加方式写入。"""
        tmp = _tmpdir()
        try:
            client = FakeClient()
            downloader = MediaDownloader(client, str(tmp))  # type: ignore[arg-type]
            task = DownloadTask(item=_item(), kind=DownloadKind.AUDIO)
            tmp_file = downloader._tmp_path(task, "audio.m4a")
            tmp_file.write_bytes(b"already-")

            # 206 = 服务端接受 Range，返回剩余部分
            stub = _use_session(downloader, FakeResponse(b"rest", status=206))
            downloader.download(task)

            assert stub.last_headers["Range"].startswith("bytes=")
            assert pathlib.Path(task.save_path).read_bytes() == b"already-rest"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_audio_url_raises(self) -> None:
        tmp = _tmpdir()
        try:
            client = FakeClient(StreamSelection(video_url="v", audio_url=""))
            downloader = MediaDownloader(client, str(tmp))  # type: ignore[arg-type]
            task = DownloadTask(item=_item(), kind=DownloadKind.AUDIO)
            try:
                downloader.download(task)
            except DownloadError as exc:
                assert "音频" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("应当抛 DownloadError")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_retries_then_succeeds(self) -> None:
        tmp = _tmpdir()
        try:
            client = FakeClient()
            downloader = MediaDownloader(client, str(tmp))  # type: ignore[arg-type]
            task = DownloadTask(item=_item(), kind=DownloadKind.AUDIO)
            attempts = {"n": 0}

            def flaky(*_a: Any, **_k: Any) -> FakeResponse:
                attempts["n"] += 1
                if attempts["n"] == 1:
                    return FakeResponse(b"", status=500)
                return FakeResponse(b"ok")

            _use_session(downloader, flaky)
            with patched(dl_mod, DOWNLOAD_BACKOFF_BASE=1.0):
                out = downloader.download(task)
            assert attempts["n"] == 2
            assert pathlib.Path(out).read_bytes() == b"ok"
            assert task.status is DownloadStatus.COMPLETED
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gives_up_after_retries(self) -> None:
        tmp = _tmpdir()
        try:
            client = FakeClient()
            downloader = MediaDownloader(client, str(tmp))  # type: ignore[arg-type]
            task = DownloadTask(item=_item(), kind=DownloadKind.AUDIO)

            def always_fail(*_a: Any, **_k: Any) -> FakeResponse:
                return FakeResponse(b"", status=503)

            _use_session(downloader, always_fail)
            with patched(dl_mod, DOWNLOAD_RETRIES=2, DOWNLOAD_BACKOFF_BASE=1.0):
                try:
                    downloader.download(task)
                except DownloadError as exc:
                    assert "音频" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("应当抛 DownloadError")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cancel_mid_download(self) -> None:
        tmp = _tmpdir()
        try:
            client = FakeClient()
            downloader = MediaDownloader(client, str(tmp))  # type: ignore[arg-type]
            task = DownloadTask(item=_item(), kind=DownloadKind.AUDIO)

            class CancellingResponse(FakeResponse):
                def iter_content(self, chunk_size: int = 1024, **_kw: object):
                    yield b"first"
                    task.cancel()  # 第二块之前取消
                    yield b"second"

            _use_session(downloader, CancellingResponse(b"ignored"))
            with patched(dl_mod, DOWNLOAD_RETRIES=1):
                try:
                    downloader.download(task)
                except DownloadCancelled:
                    pass
                else:  # pragma: no cover
                    raise AssertionError("应当抛 DownloadCancelled")
            assert task.cancelled
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestVideoDownload:
    def test_merge_called_and_temp_files_cleaned(self) -> None:
        tmp = _tmpdir()
        try:
            client = FakeClient()
            downloader = MediaDownloader(client, str(tmp))  # type: ignore[arg-type]
            task = DownloadTask(item=_item(), kind=DownloadKind.VIDEO)
            merge_args: dict[str, Any] = {}

            def fake_merge(video: str, audio: str, output: str) -> None:
                merge_args.update(video=video, audio=audio, output=output)
                pathlib.Path(output).write_bytes(b"merged-mp4")

            body = b"m" * 1024
            _use_session(downloader, FakeResponse(body))
            with patched(dl_mod, merge_video_audio=fake_merge):
                out = downloader.download(task)

            assert pathlib.Path(out).read_bytes() == b"merged-mp4"
            assert pathlib.Path(merge_args["video"]).name.startswith(".")
            assert pathlib.Path(merge_args["audio"]).name.startswith(".")
            assert not list(tmp.glob("*.part"))
            assert task.status is DownloadStatus.COMPLETED
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_video_stream_raises(self) -> None:
        tmp = _tmpdir()
        try:
            client = FakeClient(StreamSelection(video_url="", audio_url="a"))
            downloader = MediaDownloader(client, str(tmp))  # type: ignore[arg-type]
            task = DownloadTask(item=_item(), kind=DownloadKind.VIDEO)
            try:
                downloader.download(task)
            except DownloadError as exc:
                assert "音视频流" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("应当抛 DownloadError")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_progress_advances_within_stage(self) -> None:
        tmp = _tmpdir()
        try:
            client = FakeClient()
            downloader = MediaDownloader(client, str(tmp))  # type: ignore[arg-type]
            task = DownloadTask(item=_item(), kind=DownloadKind.AUDIO)
            body = b"p" * 8192
            _use_session(downloader, FakeResponse(body))
            downloader.download(task)
            assert task.progress == 100.0
            assert task.status is DownloadStatus.COMPLETED
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestBackupUrlFallback:
    """主地址失败时应回退到 B 站自带的 backupUrl（实测每档有 2 个备用 CDN）。"""

    def _downloader(self, tmp: pathlib.Path, responder: Any) -> MediaDownloader:
        client = FakeClient()
        downloader = MediaDownloader(client, str(tmp))  # type: ignore[arg-type]
        _use_session(downloader, responder)
        return downloader

    def test_falls_back_to_backup_on_primary_failure(self) -> None:
        tmp = _tmpdir()
        try:
            attempted: list[str] = []

            def responder(url: str, **_kw: Any) -> FakeResponse:
                attempted.append(url)
                if "main" in url:
                    return FakeResponse(b"", status=403)  # 主地址 403
                return FakeResponse(b"backup-ok")

            downloader = self._downloader(tmp, responder)
            streams = StreamSelection(
                audio_url="https://x/main.m4a",
                audio_backups=["https://x/backup1.m4a", "https://x/backup2.m4a"],
            )
            task = DownloadTask(item=_item(), kind=DownloadKind.AUDIO)
            with patched(dl_mod, DOWNLOAD_RETRIES=1, DOWNLOAD_BACKOFF_BASE=1.0):
                output = downloader.download(task, streams)

            assert pathlib.Path(output).read_bytes() == b"backup-ok"
            assert attempted[0] == "https://x/main.m4a"
            assert "https://x/backup1.m4a" in attempted, f"未尝试备用地址: {attempted}"
            assert task.status is DownloadStatus.COMPLETED
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_tries_all_backups_before_giving_up(self) -> None:
        tmp = _tmpdir()
        try:
            attempted: list[str] = []

            def responder(url: str, **_kw: Any) -> FakeResponse:
                attempted.append(url)
                return FakeResponse(b"", status=404)

            downloader = self._downloader(tmp, responder)
            streams = StreamSelection(
                audio_url="https://x/main.m4a",
                audio_backups=["https://x/b1.m4a", "https://x/b2.m4a"],
            )
            task = DownloadTask(item=_item(), kind=DownloadKind.AUDIO)
            with patched(dl_mod, DOWNLOAD_RETRIES=1, DOWNLOAD_BACKOFF_BASE=1.0):
                try:
                    downloader.download(task, streams)
                except DownloadError:
                    pass
                else:  # pragma: no cover
                    raise AssertionError("全部失败时应抛 DownloadError")

            # 每个地址都会被重试多次，这里只关心「尝试过哪些不同的地址」
            unique = list(dict.fromkeys(attempted))
            assert unique == [
                "https://x/main.m4a",
                "https://x/b1.m4a",
                "https://x/b2.m4a",
            ], unique
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    def test_primary_success_never_touches_backup(self) -> None:
        tmp = _tmpdir()
        try:
            attempted: list[str] = []

            def responder(url: str, **_kw: Any) -> FakeResponse:
                attempted.append(url)
                return FakeResponse(b"primary-ok")

            downloader = self._downloader(tmp, responder)
            streams = StreamSelection(
                audio_url="https://x/main.m4a", audio_backups=["https://x/b1.m4a"]
            )
            task = DownloadTask(item=_item(), kind=DownloadKind.AUDIO)
            output = downloader.download(task, streams)
            assert pathlib.Path(output).read_bytes() == b"primary-ok"
            assert attempted == ["https://x/main.m4a"], attempted
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_backup_clears_partial_file(self) -> None:
        """回退到备用地址前必须清掉主地址留下的半成品，否则会拼接成坏文件。"""
        tmp = _tmpdir()
        try:
            def responder(url: str, **_kw: Any) -> FakeResponse:
                if "main" in url:
                    return FakeResponse(b"PARTIAL-JUNK", status=500)
                return FakeResponse(b"clean")

            downloader = self._downloader(tmp, responder)
            streams = StreamSelection(
                audio_url="https://x/main.m4a", audio_backups=["https://x/b1.m4a"]
            )
            task = DownloadTask(item=_item(), kind=DownloadKind.AUDIO)
            with patched(dl_mod, DOWNLOAD_RETRIES=1, DOWNLOAD_BACKOFF_BASE=1.0):
                output = downloader.download(task, streams)
            content = pathlib.Path(output).read_bytes()
            assert content == b"clean", f"残留与备用内容拼接了: {content!r}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestDeferredSessionClose:
    """退出时若仍有下载在跑，不能立刻关会话（会让在飞请求报错）。"""

    def test_close_deferred_while_active(self) -> None:
        tmp = _tmpdir()
        try:
            client = FakeClient()
            downloader = MediaDownloader(client, str(tmp))  # type: ignore[arg-type]
            stub = _use_session(downloader, FakeResponse(b"x"))

            downloader._active = 1  # 模拟正在下载  # noqa: SLF001
            downloader.close()
            assert stub.closed is False, "有活跃任务时不应立即关闭会话"
            assert downloader._close_pending is True  # noqa: SLF001

            downloader._active = 0  # noqa: SLF001
            downloader._close_now()  # noqa: SLF001
            assert stub.closed is True
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_close_immediate_when_idle(self) -> None:
        tmp = _tmpdir()
        try:
            client = FakeClient()
            downloader = MediaDownloader(client, str(tmp))  # type: ignore[arg-type]
            stub = _use_session(downloader, FakeResponse(b"x"))
            downloader.close()
            assert stub.closed is True
            downloader.close()  # 幂等
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_download_decrements_active_counter(self) -> None:
        tmp = _tmpdir()
        try:
            client = FakeClient()
            downloader = MediaDownloader(client, str(tmp))  # type: ignore[arg-type]
            _use_session(downloader, FakeResponse(b"data"))
            task = DownloadTask(item=_item(), kind=DownloadKind.AUDIO)
            downloader.download(task)
            assert downloader._active == 0, "download 结束后活跃计数必须归零"  # noqa: SLF001
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
