"""网络与代理策略测试。

回归一个真实故障：本机系统代理 127.0.0.1:7890 没运行，而 requests / aiohttp
在 trust_env=True 时会自动读系统代理，导致所有出网请求被死代理拦住：

    ProxyError('Unable to connect to proxy', ... WinError 10061 拒绝连接)

因此默认必须直连，并允许用户显式开启代理。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import tempfile

from bilidown.core import network


def _tempdir() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix="bilidown-net-"))


class TestParseLoginCookiesUnaffected:
    """确保没有破坏解析逻辑（顺手的最小回归）。"""

    def test_sessdata_kept_raw(self) -> None:
        from bilidown.core.qrlogin import parse_login_cookies

        cookies = parse_login_cookies("https://x/c?SESSDATA=a%2Cb%2Ac")
        assert cookies["SESSDATA"] == "a%2Cb%2Ac"


class TestNetworkSettingsDefaults:
    def test_defaults_are_direct_connection(self) -> None:
        """默认必须直连（不用代理），否则死代理会把所有请求拦死。"""
        settings = network.NetworkSettings()
        assert settings.use_system_proxy is False
        assert settings.proxy == ""
        assert settings.trust_env() is False
        assert settings.proxies() is None

    def test_explicit_proxy_overrides(self) -> None:
        settings = network.NetworkSettings(proxy="http://127.0.0.1:7890")
        assert settings.proxies() == {
            "http": "http://127.0.0.1:7890",
            "https": "http://127.0.0.1:7890",
        }
        # 显式代理时不应再让 requests 自己读环境
        assert settings.trust_env() is False

    def test_system_proxy_opt_in(self) -> None:
        settings = network.NetworkSettings(use_system_proxy=True)
        assert settings.trust_env() is True
        assert settings.proxies() is None

    def test_describe(self) -> None:
        assert "直连" in network.describe(network.NetworkSettings())
        assert "系统设置" in network.describe(network.NetworkSettings(use_system_proxy=True))
        assert "7890" in network.describe(network.NetworkSettings(proxy="http://127.0.0.1:7890"))


class TestCreateSession:
    def test_session_does_not_trust_env_by_default(self) -> None:
        """关键回归：默认会话绝不能自动读系统代理。"""
        session = network.create_session(network.NetworkSettings())
        try:
            assert session.trust_env is False
            assert not session.proxies
        finally:
            session.close()

    def test_session_uses_explicit_proxy(self) -> None:
        session = network.create_session(
            network.NetworkSettings(proxy="http://127.0.0.1:7890")
        )
        try:
            assert session.proxies.get("http") == "http://127.0.0.1:7890"
            assert session.proxies.get("https") == "http://127.0.0.1:7890"
        finally:
            session.close()

    def test_session_has_user_agent(self) -> None:
        session = network.create_session(network.NetworkSettings())
        try:
            assert "Mozilla" in session.headers.get("User-Agent", "")
        finally:
            session.close()

    def test_system_proxy_opt_in_sets_trust_env(self) -> None:
        session = network.create_session(
            network.NetworkSettings(use_system_proxy=True)
        )
        try:
            assert session.trust_env is True
        finally:
            session.close()


class TestConfigFile:
    """代理字段现在由 preferences 统一读写，这里通过替换偏好存储来隔离文件。"""

    @staticmethod
    def _use_temp_store(path: pathlib.Path):
        """把模块级默认偏好存储换成指向临时文件的实例，返回还原函数。

        注意不能调用 ``network.reset_cache()`` —— 它会清掉我们刚替换进去的实例。
        这里只清偏好存储自身的缓存。
        """
        from bilidown.core import preferences as prefs_mod

        original = prefs_mod._default_store  # noqa: SLF001
        temp_store = prefs_mod.PreferenceStore(path)
        prefs_mod._default_store = temp_store  # noqa: SLF001
        temp_store.reset_cache()

        def restore() -> None:
            prefs_mod._default_store = original  # noqa: SLF001

        return restore

    def test_missing_config_returns_defaults_and_creates_file(self) -> None:
        tmp = _tempdir()
        restore = self._use_temp_store(tmp / "config.json")
        try:
            settings = network.load_network_settings(force=True)
            assert settings.use_system_proxy is False
            assert (tmp / "config.json").is_file(), "首次运行应生成默认配置"
            saved = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
            assert saved["use_system_proxy"] is False
        finally:
            restore()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_config_file_read(self) -> None:
        tmp = _tempdir()
        try:
            path = tmp / "config.json"
            path.write_text(
                json.dumps({"use_system_proxy": True, "proxy": ""}), encoding="utf-8"
            )
            restore = self._use_temp_store(path)
            try:
                settings = network.load_network_settings(force=True)
                assert settings.use_system_proxy is True
                assert settings.trust_env() is True
            finally:
                restore()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_corrupt_config_falls_back_to_direct(self) -> None:
        tmp = _tempdir()
        try:
            path = tmp / "config.json"
            path.write_text("{ this is not json", encoding="utf-8")
            restore = self._use_temp_store(path)
            try:
                settings = network.load_network_settings(force=True)
                assert settings.use_system_proxy is False
                assert settings.trust_env() is False
            finally:
                restore()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_real_config_path_is_under_user_data(self) -> None:
        path = network.config_path()
        assert path.endswith("config.json")
        assert "bilidown" in path.lower()


class TestLibrarySettingsSync:
    def test_apply_sets_trust_env_false(self) -> None:
        from bilibili_api.utils.network import request_settings

        original = request_settings.get_trust_env()
        try:
            network.apply_library_settings(network.NetworkSettings())
            assert request_settings.get_trust_env() is False, (
                "必须把「直连」策略同步给 bilibili_api，否则搜索仍会走死代理"
            )
        finally:
            request_settings.set_trust_env(original)

    def test_apply_with_system_proxy_sets_true(self) -> None:
        from bilibili_api.utils.network import request_settings

        original = request_settings.get_trust_env()
        try:
            network.apply_library_settings(
                network.NetworkSettings(use_system_proxy=True)
            )
            assert request_settings.get_trust_env() is True
        finally:
            request_settings.set_trust_env(original)

    def test_apply_never_raises(self) -> None:
        # 即使库结构变化也不能影响启动
        network.apply_library_settings(network.NetworkSettings())


class TestDownloaderUsesOwnSession:
    def test_downloader_session_bypasses_system_proxy(self) -> None:
        """下载器必须用自己的会话，不能落到 requests 的隐式系统代理上。"""
        from bilidown.core.downloader import MediaDownloader

        class FakeClient:
            pass

        downloader = MediaDownloader(FakeClient(), ".")  # type: ignore[arg-type]
        try:
            session = downloader._get_session()  # noqa: SLF001
            assert session.trust_env is False, "下载会话不能信任环境代理"
            # 复用同一个会话（多个流/重试共享连接）
            assert downloader._get_session() is session  # noqa: SLF001
        finally:
            downloader.close()

    def test_close_is_idempotent(self) -> None:
        from bilidown.core.downloader import MediaDownloader

        class FakeClient:
            pass

        downloader = MediaDownloader(FakeClient(), ".")  # type: ignore[arg-type]
        downloader._get_session()  # noqa: SLF001
        downloader.close()
        downloader.close()  # 重复关闭不应报错
