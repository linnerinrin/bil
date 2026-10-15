"""领域模型与凭据存储测试。"""

from __future__ import annotations

import json
import pathlib
import shutil
import tempfile

from bilidown.core.credential import _SCHEME_B64, _SCHEME_DPAPI, _dpapi_available, CredentialStore
from bilidown.core.models import (
    DownloadCancelled,
    DownloadKind,
    DownloadStatus,
    DownloadTask,
    VideoItem,
)


def _item() -> VideoItem:
    return VideoItem(bvid="BV1xx411c7mD", cid=123, title="标题", author="UP")


def _tempdir() -> pathlib.Path:
    """自建临时目录（不依赖 pytest 的 tmp_path，便于零依赖运行器执行）。"""
    return pathlib.Path(tempfile.mkdtemp(prefix="bilidown-test-"))


class TestVideoItem:
    def test_copy_text_contains_fields(self) -> None:
        text = _item().copy_text()
        assert "BV1xx411c7mD" in text
        assert "标题" in text
        assert "UP" in text

    def test_cid_is_mutable_for_lazy_resolution(self) -> None:
        item = VideoItem(bvid="BV1xx411c7mD", cid=0, title="t")
        assert item.cid == 0
        item.cid = 999
        assert item.cid == 999


class TestDownloadStatus:
    def test_terminal_states(self) -> None:
        assert DownloadStatus.COMPLETED.is_terminal
        assert DownloadStatus.FAILED.is_terminal
        assert DownloadStatus.CANCELLED.is_terminal
        assert not DownloadStatus.PENDING.is_terminal
        assert not DownloadStatus.DOWNLOADING.is_terminal
        assert not DownloadStatus.MERGING.is_terminal


class TestDownloadTask:
    def test_initial_state(self) -> None:
        task = DownloadTask(item=_item(), kind=DownloadKind.VIDEO)
        assert task.status is DownloadStatus.PENDING
        assert task.progress == 0.0
        assert task.save_path == ""
        assert len(task.task_id) == 12

    def test_unique_ids(self) -> None:
        a = DownloadTask(item=_item())
        b = DownloadTask(item=_item())
        assert a.task_id != b.task_id
        assert a != b

    def test_progress_clamped(self) -> None:
        task = DownloadTask(item=_item())
        task.set_progress(150)
        assert task.progress == 100.0
        task.set_progress(-5)
        assert task.progress == 0.0

    def test_progress_text(self) -> None:
        task = DownloadTask(item=_item())
        task.set_progress(42.4)
        assert task.progress_text() == "42%"
        task.set_status(DownloadStatus.MERGING)
        assert task.progress_text() == "合并中"

    def test_fail_marks_error(self) -> None:
        task = DownloadTask(item=_item())
        task.fail("网络错误")
        assert task.status is DownloadStatus.FAILED
        assert task.error == "网络错误"
        assert task.progress == -1.0
        assert task.progress_text() == "失败"

    def test_cancel_sets_flag(self) -> None:
        task = DownloadTask(item=_item())
        assert not task.cancelled
        task.cancel()
        assert task.cancelled
        assert task.status is DownloadStatus.CANCELLED

    def test_cancel_does_not_override_terminal(self) -> None:
        task = DownloadTask(item=_item())
        task.set_status(DownloadStatus.COMPLETED)
        task.cancel()
        assert task.status is DownloadStatus.COMPLETED

    def test_check_cancelled_raises(self) -> None:
        task = DownloadTask(item=_item())
        task.check_cancelled()  # 未取消不应抛
        task.cancel()
        try:
            task.check_cancelled()
        except DownloadCancelled as exc:
            assert exc.task_id == task.task_id
        else:  # pragma: no cover
            raise AssertionError("应当抛出 DownloadCancelled")


class TestCredentialStore:
    def test_load_missing_returns_none(self) -> None:
        tmp = _tempdir()
        try:
            assert CredentialStore(str(tmp / "nope.dat")).load() is None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_roundtrip(self) -> None:
        tmp = _tempdir()
        try:
            store = CredentialStore(str(tmp / "cred.dat"))
            cookies = {"SESSDATA": "abc%2Fdef", "bili_jct": "xyz", "DedeUserID": "42"}
            assert store.save(cookies) is True
            assert store.load() == cookies
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_scheme_recorded_and_not_plaintext(self) -> None:
        tmp = _tempdir()
        try:
            path = tmp / "cred.dat"
            CredentialStore(str(path)).save({"SESSDATA": "top-secret"})
            wrapper = json.loads(path.read_text(encoding="utf-8"))
            expected = _SCHEME_DPAPI if _dpapi_available() else _SCHEME_B64
            assert wrapper["scheme"] == expected
            # 明文绝不能出现在落盘内容里
            assert "top-secret" not in path.read_text(encoding="utf-8")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_corrupt_file_returns_none(self) -> None:
        tmp = _tempdir()
        try:
            path = tmp / "cred.dat"
            path.write_text("{ not json", encoding="utf-8")
            assert CredentialStore(str(path)).load() is None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_empty_cookies_not_saved(self) -> None:
        tmp = _tempdir()
        try:
            path = tmp / "cred.dat"
            assert CredentialStore(str(path)).save({}) is False
            assert not path.exists()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_clear(self) -> None:
        tmp = _tempdir()
        try:
            path = tmp / "cred.dat"
            store = CredentialStore(str(path))
            store.save({"a": "b"})
            store.clear()
            assert store.load() is None
            store.clear()  # 重复清理不应报错
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
