"""偏好存储测试：默认值、越界纠正、损坏回退、旧字段兼容、原子写入。"""

from __future__ import annotations

import json
import pathlib
import shutil
import tempfile

from bilidown.config.paths import default_download_dir
from bilidown.core.preferences import (
    CONCURRENT_RANGE,
    DEFAULT_CONCURRENT,
    DEFAULT_RETRIES,
    RETRIES_RANGE,
    PreferenceStore,
    Preferences,
)


def _tempdir() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix="bilidown-prefs-"))


class TestNormalize:
    def test_empty_gives_defaults(self) -> None:
        prefs = Preferences.normalize({})
        assert prefs.background_path == ""
        assert prefs.download_dir == ""
        assert prefs.max_concurrent == DEFAULT_CONCURRENT
        assert prefs.download_retries == DEFAULT_RETRIES
        assert prefs.open_folder_when_done is False
        assert prefs.use_system_proxy is False
        assert prefs.proxy == ""

    def test_none_and_garbage_input(self) -> None:
        for raw in (None, [], "not-a-dict", 42):
            prefs = Preferences.normalize(raw)
            assert prefs.max_concurrent == DEFAULT_CONCURRENT

    def test_out_of_range_is_clamped(self) -> None:
        """并发/重试次数越界时必须夹到合法区间，不能让界面传入非法值。"""
        low = Preferences.normalize({"max_concurrent": 0, "download_retries": -5})
        assert low.max_concurrent == CONCURRENT_RANGE[0]
        assert low.download_retries == RETRIES_RANGE[0]

        high = Preferences.normalize({"max_concurrent": 99, "download_retries": 999})
        assert high.max_concurrent == CONCURRENT_RANGE[1]
        assert high.download_retries == RETRIES_RANGE[1]

    def test_non_numeric_falls_back(self) -> None:
        prefs = Preferences.normalize({"max_concurrent": "abc", "download_retries": None})
        assert prefs.max_concurrent == DEFAULT_CONCURRENT
        assert prefs.download_retries == DEFAULT_RETRIES

    def test_numeric_string_is_accepted(self) -> None:
        prefs = Preferences.normalize({"max_concurrent": "4", "download_retries": "2"})
        assert prefs.max_concurrent == 4
        assert prefs.download_retries == 2

    def test_bool_coercion(self) -> None:
        assert Preferences.normalize({"open_folder_when_done": "true"}).open_folder_when_done
        assert Preferences.normalize({"open_folder_when_done": 1}).open_folder_when_done
        assert not Preferences.normalize({"open_folder_when_done": "no"}).open_folder_when_done

    def test_missing_background_file_is_dropped(self) -> None:
        """背景图被删除后，配置里残留的路径必须被忽略，回退内置背景。"""
        prefs = Preferences.normalize({"background_path": "Z:/definitely/missing.jpg"})
        assert prefs.background_path == ""

    def test_existing_background_file_kept(self) -> None:
        tmp = _tempdir()
        try:
            image = tmp / "bg.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            prefs = Preferences.normalize({"background_path": str(image)})
            assert prefs.background_path == str(image)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_legacy_proxy_fields_are_read(self) -> None:
        """旧版配置文件只有代理字段，必须能正常读出。"""
        prefs = Preferences.normalize(
            {"use_system_proxy": True, "proxy": "http://127.0.0.1:7890"}
        )
        assert prefs.use_system_proxy is True
        assert prefs.proxy == "http://127.0.0.1:7890"
        # 其余字段取默认值
        assert prefs.max_concurrent == DEFAULT_CONCURRENT

    def test_unknown_fields_are_preserved(self) -> None:
        """不认识的字段不能丢，避免旧版本或用户手写的配置被静默删除。"""
        prefs = Preferences.normalize({"some_future_option": "keep-me"})
        assert prefs.extras.get("some_future_option") == "keep-me"
        assert prefs.to_dict().get("some_future_option") == "keep-me"


class TestDerivedValues:
    def test_effective_download_dir_default(self) -> None:
        prefs = Preferences()
        assert prefs.effective_download_dir() == default_download_dir()

    def test_effective_download_dir_custom(self) -> None:
        prefs = Preferences(download_dir="D:/my-videos")
        assert prefs.effective_download_dir() == pathlib.Path("D:/my-videos")

    def test_tilde_is_expanded(self) -> None:
        prefs = Preferences(download_dir="~/somewhere")
        resolved = prefs.effective_download_dir()
        assert "~" not in str(resolved)
        assert resolved.is_absolute()

    def test_copy_with_applies_and_normalizes(self) -> None:
        prefs = Preferences()
        updated = prefs.copy_with(max_concurrent=99, open_folder_when_done=True)
        assert updated.max_concurrent == CONCURRENT_RANGE[1]  # 已夹紧
        assert updated.open_folder_when_done is True
        # 原对象不受影响
        assert prefs.max_concurrent == DEFAULT_CONCURRENT


class TestStoreLoad:
    def test_missing_file_creates_default(self) -> None:
        tmp = _tempdir()
        try:
            path = tmp / "config.json"
            store = PreferenceStore(path)
            prefs = store.load()
            assert prefs.max_concurrent == DEFAULT_CONCURRENT
            assert path.is_file(), "首次读取应生成默认配置文件"
            saved = json.loads(path.read_text(encoding="utf-8"))
            assert saved["max_concurrent"] == DEFAULT_CONCURRENT
            assert saved["open_folder_when_done"] is False
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_corrupt_file_falls_back(self) -> None:
        tmp = _tempdir()
        try:
            path = tmp / "config.json"
            path.write_text("{ not valid json", encoding="utf-8")
            prefs = PreferenceStore(path).load()
            assert prefs.max_concurrent == DEFAULT_CONCURRENT
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_save_then_load_roundtrip(self) -> None:
        tmp = _tempdir()
        try:
            path = tmp / "config.json"
            store = PreferenceStore(path)
            store.save(
                Preferences(
                    download_dir="D:/videos",
                    max_concurrent=4,
                    download_retries=1,
                    open_folder_when_done=True,
                )
            )
            # 新 store 强制从磁盘读
            fresh = PreferenceStore(path).load()
            assert fresh.download_dir == "D:/videos"
            assert fresh.max_concurrent == 4
            assert fresh.download_retries == 1
            assert fresh.open_folder_when_done is True
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_save_is_atomic_no_leftover_tmp(self) -> None:
        """写入应是「临时文件 + 原子替换」，不应留下 .tmp 残留。"""
        tmp = _tempdir()
        try:
            path = tmp / "config.json"
            store = PreferenceStore(path)
            assert store.save(Preferences()) is True
            assert not list(tmp.glob("*.tmp")), "不应残留临时文件"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_save_to_unwritable_path_returns_false(self) -> None:
        """写入失败只返回 False，不抛异常（用户操作不应因此中断）。"""
        tmp = _tempdir()
        try:
            # 用一个「父路径是文件」的非法位置模拟写入失败
            blocker = tmp / "blocker"
            blocker.write_text("x", encoding="utf-8")
            store = PreferenceStore(blocker / "config.json")
            assert store.save(Preferences()) is False
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cache_is_used_and_can_be_reset(self) -> None:
        tmp = _tempdir()
        try:
            path = tmp / "config.json"
            store = PreferenceStore(path)
            store.save(Preferences(max_concurrent=5))
            assert store.load().max_concurrent == 5

            # 绕过 store 直接改文件：缓存未失效时仍返回旧值
            path.write_text(json.dumps({"max_concurrent": 2}), encoding="utf-8")
            assert store.load().max_concurrent == 5
            # force 后读到新值
            assert store.load(force=True).max_concurrent == 2
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_real_path_is_under_user_data(self) -> None:
        path = PreferenceStore().path
        assert path.name == "config.json"
        assert "bilidown" in str(path).lower()
