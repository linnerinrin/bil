"""后台事件循环桥接测试（修复 B4 的核心组件）。"""

from __future__ import annotations

import asyncio
import threading
import time

from bilidown.core.bridge import BackgroundLoop


class TestBackgroundLoop:
    def test_run_sync_returns_result(self) -> None:
        loop = BackgroundLoop()
        try:
            assert loop.run_sync(asyncio.sleep(0, result=42)) == 42
        finally:
            loop.shutdown()

    def test_loop_is_single_and_stable(self) -> None:
        """B4 回归：所有任务必须跑在同一个 loop 上。"""
        loop = BackgroundLoop()

        async def current() -> object:
            return asyncio.get_running_loop()

        try:
            first = loop.run_sync(current())
            second = loop.run_sync(current())
            assert first is second is loop.loop
        finally:
            loop.shutdown()

    def test_runs_in_separate_thread(self) -> None:
        """界面线程不应被封装进去 —— loop 必须属于后台线程。"""
        loop = BackgroundLoop()
        main_thread = threading.current_thread().name

        async def worker_thread_name() -> str:
            thread = await asyncio.to_thread(threading.current_thread)
            return thread.name

        try:
            worker = loop.run_sync(worker_thread_name())
            assert worker != main_thread
        finally:
            loop.shutdown()

    def test_submit_success_callback(self) -> None:
        loop = BackgroundLoop()
        try:
            results: list[int] = []
            done = threading.Event()

            def on_success(value: int) -> None:
                results.append(value)
                done.set()

            loop.submit(asyncio.sleep(0, result=7), on_success=on_success)
            assert done.wait(5), "回调未被触发"
            assert results == [7]
        finally:
            loop.shutdown()

    def test_submit_error_callback(self) -> None:
        loop = BackgroundLoop()
        try:
            errors: list[BaseException] = []
            done = threading.Event()

            async def boom() -> None:
                raise ValueError("boom")

            def on_error(exc: BaseException) -> None:
                errors.append(exc)
                done.set()

            loop.submit(boom(), on_error=on_error)
            assert done.wait(5), "错误回调未被触发"
            assert isinstance(errors[0], ValueError)
        finally:
            loop.shutdown()

    def test_callback_exception_does_not_break_loop(self) -> None:
        loop = BackgroundLoop()
        try:
            done = threading.Event()

            def bad_callback(_value: int) -> None:
                raise RuntimeError("回调自身出错")

            loop.submit(asyncio.sleep(0, result=1), on_success=bad_callback)
            time.sleep(0.3)
            # loop 必须仍然可用
            assert loop.run_sync(asyncio.sleep(0, result="alive")) == "alive"
            done.set()
        finally:
            loop.shutdown()

    def test_submit_after_shutdown_raises(self) -> None:
        loop = BackgroundLoop()
        loop.shutdown()

        async def work() -> None:
            return None

        try:
            loop.submit(work())
        except RuntimeError:
            pass
        else:  # pragma: no cover
            raise AssertionError("关闭后提交应当抛 RuntimeError")

    def test_singleton_instance(self) -> None:
        first = BackgroundLoop.instance()
        second = BackgroundLoop.instance()
        assert first is second
        BackgroundLoop.shutdown_instance()
