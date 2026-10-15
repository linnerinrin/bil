"""常驻后台事件循环 —— 本项目的结构性支点。

修复审计缺陷 B4 与 B7：

B4（会话泄漏）
    bilibili_api 的会话池按事件循环分桶：
        session_pool: Dict[str, Dict[asyncio.AbstractEventLoop, BiliAPIClient]]
    旧代码对「每一次网络操作」都调用一次 asyncio.run()，每次都新建并关闭一个
    event loop，于是每个 loop 都新建一个 aiohttp session 且永不关闭 —— 连接/FD 泄漏。
    这里改为全进程唯一一个 loop，从而复用同一个 session。

B7（UI 冻结）
    旧代码在 Tk 主线程里 asyncio.run(搜索/下载)，整个界面卡死。
    这里把协程提交到后台线程的 loop，UI 线程只通过回调更新界面。

用法：
    loop = BackgroundLoop.instance()
    loop.submit(coro, on_success=..., on_error=...)
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import Any, TypeVar

from ..utils.logger import get_logger

T = TypeVar("T")

_log = get_logger("loop")


class BackgroundLoop:
    """在专属守护线程上运行的常驻 asyncio 事件循环（进程内单例）。"""

    _instance: BackgroundLoop | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="bilidown-asyncio", daemon=True
        )
        self._thread.start()
        _log.debug("后台事件循环已启动")

    # ------------------------------------------------------------------ 单例
    @classmethod
    def instance(cls) -> BackgroundLoop:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def shutdown_instance(cls) -> None:
        with cls._instance_lock:
            inst = cls._instance
            cls._instance = None
        if inst is not None:
            inst.shutdown()

    # ------------------------------------------------------------- 内部实现
    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            # 清理挂起任务与未关闭的会话
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:  # noqa: BLE001 - 关停阶段不再抛错
                _log.debug("关闭挂起任务时出错", exc_info=True)

    # ----------------------------------------------------------------- 接口
    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def submit(
        self,
        coro: Coroutine[Any, Any, T],
        *,
        on_success: Callable[[T], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> Future[T]:
        """把协程提交到后台 loop，返回 concurrent.futures.Future。

        回调在**后台线程**执行；UI 层需自行用 root.after(0, ...) 切回主线程。
        """
        if self._loop.is_closed():
            raise RuntimeError("后台事件循环已关闭，无法提交任务")

        future: Future[T] = asyncio.run_coroutine_threadsafe(coro, self._loop)

        if on_success is not None or on_error is not None:

            def _done(fut: Future[T]) -> None:
                if fut.cancelled():
                    return
                exc = fut.exception()
                if exc is not None:
                    if on_error is not None:
                        try:
                            on_error(exc)
                        except Exception:  # noqa: BLE001
                            _log.exception("on_error 回调自身抛出异常")
                    else:
                        _log.error("后台任务失败: %r", exc, exc_info=exc)
                elif on_success is not None:
                    try:
                        on_success(fut.result())
                    except Exception:  # noqa: BLE001
                        _log.exception("on_success 回调自身抛出异常")

            future.add_done_callback(_done)

        return future

    def run_sync(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """同步等待一个协程完成（仅用于脚本/测试，UI 不得调用）。"""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def shutdown(self, timeout: float = 5.0) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
        if not self._loop.is_closed():
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                _log.debug("关闭事件循环失败", exc_info=True)
        _log.debug("后台事件循环已停止")
