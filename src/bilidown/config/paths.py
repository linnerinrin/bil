"""路径解析：严格区分「只读资源」与「可写用户数据」。

修复审计缺陷 A1 / A2：
  - A1：PyInstaller 打包后资源位于 sys._MEIPASS，原先用相对路径直接打开必然 FileNotFoundError。
  - A2：原先所有文件访问（背景图、二维码、下载目录）都相对「当前工作目录」，
        从别的目录启动就会崩。这里统一改为绝对路径。

对外接口：
    resource_path(*parts)   -> 只读资源（打包后从 _MEIPASS 取）
    user_data_dir()         -> 可写用户数据根目录
    ensure_user_data_dir()  -> 确保存在并返回
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# 注意：这里故意使用标准库 logging 而不是 utils.logger.get_logger。
# utils.logger 需要从本模块导入 ensure_user_data_dir，若本模块再反向导入它，
# 就会形成循环依赖（config.paths -> utils.logger -> config.paths）。
# 用同名子 logger 同样能接入项目的日志配置（bilidown.* 层级）。
_log = logging.getLogger("bilidown.paths")

APP_NAME = "bilidown"

# 内置背景资源名（打包时随 resources/ 一起打入）
BACKGROUND_NAME = "background.jpg"

# 允许作为自定义背景的图片后缀
BACKGROUND_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})

# 内置背景的引用前缀：配置里存 "builtin:<文件名>"，避免把绝对路径与资源名混在一起
BUILTIN_PREFIX = "builtin:"


def list_builtin_backgrounds() -> list[str]:
    """列出打包内置的背景图文件名（按名称排序）。

    想增加内置背景，只要把图片放进 ``src/bilidown/resources/`` 即可，
    设置页会自动列出来（打包时该目录整体打入，无需改代码）。
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
    except OSError:  # pragma: no cover - 目录不可读时当作没有内置背景
        _log.warning("读取内置背景目录失败: %s", root, exc_info=True)
        return []


def default_builtin_background() -> str:
    """默认内置背景文件名（第一个可用的，找不到返回空串）。"""
    names = list_builtin_backgrounds()
    if not names:
        return ""
    if BACKGROUND_NAME in names:
        return BACKGROUND_NAME
    return names[0]


def is_builtin_ref(value: str) -> bool:
    """判断一个背景偏好值是不是「内置背景引用」。"""
    return bool(value) and value.startswith(BUILTIN_PREFIX)


def parse_builtin_ref(value: str) -> str:
    """从 "builtin:xxx.jpg" 取出文件名；不是内置引用则返回空串。"""
    if not is_builtin_ref(value):
        return ""
    return value[len(BUILTIN_PREFIX) :]


def make_builtin_ref(name: str) -> str:
    """构造内置背景引用。"""
    return f"{BUILTIN_PREFIX}{name}"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 等打包环境中。"""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """只读资源根目录。

    打包后为 sys._MEIPASS（onefile/onedir 均由 PyInstaller 提供），
    开发态为源码包内的 resources/ 目录。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        # 打包时资源以 ('src/bilidown/resources', 'bilidown/resources') 形式打入
        bundled = Path(meipass) / "bilidown" / "resources"
        if bundled.exists():
            return bundled
        return Path(meipass) / "resources"
    # 开发态：本文件位于 <pkg>/config/paths.py，包根为其上两级
    return Path(__file__).resolve().parent.parent / "resources"


def resource_path(*parts: str) -> Path:
    """拼接出一个只读资源的绝对路径（不保证存在，由调用方决定如何兜底）。"""
    return resource_root().joinpath(*parts)


def user_data_dir() -> Path:
    """可写用户数据根目录（不放程序目录，避免 UAC/只读盘问题）。

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
    """确保用户数据目录存在并返回。"""
    d = user_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_download_dir() -> Path:
    """默认下载目录：用户「下载」文件夹下的 bilidown 子目录。"""
    downloads = Path.home() / "Downloads"
    if not downloads.exists():
        downloads = Path.home()
    return downloads / APP_NAME


def ensure_download_dir() -> Path:
    """确保默认下载目录存在并返回。"""
    d = default_download_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        d = ensure_user_data_dir() / "downloads"
        d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_dir(path: str | Path) -> Path | None:
    """确保任意目录存在；无法创建时返回 None（调用方负责提示）。

    用于「设置下载目录」：用户可能填一个不存在的路径或没有写权限的位置。
    """
    try:
        target = Path(path).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        return target if target.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def load_image_fitted(path: str | Path, size: tuple[int, int]) -> object | None:
    """加载单张图片并按**等比填充（cover）**缩放到目标尺寸；失败返回 None。

    cover 的语义：先等比放大到刚好盖住目标区域，再居中裁掉多余边缘，
    因此既不会拉伸变形，也不会出现黑边。
    """
    from PIL import Image, ImageOps

    try:
        if not Path(path).is_file():
            return None
        with Image.open(path) as opened:
            image = opened.convert("RGB")
        return ImageOps.fit(image, size, method=Image.LANCZOS, centering=(0.5, 0.5))
    except Exception:  # noqa: BLE001 - 坏图/权限/解码失败都只记日志
        _log.warning("图片加载失败: %s", path, exc_info=True)
        return None


def load_builtin_background(
    size: tuple[int, int] = (1200, 800), name: str = ""
) -> object | None:
    """加载指定的内置背景图；``name`` 为空时用默认内置背景。

    失败返回 None（调用方回退纯色）。
    """
    candidates: list[str] = []
    if name:
        candidates.append(name)
    default = default_builtin_background()
    if default and default not in candidates:
        candidates.append(default)
    # 兼容历史上把内置图放在包根的其他命名
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
    """加载背景：优先自定义图片，失败则用内置背景；都不可用返回 None。

    支持三类取值：

    - ``""``：默认内置背景
    - ``"builtin:文件名"``：指定的内置背景
    - 其它：自定义图片的绝对路径

    需要区分「用的是自定义图还是内置图」的场景（例如设置页与窗口），
    请分别调用 :func:`load_image_fitted` 与 :func:`load_builtin_background`。
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
    """判断文件是否是我们支持的背景图格式（按后缀，最终仍以能否解码为准）。"""
    return Path(path).suffix.lower() in BACKGROUND_SUFFIXES
