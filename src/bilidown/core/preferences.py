"""用户偏好：背景、下载目录、并发与重试等运行时设置。

设计要点
--------
1. **只用一个配置文件**：``%LOCALAPPDATA%\\bilidown\\config.json``。
   原先代理配置由 ``core/network.py`` 自己读写同一路径，现在统一委托到这里，
   避免两处各写一半字段导致互相覆盖。
2. **绝不因为配置出错而崩**：文件缺失自动生成、损坏回退默认值、越界值自动纠正。
3. **兼容旧文件**：旧版本只有 ``use_system_proxy`` / ``proxy`` 两个字段，
   读取时其余字段取默认值，写入时保留原字段，不会让旧配置失效。
4. 校验逻辑集中在 ``normalize``，所以「从文件加载」与「界面提交」走同一套规则。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config.paths import (
    default_download_dir,
    ensure_user_data_dir,
    is_builtin_ref,
    list_builtin_backgrounds,
    parse_builtin_ref,
)
from ..utils.logger import get_logger

_log = get_logger("preferences")

CONFIG_FILE = "config.json"

# 边界值：界面与文件读取共用，避免两处各写一套
CONCURRENT_RANGE = (1, 5)
RETRIES_RANGE = (1, 5)
DEFAULT_CONCURRENT = 2
DEFAULT_RETRIES = 3

# 背景图支持的格式（供文件对话框使用）
BACKGROUND_FILETYPES: tuple[tuple[str, str], ...] = (
    ("图片文件", "*.jpg *.jpeg *.png *.bmp *.webp"),
    ("所有文件", "*.*"),
)

# 兼容更短的名字
BackgroundFiletypes = BACKGROUND_FILETYPES
BACKGROUND_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})

# 备选背景目录（选择图片时作为初始目录），实际存在的才会用
_BACKGROUND_HINTS = (
    ("Pictures", "图片"),
    ("Pictures", "Screenshots"),
    ("Downloads",),
    ("Desktop",),
)


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    """把任意输入夹到 [low, high]；无法解析则用默认值。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


@dataclass(slots=True)
class Preferences:
    """全部用户偏好。字段顺序即配置文件里的书写顺序。"""

    # 自定义背景图路径；空字符串表示使用程序内置背景
    background_path: str = ""
    # 下载目录；空字符串表示使用默认目录（~/Downloads/bilidown）
    download_dir: str = ""
    max_concurrent: int = DEFAULT_CONCURRENT
    download_retries: int = DEFAULT_RETRIES
    open_folder_when_done: bool = False
    # 网络：保留原 core/network.py 的语义
    use_system_proxy: bool = False
    proxy: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------- 校验
    @classmethod
    def normalize(cls, raw: Any) -> Preferences:
        """从任意 dict-like 构造出合法偏好（未知字段保留在 extras 里）。"""
        if not isinstance(raw, dict):
            raw = {}

        background = str(raw.get("background_path") or "").strip()
        if is_builtin_ref(background):
            # 内置背景引用：确认那个资源确实存在，否则回退默认内置背景
            name = parse_builtin_ref(background)
            if name not in list_builtin_backgrounds():
                _log.info("配置中的内置背景不存在，改用默认内置背景: %s", name)
                background = ""
        elif background and not Path(background).is_file():
            # 自定义背景被删除/改名时忽略它，回退内置背景，避免每次启动都报错
            _log.info("配置中的背景图不存在，改用内置背景: %s", background)
            background = ""

        download_dir = str(raw.get("download_dir") or "").strip()

        known = {
            "background_path",
            "download_dir",
            "max_concurrent",
            "download_retries",
            "open_folder_when_done",
            "use_system_proxy",
            "proxy",
        }
        extras = {k: v for k, v in raw.items() if k not in known}

        return cls(
            background_path=background,
            download_dir=download_dir,
            max_concurrent=_clamp(
                raw.get("max_concurrent"), *CONCURRENT_RANGE, DEFAULT_CONCURRENT
            ),
            download_retries=_clamp(
                raw.get("download_retries"), *RETRIES_RANGE, DEFAULT_RETRIES
            ),
            open_folder_when_done=_as_bool(raw.get("open_folder_when_done")),
            use_system_proxy=_as_bool(raw.get("use_system_proxy")),
            proxy=str(raw.get("proxy") or "").strip(),
            extras=extras,
        )

    # ------------------------------------------------------------- 派生
    def effective_download_dir(self) -> Path:
        """实际使用的下载目录（已展开 ``~`` 与环境变量）。"""
        if not self.download_dir:
            return default_download_dir()
        try:
            return Path(self.download_dir).expanduser()
        except (RuntimeError, ValueError):  # pragma: no cover - 极端的畸形路径
            _log.warning("下载目录无法解析，使用默认目录: %r", self.download_dir)
            return default_download_dir()

    def to_dict(self) -> dict[str, Any]:
        """转成用于落盘的 dict（含未识别的旧字段，保证不丢用户数据）。"""
        data = asdict(self)
        extras = data.pop("extras", {}) or {}
        data.update(extras)
        return data

    def copy_with(self, **changes: Any) -> Preferences:
        """基于当前值生成一个改了几项的副本（用于「应用」按钮）。"""
        data = self.to_dict()
        data.update(changes)
        return Preferences.normalize(data)


class PreferenceStore:
    """偏好读写（可指定路径，便于测试）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._explicit = Path(path) if path is not None else None
        self._cached: Preferences | None = None

    # ------------------------------------------------------------- 路径
    @property
    def path(self) -> Path:
        if self._explicit is not None:
            return self._explicit
        return ensure_user_data_dir() / CONFIG_FILE

    # ------------------------------------------------------------- 读
    def load(self, *, force: bool = False) -> Preferences:
        """读取偏好；文件缺失/损坏都返回默认值，并尽力补写一份默认文件。"""
        if self._cached is not None and not force:
            return self._cached

        target = self.path
        try:
            with open(target, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            prefs = Preferences()
            self._cached = prefs
            self.save(prefs)
            _log.info("已生成默认配置: %s", target)
            return prefs
        except (OSError, ValueError):
            _log.warning("配置文件损坏，使用默认值: %s", target, exc_info=True)
            prefs = Preferences()
            self._cached = prefs
            return prefs

        prefs = Preferences.normalize(raw)
        self._cached = prefs
        _log.debug(
            "已加载偏好: 背景=%s 下载目录=%s 并发=%d 重试=%d 自动打开=%s",
            prefs.background_path or "（内置）",
            prefs.effective_download_dir(),
            prefs.max_concurrent,
            prefs.download_retries,
            prefs.open_folder_when_done,
        )
        return prefs

    # ------------------------------------------------------------- 写
    def save(self, prefs: Preferences) -> bool:
        """原子写入偏好；失败只记日志（不打断用户操作）。"""
        target = self.path
        self._cached = prefs
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(prefs.to_dict(), fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            tmp.replace(target)  # 原子替换，避免写一半损坏配置
            _log.info("偏好已保存: %s", target)
            return True
        except OSError:
            _log.warning("偏好写入失败: %s", target, exc_info=True)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    # ------------------------------------------------------------- 测试支持
    def reset_cache(self) -> None:
        self._cached = None


# ---------------------------------------------------------------- 模块级便捷入口
_default_store: PreferenceStore | None = None


def store() -> PreferenceStore:
    """进程级默认存储。"""
    global _default_store
    if _default_store is None:
        _default_store = PreferenceStore()
    return _default_store


def reset_default_store() -> None:
    """丢弃默认存储及其缓存（测试、或运行时改了配置文件后调用）。"""
    global _default_store
    _default_store = None


def load() -> Preferences:
    return store().load()


def save(prefs: Preferences) -> bool:
    return store().save(prefs)


def update(**changes: Any) -> Preferences:
    """读出现有偏好、改几项、落盘并返回新偏好。"""
    prefs = load().copy_with(**changes)
    save(prefs)
    return prefs


def background_hint_dir() -> str:
    """选择背景图时的初始目录（取第一个存在的候选，否则用户主目录）。"""
    home = Path.home()
    for parts in _BACKGROUND_HINTS:
        candidate = home.joinpath(*parts)
        if candidate.is_dir():
            return str(candidate)
    return str(home)
