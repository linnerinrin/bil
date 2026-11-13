"""路径解析：严格区分「只读资源」与「可写用户数据」

对外接口：
    resource_path(*parts)：只读资源（打包后从_MEIPASS取）
    user_data_dir()：可写用户数据根目录
    ensure_user_data_dir()：确保存在并返回
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

#使用标准库logging而非utils.logger.get_logger，避免循环依赖（config.paths -> utils.logger -> config.paths）
_log = logging.getLogger("bilidown.paths")

APP_NAME = "bilidown"

#内置背景资源名（打包时随resources/一起打入）
BACKGROUND_NAME = "background.jpg"

#允许作为自定义背景的图片后缀
BACKGROUND_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})

#内置背景的引用前缀：配置里存"builtin:<文件名>"，与绝对路径区分
BUILTIN_PREFIX = "builtin:"


def list_builtin_backgrounds() -> list[str]:
    """列出打包内置的背景图文件名（按名称排序）

    图片放进``src/bilidown/resources/``即可被设置页列出（打包时该目录整体打入）
    """
    root = resource_root()
    try:
        if not root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in root.iterdir()
            if entry.is_file() and entry.suffix.lower() in BACKGROUND_SUFFIXES
        )
    except OSError:  #pragma: no cover - 目录不可读时当作没有内置背景
        _log.warning("读取内置背景目录失败: %s", root, exc_info=True)
        return []


def default_builtin_background() -> str:
    """默认内置背景文件名（第一个可用的，找不到返回空串）"""
    names = list_builtin_backgrounds()
    if not names:
        return ""
    if BACKGROUND_NAME in names:
        return BACKGROUND_NAME
    return names[0]


def is_builtin_ref(value: str) -> bool:
    """判断一个背景偏好值是不是「内置背景引用」"""
    return bool(value) and value.startswith(BUILTIN_PREFIX)


def parse_builtin_ref(value: str) -> str:
    """从"builtin:xxx.jpg"取出文件名；不是内置引用则返回空串"""
    if not is_builtin_ref(value):
        return ""
    return value[len(BUILTIN_PREFIX) :]


def make_builtin_ref(name: str) -> str:
    """构造内置背景引用"""
    return f"{BUILTIN_PREFIX}{name}"


def is_frozen() -> bool:
    """是否运行在PyInstaller等打包环境中"""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """只读资源根目录

    打包后为sys._MEIPASS（onefile/onedir均由PyInstaller提供），
    开发态为源码包内的resources/目录
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        #打包时资源以('src/bilidown/resources', 'bilidown/resources')形式打入
        bundled = Path(meipass) / "bilidown" / "resources"
        if bundled.exists():
            return bundled
        return Path(meipass) / "resources"
    #开发态：本文件位于<pkg>/config/paths.py，包根为其上两级
    return Path(__file__).resolve().parent.parent / "resources"


def resource_path(*parts: str) -> Path:
    """拼接出一个只读资源的绝对路径（不保证存在，由调用方决定如何兜底）"""
    return resource_root().joinpath(*parts)


def user_data_dir() -> Path:
    """可写用户数据根目录（不放程序目录，避免UAC/只读盘问题）

    Windows: %LOCALAPPDATA%\\bilidown
    其他平台: ~/.local/share/bilidown
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Local" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def ensure_user_data_dir() -> Path:
    """确保用户数据目录存在并返回"""
    d = user_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_download_dir() -> Path:
    """默认下载目录：用户「下载」文件夹下的bilidown子目录"""
    downloads = Path.home() / "Downloads"
    if not downloads.exists():
        downloads = Path.home()
    return downloads / APP_NAME


def ensure_download_dir() -> Path:
    """确保默认下载目录存在并返回"""
    d = default_download_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        d = ensure_user_data_dir() / "downloads"
        d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_dir(path: str | Path) -> Path | None:
    """确保任意目录存在；无法创建时返回None（调用方负责提示）

    用于「设置下载目录」：用户可能填不存在的路径或没有写权限的位置
    """
    try:
        target = Path(path).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        return target if target.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def load_image_fitted(path: str | Path, size: tuple[int, int]) -> object | None:
    """加载单张图片并等比填充（cover）缩放到目标尺寸；失败返回None"""
    from PIL import Image, ImageOps

    try:
        if not Path(path).is_file():
            return None
        with Image.open(path) as opened:
            image = opened.convert("RGB")
        return ImageOps.fit(image, size, method=Image.LANCZOS, centering=(0.5, 0.5))
    except Exception:  #noqa: BLE001 - 坏图/权限/解码失败都只记日志
        _log.warning("图片加载失败: %s", path, exc_info=True)
        return None


def load_builtin_background(
    size: tuple[int, int] = (1200, 800), name: str = ""
) -> object | None:
    """加载指定的内置背景图；``name``为空时用默认内置背景，失败返回None"""
    candidates: list[str] = []
    if name:
        candidates.append(name)
    default = default_builtin_background()
    if default and default not in candidates:
        candidates.append(default)
    #额外候选：包根的其他命名
    candidates.append("ctj.jpg")

    for candidate in candidates:
        image = load_image_fitted(resource_path(candidate), size)
        if image is not None:
            return image
    _log.warning("未找到内置背景图（资源目录: %s）", resource_root())
    return None


def load_background_image(
    size: tuple[int, int] = (1200, 800),
    custom_path: str | Path | None = None,
) -> object | None:
    """加载背景：优先自定义图片，失败则用内置背景；都不可用返回None

    支持三类取值：

    - ``""``：默认内置背景
    - ``"builtin:文件名"``：指定的内置背景
    - 其它：自定义图片的绝对路径
    """
    text = str(custom_path or "")
    if text:
        if is_builtin_ref(text):
            return load_builtin_background(size, parse_builtin_ref(text))
        image = load_image_fitted(text, size)
        if image is not None:
            return image
        _log.warning("自定义背景不可用，改用内置背景: %s", text)
    return load_builtin_background(size)


def is_supported_background(path: str | Path) -> bool:
    """判断文件是否为支持的背景图格式（按后缀，最终仍以能否解码为准）"""
    return Path(path).suffix.lower() in BACKGROUND_SUFFIXES
