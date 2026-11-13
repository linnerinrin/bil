#架构：偏好存储提供代理配置；本模块转换为requests与bilibili_api的出网策略
"""网络会话与代理策略

默认不信任环境代理（``trust_env=False``）；需要代理时在
``%LOCALAPPDATA%\\bilidown\\config.json``中开启
（``proxy``指定地址，或``use_system_proxy``用系统代理）

代理字段的读写统一由:mod:`bilidown.core.preferences`负责，本模块只消费它
"""

from __future__ import annotations  #延迟求值类型标注

#模块依赖：数据类承载代理偏好，日志记录网络配置状态
from dataclasses import dataclass  #定义轻量配置对象
from typing import Any  #兼容requests会话返回类型
from urllib.parse import urlsplit, urlunsplit  #解析代理URL以剥离认证信息

from ..utils.logger import get_logger  #获取模块日志器
from . import preferences as preferences_mod  #复用偏好存储

_log = get_logger("net")  #创建网络模块日志器


#函数：代理URL写入日志/界面前去掉用户名密码，防止凭据泄露
def _sanitize_proxy(proxy: str) -> str:  #返回脱敏后的代理描述
    """剥离代理URL中的认证信息，只保留scheme://host:port"""
    try:  #解析失败时返回占位符，绝不回传原文
        parts = urlsplit(proxy)  #拆分代理URL
    except ValueError:  #URL结构非法
        return "***"  #完全隐藏内容
    if parts.hostname is None:  #无法解析出主机（如裸host:port）
        return proxy.split("@")[-1] if "@" in proxy else proxy  #去掉userinfo前缀
    host = parts.hostname  #提取主机名
    if parts.port is not None:  #携带端口时拼接
        return urlunsplit((parts.scheme or "http", f"{host}:{parts.port}", "", "", ""))
    return urlunsplit((parts.scheme or "http", host, "", "", ""))  #仅保留协议与主机


#配置模型：只描述代理策略，不负责持久化
@dataclass(slots=True)  #限制实例动态属性
class NetworkSettings:  #封装本次会话的网络选项
    """与出网相关的设置（由偏好推导而来，不单独落盘）"""

    use_system_proxy: bool = False  #是否启用系统代理
    proxy: str = ""  #用户显式填写的代理地址

    #方法：把显式代理转换为requests的协议映射
    def proxies(self) -> dict[str, str] | None:  #返回双协议代理配置
        """返回requests需要的proxies参数；不需要代理时返回None"""
        if not self.proxy:  #未配置显式代理
            return None  #让会话不设置代理映射
        return {"http": self.proxy, "https": self.proxy}  #同一代理用于两种协议

    #方法：显式代理优先于环境代理
    def trust_env(self) -> bool:  #判断是否读取环境代理
        """是否让requests/aiohttp自行读取环境代理"""
        return self.use_system_proxy and not self.proxy  #只在未指定代理时读取环境


#函数：向调用方提供共享配置的路径
def config_path() -> str:  #返回配置文件位置
    """配置文件路径（与偏好共用同一个文件）"""
    return str(preferences_mod.store().path)  #转换偏好存储路径


#函数：从偏好读取配置并记录当前出网方式
def load_network_settings(*, force: bool = False) -> NetworkSettings:  #按需强制刷新
    """从偏好中取出网络设置"""
    prefs = preferences_mod.store().load(force=force)  #读取共享偏好
    settings = NetworkSettings(  #仅提取网络相关字段
        use_system_proxy=prefs.use_system_proxy, proxy=prefs.proxy
    )
    if settings.proxy:  #优先使用显式代理
        _log.info("使用显式代理: %s", _sanitize_proxy(settings.proxy))  #记录脱敏后的代理地址
    elif settings.use_system_proxy:  #未显式配置时使用系统代理
        _log.info("按配置使用系统代理")  #记录环境代理模式
    else:  #两种代理模式都未启用
        _log.info("不使用代理（直连）")  #记录直连模式
    return settings  #返回本次加载的策略


#函数：让下次读取配置时绕过缓存
def reset_cache() -> None:  #清理存储缓存
    """让缓存的偏好失效，下次调用重新从磁盘读取（只清缓存，不丢弃偏好存储实例）"""
    preferences_mod.store().reset_cache()  #保留存储实例并失效其缓存


#函数：为同步请求创建遵守统一代理策略的会话
def create_session(settings: NetworkSettings | None = None) -> Any:  #接受可选显式设置
    """创建一个按本项目策略配置好的requests会话

    所有同步出网请求（下载、换票、头像）都应使用它
    """
    import requests  #在创建会话时加载同步HTTP库

    cfg = settings or load_network_settings()  #优先使用调用方传入的设置
    session = requests.Session()  #创建独立会话
    #关键：不自动读取系统代理，避免被失效代理拦住
    session.trust_env = cfg.trust_env()  #设置是否继承环境代理
    proxies = cfg.proxies()  #构造显式代理映射
    if proxies:  #仅在指定代理时覆盖会话配置
        session.proxies.update(proxies)  #同步代理映射
    session.headers.update(  #设置默认浏览器标识
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
    )
    return session  #返回可用于同步请求的会话


#函数：同步代理策略给bilibili_api的异步请求层
def apply_library_settings(settings: NetworkSettings | None = None) -> None:  #可传入预读设置
    """把同一套策略同步给bilibili_api（它用aiohttp，默认会读环境代理）

    在应用启动时调用一次即可
    """
    cfg = settings or load_network_settings()  #加载本次应生效的网络配置
    try:  #避免第三方库同步失败中断启动
        from bilibili_api.utils.network import request_settings  #获取库的全局网络设置

        request_settings.set_trust_env(cfg.trust_env())  #设置环境代理继承方式
        #始终写入代理值：cfg.proxy为空时用空串清掉库中先前残留的代理
        request_settings.set_proxy(cfg.proxy or "")  #更新或清空全局代理
        _log.debug(  #输出全局网络设置供诊断
            "已同步 bilibili_api 网络设置: trust_env=%s, proxy=%s",
            request_settings.get_trust_env(),
            _sanitize_proxy(request_settings.get_proxy() or ""),  #脱敏后再记录
        )
    except Exception:  #noqa: BLE001 - 同步失败不应影响程序启动
        _log.warning("同步 bilibili_api 网络设置失败", exc_info=True)  #仅告警


#函数：返回界面或日志可显示的代理摘要
def describe(settings: NetworkSettings | None = None) -> str:  #格式化网络模式
    """给日志/界面用的一句话描述"""
    cfg = settings or load_network_settings()  #获取当前网络配置
    if cfg.proxy:  #显式代理优先
        return f"代理：{_sanitize_proxy(cfg.proxy)}"  #返回脱敏后的代理地址
    if cfg.use_system_proxy:  #无显式代理而启用系统代理
        return "代理：系统设置"  #返回系统代理说明
    return "代理：不使用（直连）"  #返回直连说明
