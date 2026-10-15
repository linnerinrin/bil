"""网络会话与代理策略。

背景（真实踩到的坑）
--------------------
本机的系统代理 ``127.0.0.1:7890`` 没有运行，而 ``requests`` 与 ``aiohttp``
在 ``trust_env=True``（默认值）时会**自动读取系统代理**，于是所有网络请求都会
被这个死代理拦住：

    ProxyError('Unable to connect to proxy',
      HTTPSConnection(host='127.0.0.1', port=7890): WinError 10061 拒绝连接)

表现为「明明能上网，程序却报连接失败」。

统一策略
--------
1. 默认**不信任环境代理**（``trust_env=False``）；
2. 需要代理时在 ``%LOCALAPPDATA%\\bilidown\\config.json`` 中开启
   （``proxy`` 指定地址，或 ``use_system_proxy`` 用系统代理）。

代理字段的读写统一由 :mod:`bilidown.core.preferences` 负责，本模块只消费它，
避免两个模块各写一半字段、互相覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..utils.logger import get_logger
from . import preferences as preferences_mod

_log = get_logger("net")


@dataclass(slots=True)
class NetworkSettings:
    """与出网相关的设置（由偏好推导而来，不单独落盘）。"""

    use_system_proxy: bool = False
    proxy: str = ""

    def proxies(self) -> dict[str, str] | None:
        """返回 requests 需要的 proxies 参数；不需要代理时返回 None。"""
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def trust_env(self) -> bool:
        """是否让 requests/aiohttp 自行读取环境代理。"""
        return self.use_system_proxy and not self.proxy


def config_path() -> str:
    """配置文件路径（与偏好共用同一个文件）。"""
    return str(preferences_mod.store().path)


def load_network_settings(*, force: bool = False) -> NetworkSettings:
    """从偏好中取出网络设置。"""
    prefs = preferences_mod.store().load(force=force)
    settings = NetworkSettings(
        use_system_proxy=prefs.use_system_proxy, proxy=prefs.proxy
    )
    if settings.proxy:
        _log.info("使用显式代理: %s", settings.proxy)
    elif settings.use_system_proxy:
        _log.info("按配置使用系统代理")
    else:
        _log.info("不使用代理（直连）")
    return settings


def reset_cache() -> None:
    """让缓存的偏好失效，下次调用重新从磁盘读取。

    注意：只清缓存，**不丢弃**偏好存储实例本身——否则测试里替换好的
    临时存储会被一起清掉。
    """
    preferences_mod.store().reset_cache()


def create_session(settings: NetworkSettings | None = None) -> Any:
    """创建一个按本项目策略配置好的 requests 会话。

    所有同步出网请求（下载、换票、头像）都应使用它。
    """
    import requests

    cfg = settings or load_network_settings()
    session = requests.Session()
    # 关键：不自动读取系统代理，避免被失效代理拦住
    session.trust_env = cfg.trust_env()
    proxies = cfg.proxies()
    if proxies:
        session.proxies.update(proxies)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def apply_library_settings(settings: NetworkSettings | None = None) -> None:
    """把同一套策略同步给 bilibili_api（它用 aiohttp，默认会读环境代理）。

    在应用启动时调用一次即可。
    """
    cfg = settings or load_network_settings()
    try:
        from bilibili_api.utils.network import request_settings

        request_settings.set_trust_env(cfg.trust_env())
        if cfg.proxy:
            request_settings.set_proxy(cfg.proxy)
        _log.debug(
            "已同步 bilibili_api 网络设置: trust_env=%s, proxy=%r",
            request_settings.get_trust_env(),
            request_settings.get_proxy(),
        )
    except Exception:  # noqa: BLE001 - 同步失败不应影响程序启动
        _log.warning("同步 bilibili_api 网络设置失败", exc_info=True)


def describe(settings: NetworkSettings | None = None) -> str:
    """给日志/界面用的一句话描述。"""
    cfg = settings or load_network_settings()
    if cfg.proxy:
        return f"代理：{cfg.proxy}"
    if cfg.use_system_proxy:
        return "代理：系统设置"
    return "代理：不使用（直连）"
