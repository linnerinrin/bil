"""统一日志。

修复审计缺陷 B9（异常被静默吞掉、无任何日志）：
  - 文件日志写入用户数据目录，带轮转，打包后仍可用；
  - 控制台日志在 windowed exe（无 stderr）下自动跳过，避免二次崩溃。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from ..config.paths import ensure_user_data_dir

_LOGGER_NAME = "bilidown"
_configured = False

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(level: int = logging.INFO, *, force: bool = False) -> logging.Logger:
    """初始化根日志器（幂等）。返回 bilidown 日志器。"""
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured and not force:
        return logger

    logger.setLevel(level)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()

    formatter = logging.Formatter(_FMT)

    # 文件日志（始终启用）
    try:
        log_path = ensure_user_data_dir() / "bilidown.log"
        fh = RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except OSError:
        # 磁盘只读等极端情况：不因为日志而中断程序
        pass

    # 控制台日志（无 stderr 时跳过 —— 打包 windowed 模式）
    stream = None
    try:
        candidate = sys.stderr
        if candidate is not None:
            candidate.fileno()  # windowed 模式下会抛 OSError/ValueError
            stream = candidate
    except (AttributeError, ValueError, OSError):
        stream = None
    if stream is not None:
        sh = logging.StreamHandler(stream)
        sh.setFormatter(formatter)
        logger.addHandler(sh)

    _configured = True
    logger.debug("日志系统初始化完成")
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """获取子日志器，例如 get_logger("downloader")。"""
    root = logging.getLogger(_LOGGER_NAME)
    if not _configured:
        setup_logging()
    return root if not name else root.getChild(name)
