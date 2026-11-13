#架构作用：为同步控制器提供复用的后台asyncio循环与线程间提交接口
"""常驻后台事件循环

全进程唯一一个loop，复用bilibili_api会话；协程提交到后台线程执行，
UI线程只通过回调更新界面

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


#线程桥接：维护进程内单例，并在专属线程执行协程
class BackgroundLoop:
    """在专属守护线程上运行的常驻asyncio事件循环（进程内单例）"""

    _instance: BackgroundLoop | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()  #创建专属于后台线程的事件循环
        self._thread = threading.Thread(  #用守护线程持续运行异步任务
            target=self._run, name="bilidown-asyncio", daemon=True
        )
        self._thread.start()  #启动线程，后续请求共用同一循环
        _log.debug("后台事件循环已启动")

    #------------------------------------------------------------------ 单例
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

    #------------------------------------------------------------- 内部实现
    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            #关停顺序：取消挂起任务 → 关闭第三方会话 → 收尾异步生成器与默认执行器
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                self._loop.run_until_complete(self._close_third_party_sessions())
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                self._loop.run_until_complete(self._loop.shutdown_default_executor())
            except Exception:  #noqa: BLE001 - 关停阶段不再抛错
                _log.debug("关停后台循环时出错", exc_info=True)

    async def _close_third_party_sessions(self) -> None:
        """关闭bilibili_api会话池中绑定到本loop的aiohttp/httpx会话"""
        try:
            from bilibili_api.utils.network import session_pool  #延迟导入，避免硬依赖
        except ImportError:  #pragma: no cover - 未安装第三方库时无可关闭
            return
        for pool in session_pool.values():  #按客户端类型分桶（httpx / aiohttp）
            client = pool.pop(self._loop, None)
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  #noqa: BLE001 - 单个会话失败不影响其余关停
                    _log.debug("关闭第三方会话失败", exc_info=True)

    #----------------------------------------------------------------- 接口
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
        """把协程提交到后台loop，返回concurrent.futures.Future

        回调在**后台线程**执行；UI层需自行用root.after(0, ...)切回主线程
        """
        if self._loop.is_closed():  #拒绝向已关闭的循环继续排任务
            raise RuntimeError("后台事件循环已关闭，无法提交任务")

        future: Future[T] = asyncio.run_coroutine_threadsafe(coro, self._loop)  #跨线程排入协程并保留Future

        if on_success is not None or on_error is not None:  #仅在调用方提供处理器时安装回调

            def _done(fut: Future[T]) -> None:  #完成回调运行在线程桥接侧，UI更新须另切主线程
                if fut.cancelled():  #已取消时跳过成功与失败处理
                    return
                exc = fut.exception()  #获取异步操作的失败原因
                if exc is not None:  #根据完成状态转发给错误处理器
                    if on_error is not None:
                        try:
                            on_error(exc)
                        except Exception:  #noqa: BLE001
                            _log.exception("on_error 回调自身抛出异常")
                    else:
                        _log.error("后台任务失败: %r", exc, exc_info=exc)
                elif on_success is not None:
                    try:
                        on_success(fut.result())
                    except Exception:  #noqa: BLE001
                        _log.exception("on_success 回调自身抛出异常")

            future.add_done_callback(_done)

        return future

    def run_sync(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """同步等待一个协程完成（仅用于脚本/测试，UI不得调用）"""
        future: Future[T] = asyncio.run_coroutine_threadsafe(coro, self._loop)  #保留Future以便超时后取消
        try:
            return future.result(timeout)
        except TimeoutError:  #超时仅停止等待不够，还需取消仍在后台运行的协程
            future.cancel()  #避免协程在调用方放弃后继续访问已退出的资源
            raise

    def shutdown(self, timeout: float = 5.0) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():  #join超时：线程仍在运行，close会与运行中的loop冲突
            _log.warning("后台事件循环线程未在 %.1f 秒内退出，跳过 close", timeout)
            return
        if not self._loop.is_closed():
            try:
                self._loop.close()
            except Exception:  #noqa: BLE001
                _log.debug("关闭事件循环失败", exc_info=True)
        _log.debug("后台事件循环已停止")  #记录停止流程已执行
