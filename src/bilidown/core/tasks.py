#架构作用：登记任务、异步解析下载地址、限流执行并广播任务结果
"""下载任务编排：异步解析 + 信号量限流 + 取消/重试 + 完成回调

执行流程（两段式，避免在UI线程做网络解析）：
    1) prepare_async  在后台事件循环里解析cid / playurl
    2) _execute       在下载线程池中受限流并发执行下载与合并
"""

#延迟求值类型注解
from __future__ import annotations

#为准备阶段提供超时控制
import asyncio
#提供任务与配置更新的锁和信号量
import threading
#为任务完成回调声明类型
from collections.abc import Callable
#执行下载工作并保存准备任务future
from concurrent.futures import Future, ThreadPoolExecutor
#表示可变下载路径
from pathlib import Path

#读取默认并发数
from ..config.settings import MAX_CONCURRENT_DOWNLOADS
#构造模块日志记录器
from ..utils.logger import get_logger
#异步解析任务的后台事件循环
from .bridge import BackgroundLoop
#视频流解析客户端类型
from .client import BiliClient
#下载器与可读错误类型
from .downloader import DownloadError, MediaDownloader
#任务状态及取消控制流
from .models import DownloadCancelled, DownloadStatus, DownloadTask

#本模块的任务日志
_log = get_logger("tasks")

#解析下载地址的最长等待秒数，超时按解析失败处理，避免准备阶段无限挂起
PREPARE_TIMEOUT_SECONDS = 120

#任务结束后调用：on_settled(task)。在**工作线程/后台线程**中触发，
#UI层需自行切回主线程
SettleCallback = Callable[[DownloadTask], None]


#管理器持有异步解析与下载执行两个阶段的生命周期
class TaskManager:
    """管理下载任务的生命周期"""

    def __init__(
        self,
        client: BiliClient,
        download_dir: str | None = None,
        max_workers: int = MAX_CONCURRENT_DOWNLOADS,
        bridge: BackgroundLoop | None = None,
    ) -> None:
        self.client = client  #保存流解析客户端
        self.downloader = MediaDownloader(client, download_dir)  #创建下载执行器
        self.download_dir = self.downloader.download_dir  #暴露当前下载目录
        self._bridge = bridge or BackgroundLoop.instance()  #复用后台事件循环
        self._slots = threading.BoundedSemaphore(max(1, max_workers))  #限制在途下载
        self._executor = self._new_executor(max_workers)  #线程池规模与信号量保持一致
        self._tasks: list[DownloadTask] = []  #保留任务历史记录
        self._prep_futures: dict[str, Future[object]] = {}  #跟踪准备阶段任务
        self._task_dirs: dict[str, str] = {}  #每个任务提交时冻结的下载目录
        self._listeners: list[SettleCallback] = []  #注册终态监听器
        self._lock = threading.RLock()  #保护管理器共享字段
        self._shutdown = False  #标记管理器尚未关闭

    #按并发上限创建线程池，保证池规模与信号量名额一致
    @staticmethod
    def _new_executor(max_workers: int) -> ThreadPoolExecutor:
        return ThreadPoolExecutor(
            max_workers=max(1, max_workers), thread_name_prefix="bilidown-dl"
        )

    #------------------------------------------------------------- 观察者
    def add_listener(self, callback: SettleCallback) -> None:
        """注册「任务结束」回调（完成/失败/取消都会触发）"""
        with self._lock:  #串行注册监听器
            self._listeners.append(callback)  #保存回调供任务完成调用

    def _notify(self, task: DownloadTask) -> None:
        with self._lock:  #复制回调列表以免调用期间修改
            listeners = list(self._listeners)  #快照供锁外执行
        for cb in listeners:  #通知每位监听器
            try:  #隔离各监听器异常
                cb(task)  #传递已结束任务
            except Exception:  #noqa: BLE001
                _log.exception("任务监听器抛出异常")  #保留异常堆栈

    #------------------------------------------------------------- 任务操作
    #------------------------------------------------------------- 去重与提交
    def _find_duplicate(self, task: DownloadTask) -> DownloadTask | None:
        """查找同视频 + 同分P + 同格式 + 同清晰度的未结束任务"""
        with self._lock:  #避免任务列表在遍历中被修改
            for existing in self._tasks:  #逐条比较在途任务
                if existing.status.is_terminal:  #只判重未结束任务
                    continue  #跳过已结束记录
                #比较视频、格式、分页和音视频画质
                if (
                    existing.item.bvid == task.item.bvid
                    and existing.format is task.format
                    and existing.page_index == task.page_index
                    and existing.qn == task.qn
                    and existing.audio_qn == task.audio_qn
                ):
                    return existing  #找到已排队的等价任务
        return None  #无重复项

    def submit(self, task: DownloadTask) -> DownloadTask:
        """登记并排队一个任务（解析阶段即开始，下载阶段受限流）

        去重：同一个视频 + 同一个分P + 同一格式 + 同一清晰度，若已有未结束的
        任务，直接返回那一条，避免并发写同一个输出文件
        """
        return self._submit(task, dedupe=True)  #普通提交启用去重

    def _submit(self, task: DownloadTask, *, dedupe: bool) -> DownloadTask:
        #查重、关闭检查与登记在同一把锁内原子完成，并发提交不会绕过去重
        with self._lock:
            if self._shutdown:  #已关闭时不接收新任务
                raise RuntimeError("任务管理器已关闭")  #拒绝登记
            if dedupe:  #显式重试前已取消原任务，可安全走同一判重
                duplicate = self._find_duplicate(task)
                if duplicate is not None:  #已存在等价任务则直接返回
                    _log.info(
                        "重复任务已存在，跳过新增 %s P%d %s",
                        task.item.bvid,
                        task.page_index,
                        task.format.value,
                    )
                    return duplicate  #不生成第二条任务
            self._tasks.append(task)  #添加到任务历史
            #提交时冻结下载目录快照：之后set_download_dir只影响新任务
            self._task_dirs[task.task_id] = str(self.downloader.download_dir)

        if task.cancelled:  #提交前已取消则立即通知
            task.set_status(DownloadStatus.CANCELLED, "已取消（未开始）")  #更新状态
            with self._lock:  #清理不再执行的目录快照
                self._task_dirs.pop(task.task_id, None)
            self._notify(task)  #通知监听器
            return task  #不再解析

        #封面不解析媒体流，直接进下载队列
        if not task.format.needs_streams:
            self._execute(task, None)
            return task

        #准备阶段带独立期限，防止解析协程无限挂起
        future = self._bridge.submit(
            self._prepare_with_timeout(task),
            on_success=lambda streams, t=task: self._execute(t, streams),
            on_error=lambda exc, t=task: self._fail_prepare(t, exc),
        )
        with self._lock:  #记录解析任务用于取消
            if future.done():  #回调可能已先执行，避免留下过期记录
                self._prep_futures.pop(task.task_id, None)
            else:
                self._prep_futures[task.task_id] = future  #按任务ID保存future
        return task  #返回登记后的对象

    async def _prepare_with_timeout(self, task: DownloadTask) -> object:
        #包装解析协程，超时抛出TimeoutError并走统一失败路径
        return await asyncio.wait_for(
            self.downloader.prepare_async(task), timeout=PREPARE_TIMEOUT_SECONDS
        )

    def _execute(self, task: DownloadTask, streams: object) -> None:
        """解析成功后进入限流队列，在工作线程中执行下载"""
        with self._lock:  #清除已完成的解析future
            self._prep_futures.pop(task.task_id, None)  #释放该任务准备记录
            task_dir = self._task_dirs.pop(task.task_id, None)  #取出提交时冻结的目录
            notify = False
            if self._shutdown or task.cancelled:  #关闭或取消时不提交下载线程
                task.set_status(DownloadStatus.CANCELLED, "已取消")  #显示取消状态
                notify = True
            else:
                try:  #关闭检查与提交在同一把锁内，shutdown不会夹在中间
                    self._executor.submit(self._run, task, streams, task_dir)
                except RuntimeError as exc:  #线程池已关闭的兜底分支
                    task.fail(str(exc) or "任务管理器已关闭")  #以失败收尾而非遗留任务
                    notify = True
        if notify:
            self._notify(task)  #通知监听者

    def _fail_prepare(self, task: DownloadTask, exc: BaseException) -> None:
        with self._lock:  #清理解析任务记录
            self._prep_futures.pop(task.task_id, None)  #不再跟踪准备future
            self._task_dirs.pop(task.task_id, None)  #清理目录快照
        task.fail(str(exc) or exc.__class__.__name__)  #将异常转为可见错误
        _log.error("解析下载地址失败 %s: %s", task.item.bvid, exc)  #记录解析失败
        self._notify(task)  #广播失败

    #------------------------------------------------------------- 任务执行与终态通知
    def _run(self, task: DownloadTask, streams: object, task_dir: str | None = None) -> None:
        with self._slots:  #获取并发下载名额
            try:  #将下载异常映射为任务状态
                #传入提交时冻结的目录，避免目录变更导致产物跨目录
                path = self.downloader.download(task, streams, download_dir=task_dir)  #type: ignore[arg-type]
                _log.info("任务完成 %s -> %s", task.item.bvid, path)  #记录产物路径
            except DownloadCancelled:  #用户取消下载
                task.set_status(DownloadStatus.CANCELLED, "已取消")  #标记取消
                _log.info("任务已取消 %s", task.item.bvid)  #记录取消
            except DownloadError as exc:  #可读下载错误
                task.fail(str(exc))  #将错误展示给界面
                _log.error("任务失败 %s: %s", task.item.bvid, exc)  #记录失败
            except Exception as exc:  #noqa: BLE001 - 兜底，避免线程静默死亡
                task.fail(f"未预期的错误：{exc}")  #防止任务静默挂起
                _log.exception("任务异常 %s", task.item.bvid)  #记录完整堆栈
        self._notify(task)  #执行完成后通知监听器

    def cancel(self, task: DownloadTask) -> None:
        """请求取消。若尚未开始解析则直接取消"""
        task.cancel()  #设置任务取消标志
        with self._lock:  #查询正在准备的future
            future = self._prep_futures.get(task.task_id)  #获取准备任务
        if future is not None and future.cancel():  #尚未启动则直接撤销
            with self._lock:  #删除准备任务记录
                self._prep_futures.pop(task.task_id, None)  #清理future
            self._notify(task)  #立即广播取消
        else:  #已运行时等待下载检查点退出
            _log.info("已请求取消任务 %s", task.item.bvid)  #记录请求

    def retry(self, task: DownloadTask) -> DownloadTask:
        """重新入队一个新任务（相同视频/分P/格式/清晰度），保留历史记录

        重试是用户显式动作；若原任务尚未结束，先发出取消请求再入队，
        避免新旧任务并发写同一份临时缓存
        """
        if not task.status.is_terminal:  #原任务仍在运行
            _log.info("重试前取消未结束任务 %s", task.item.bvid)  #记录先取消再重试
            self.cancel(task)  #先请求原任务退出再入队新任务
        #保留原任务的格式、画质和分P选择
        fresh = DownloadTask(
            item=task.item,
            kind=task.kind,
            format=task.format,
            qn=task.qn,
            audio_qn=task.audio_qn,
            page_index=task.page_index,
        )
        #原任务已取消或已终态，判重不会命中它，可安全走同一去重路径
        return self._submit(fresh, dedupe=True)

    def remove(self, task: DownloadTask) -> None:
        """从列表中移除记录（不删除已下载文件）"""
        self.cancel(task)  #移除前通知任务取消
        with self._lock:  #修改任务列表
            if task in self._tasks:  #避免重复移除报错
                self._tasks.remove(task)  #删除展示记录

    def clear_finished(self) -> int:
        """移除所有已结束的任务，返回移除数量"""
        with self._lock:  #在同一锁内筛选并删除
            done = [t for t in self._tasks if t.status.is_terminal]  #取终态快照
            for t in done:  #删除所有终态任务
                self._tasks.remove(t)  #移除一条记录
        return len(done)  #返回删除数量

    #------------------------------------------------------------- 查询
    def tasks(self) -> list[DownloadTask]:
        """返回任务快照（拷贝，避免并发修改）"""
        with self._lock:  #保护任务列表读取
            return list(self._tasks)  #返回独立快照

    def snapshot_counts(self) -> dict[str, int]:
        """各类状态计数，供状态栏显示"""
        counts: dict[str, int] = {}  #状态文字到数量的映射
        for t in self.tasks():  #遍历任务快照
            key = t.status.value  #使用状态中文值为键
            counts[key] = counts.get(key, 0) + 1  #累加状态计数
        return counts  #返回汇总

    def active_count(self) -> int:
        return sum(1 for t in self.tasks() if not t.status.is_terminal)  #统计未结束任务

    #------------------------------------------------------------- 运行时配置
    def set_download_dir(self, download_dir: str) -> None:
        """更新下载目录

        只影响**之后新建**的任务；任务提交时已冻结目录快照，
        已在下载或排队中的任务保留原目录
        """
        self.downloader.download_dir = Path(download_dir)  #更新后续任务的共享根目录
        self.download_dir = self.downloader.download_dir  #同步管理器公开路径

    def set_max_workers(self, count: int) -> None:
        """调整并发上限

        同一把锁内重建信号量与线程池，使实际并发始终等于配置值；
        已在执行的任务继续跑完，新任务按新上限排队
        """
        safe = max(1, int(count))  #至少允许一个下载
        with self._lock:  #序列化信号量与线程池的整体替换
            self._slots = threading.BoundedSemaphore(safe)  #让后续执行者领取新名额
            old_executor = self._executor  #保留旧池供在途任务跑完
            self._executor = self._new_executor(safe)  #池规模与新上限一致
        old_executor.shutdown(wait=False, cancel_futures=False)  #在途任务不受影响
        _log.info("并发上限已调整为 %d", safe)  #记录新限制

    #------------------------------------------------------------- 生命周期
    #------------------------------------------------------------- 关闭与资源回收
    def shutdown(self, wait: bool = False) -> None:
        with self._lock:  #串行进入关闭状态
            if self._shutdown:  #避免重复关闭
                return  #保持幂等
            self._shutdown = True  #拒绝后续提交
            tasks = list(self._tasks)  #快照已有任务
        for t in tasks:  #逐个请求取消
            if not t.status.is_terminal:  #不改变已结束任务
                t.cancel()  #标记取消事件
        self._executor.shutdown(wait=wait, cancel_futures=True)  #关闭线程池
        try:  #尝试释放下载会话
            self.downloader.close()  #关闭或延迟关闭会话
        except Exception:  #noqa: BLE001
            _log.debug("关闭下载会话失败", exc_info=True)  #记录清理异常
        _log.debug("任务管理器已关闭")  #记录关闭完成
