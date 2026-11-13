#架构作用：负责用户偏好的标准化、配置文件读写与运行时默认存储
"""用户偏好：背景、下载目录、并发与重试等运行时设置

- 只用一个配置文件：``%LOCALAPPDATA%\\bilidown\\config.json``
- 文件缺失自动生成、损坏回退默认值、越界值自动纠正
- 未识别字段读取时取默认值、写入时保留
- 校验集中在``normalize``，文件加载与界面提交走同一套规则
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
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

#边界值：界面与文件读取共用，避免两处各写一套
CONCURRENT_RANGE = (1, 5)
RETRIES_RANGE = (1, 5)
DEFAULT_CONCURRENT = 2
DEFAULT_RETRIES = 3

#背景图支持的格式（供文件对话框使用）
BACKGROUND_FILETYPES: tuple[tuple[str, str], ...] = (
    ("图片文件", "*.jpg *.jpeg *.png *.bmp *.webp"),
    ("所有文件", "*.*"),
)

#较短别名
BackgroundFiletypes = BACKGROUND_FILETYPES
BACKGROUND_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})

#备选背景目录（选择图片时作为初始目录），实际存在的才会用
_BACKGROUND_HINTS = (
    ("Pictures", "图片"),
    ("Pictures", "Screenshots"),
    ("Downloads",),
    ("Desktop",),
)


#数值校验：把来自文件和界面的并发/重试参数裁剪至允许范围
def _clamp(value: Any, low: int, high: int, default: int) -> int:  #将数字输入限制在有效边界内
    """把任意输入夹到 [low, high]；无法解析则用默认值"""
    try:
        number = int(value)  #int(float('inf'))会抛OverflowError，同样按无效输入处理
    except (TypeError, ValueError, OverflowError):  #输入不可转换或溢出时选取默认值
        return default
    return max(low, min(high, number))  #返回裁剪后的整数


#布尔校验：识别配置文本与原生布尔/数值形式
def _as_bool(value: Any, default: bool = False) -> bool:  #解析配置中的布尔样式值
    if isinstance(value, bool):  #原生布尔值无需转换
        return value
    if isinstance(value, str):  #仅接受明确列出的真值文本
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):  #数值按Python真值规则处理
        return bool(value)
    return default  #未知类型使用调用方默认值


#偏好模型：统一字段默认值、校验及字典转换
@dataclass(slots=True)
class Preferences:
    """全部用户偏好。字段顺序即配置文件里的书写顺序"""

    #自定义背景图路径；空字符串表示使用程序内置背景
    background_path: str = ""
    #下载目录；空字符串表示使用默认目录（~/Downloads/bilidown）
    download_dir: str = ""
    max_concurrent: int = DEFAULT_CONCURRENT
    download_retries: int = DEFAULT_RETRIES
    open_folder_when_done: bool = False
    use_system_proxy: bool = False
    proxy: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    #------------------------------------------------------------- 校验
    @classmethod
    def normalize(cls, raw: Any) -> Preferences:  #校验原始配置并构造偏好模型
        """从任意dict-like构造出合法偏好（未知字段保留在extras里）"""
        if not isinstance(raw, dict):  #非字典输入统一当作空配置
            raw = {}

        background = str(raw.get("background_path") or "").strip()  #读取并清理背景配置文本
        if is_builtin_ref(background):  #内置背景引用需要检查资源名
            #内置背景引用：确认那个资源确实存在，否则回退默认内置背景
            name = parse_builtin_ref(background)  #提取内置背景资源名称
            if name not in list_builtin_backgrounds():  #配置引用缺失资源时回退默认背景
                _log.info("配置中的内置背景不存在，改用默认内置背景: %s", name)  #留下自动回退记录
                background = ""  #空值表示使用程序内置背景
        elif background and not Path(background).is_file():  #自定义背景路径不存在时回退默认图
            #自定义背景被删除/改名时忽略它，回退内置背景，避免每次启动都报错
            _log.info("配置中的背景图不存在，改用内置背景: %s", background)  #记录被忽略的自定义路径
            background = ""  #回退到程序内置背景

        download_dir = str(raw.get("download_dir") or "").strip()  #清理用户指定的下载目录

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

    #------------------------------------------------------------- 派生
    def effective_download_dir(self) -> Path:
        """实际使用的下载目录（已展开 ``~`` 与环境变量）"""
        if not self.download_dir:
            return default_download_dir()
        try:
            return Path(self.download_dir).expanduser()
        except (RuntimeError, ValueError):  #pragma: no cover - 极端的畸形路径
            _log.warning("下载目录无法解析，使用默认目录: %r", self.download_dir)
            return default_download_dir()

    def to_dict(self) -> dict[str, Any]:
        """转成用于落盘的dict（含未识别字段，保证不丢用户数据）"""
        data = asdict(self)
        extras = data.pop("extras", {}) or {}
        data.update(extras)
        return data

    def copy_with(self, **changes: Any) -> Preferences:
        """基于当前值生成一个改了几项的副本（用于「应用」按钮）"""
        data = self.to_dict()
        data.update(changes)
        return Preferences.normalize(data)


#文件存储：缓存偏好并读写单一JSON配置文件
class PreferenceStore:
    """偏好读写（可指定路径，便于测试）"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._explicit = Path(path) if path is not None else None
        self._cached: Preferences | None = None
        self._save_lock = threading.Lock()  #串行化保存，避免并发写互相干扰

    #------------------------------------------------------------- 路径
    @property
    def path(self) -> Path:
        if self._explicit is not None:
            return self._explicit
        return ensure_user_data_dir() / CONFIG_FILE

    #------------------------------------------------------------- 读
    def load(self, *, force: bool = False) -> Preferences:
        """读取偏好；文件缺失/损坏都返回默认值，并尽力补写一份默认文件"""
        if self._cached is not None and not force:  #常规读取优先复用内存缓存
            return self._cached

        target = self.path  #解析显式路径或用户配置目录
        try:  #文件存在时按JSON读取配置
            with open(target, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:  #首次运行创建默认配置
            prefs = Preferences()
            self._cached = prefs
            self.save(prefs)  #保存失败时仍返回内存默认值
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

    #------------------------------------------------------------- 写
    def save(self, prefs: Preferences) -> bool:
        """原子写入偏好；失败只记日志（不打断用户操作）"""
        target = self.path
        with self._save_lock:  #串行化保存，独占临时文件，避免并发互覆
            tmp_path: Path | None = None
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                #在同目录创建独占临时文件，保证原子替换且不与其他写入冲突
                fd, tmp_name = tempfile.mkstemp(
                    dir=target.parent, prefix=target.stem + "-", suffix=".tmp"
                )
                tmp_path = Path(tmp_name)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(prefs.to_dict(), fh, ensure_ascii=False, indent=2)
                    fh.write("\n")
                os.replace(tmp_path, target)  #原子替换，避免写一半损坏配置
                tmp_path = None
                self._cached = prefs  #写盘成功后再更新缓存，保证与磁盘一致
                _log.info("偏好已保存: %s", target)
                return True
            except OSError:
                _log.warning("偏好写入失败: %s", target, exc_info=True)
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)  #只清理本次自己的临时文件
                    except OSError:
                        pass
                return False

    #------------------------------------------------------------- 测试支持
    def reset_cache(self) -> None:
        self._cached = None


#---------------------------------------------------------------- 模块级便捷入口
#默认存储入口：共享进程级偏好缓存并提供便捷读写函数
_default_store: PreferenceStore | None = None  #首次调用时惰性创建


def store() -> PreferenceStore:
    """进程级默认存储"""
    global _default_store
    if _default_store is None:
        _default_store = PreferenceStore()
    return _default_store


def reset_default_store() -> None:
    """丢弃默认存储及其缓存（测试、或运行时改了配置文件后调用）"""
    global _default_store
    _default_store = None


def load() -> Preferences:
    return store().load()


def save(prefs: Preferences) -> bool:
    return store().save(prefs)


def update(**changes: Any) -> Preferences:
    """读出现有偏好、改几项、落盘并返回新偏好"""
    prefs = load().copy_with(**changes)
    if not save(prefs):  #落盘失败时留下记录，返回值仅代表内存状态
        _log.warning("偏好更新未能写入磁盘，仅本次运行生效: %s", store().path)
    return prefs


def background_hint_dir() -> str:
    """选择背景图时的初始目录（取第一个存在的候选，否则用户主目录）"""
    home = Path.home()
    for parts in _BACKGROUND_HINTS:
        candidate = home.joinpath(*parts)
        if candidate.is_dir():
            return str(candidate)

    return str(home)  #候选均不存在时退回用户目录
