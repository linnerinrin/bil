#架构作用：定义视频元数据、下载格式与跨线程共享的任务状态模型
"""领域模型：搜索结果、下载任务、状态枚举"""

#延迟求值类型注解
from __future__ import annotations

#定义状态、类型与格式枚举
import enum
#为跨线程状态提供锁和取消事件
import threading
#为任务记录创建时间
import time
#生成任务标识
import uuid
#构造数据对象及其默认字段
from dataclasses import dataclass, field


#状态枚举及终态判定，供任务编排层筛选执行对象
class DownloadStatus(enum.Enum):
    """下载任务状态机"""

    PENDING = "排队中"  #尚在队列或地址解析阶段
    DOWNLOADING = "下载中"  #正在拉取媒体流
    MERGING = "合并中"  #正在合并或转码媒体
    COMPLETED = "完成"  #下载产物已经生成
    FAILED = "失败"  #执行过程中出现错误
    CANCELLED = "已取消"  #用户已发出取消请求

    #读取状态是否已经结束
    @property
    def is_terminal(self) -> bool:
        #三种状态均不再计入活动任务
        return self in (
            DownloadStatus.COMPLETED,
            DownloadStatus.FAILED,
            DownloadStatus.CANCELLED,
        )


#下载类型及具体产物格式
class DownloadKind(enum.Enum):
    """下载类型（视频/纯音频两大类）"""

    VIDEO = "video"  #视频+音频，合并为mp4
    AUDIO = "audio"  #仅音频


class DownloadFormat(enum.Enum):
    """下载产物格式

    VIDEO/COVER走不同流程；AUDIO_MP3先下m4a再用ffmpeg转码
    """

    VIDEO = "mp4"  #合并的视频文件
    AUDIO = "m4a"  #原声音频文件
    AUDIO_MP3 = "mp3"  #MP3转码文件
    AUDIO_WAV = "wav"  #WAV转码文件
    COVER = "jpg"  #视频封面文件

    #给界面显示格式名称
    @property
    def label(self) -> str:
        #按枚举值映射到中文名称
        return {
            "mp4": "视频（MP4）",
            "m4a": "音频（M4A 原声）",
            "mp3": "音频（MP3 转码）",
            "wav": "音频（WAV 无损转码）",
            "jpg": "封面（JPG）",
        }[self.value]

    #区分音频产物与视频、封面
    @property
    def is_audio(self) -> bool:
        #三类音频格式统一走音频下载分支
        return self in (
            DownloadFormat.AUDIO,
            DownloadFormat.AUDIO_MP3,
            DownloadFormat.AUDIO_WAV,
        )

    #判断是否必须请求媒体流地址
    @property
    def needs_streams(self) -> bool:
        """是否需要解析音视频流（封面不需要）"""
        #封面直接使用元数据中的图片地址
        return self is not DownloadFormat.COVER


#视频分页与搜索结果的不可共享值字段
@dataclass(slots=True)
class VideoPage:
    """一个分P"""

    index: int  #分P的序号
    cid: int  #分P对应的内容ID
    title: str = ""  #分P的显示标题
    duration: int = 0  #分P时长

    #组合序号和可选标题
    def display(self) -> str:
        head = f"P{self.index}"  #固定的序号前缀
        if self.title:  #有标题时一并显示
            return f"{head}｜{self.title}"  #合并序号与标题
        return head  #无标题时仅返回序号


@dataclass(slots=True)
class VideoItem:
    """一条视频信息（搜索结果或BV直查结果）"""

    bvid: str  #视频的BV标识
    cid: int  #视频的内容ID
    title: str  #视频标题
    author: str = ""  #投稿作者
    play: int = 0  #播放量
    danmaku: int = 0  #弹幕数量
    duration: int = 0  #视频时长
    cover: str = ""  #封面图片地址
    pages: list[VideoPage] = field(default_factory=list)  #独立的分P列表

    #供复制功能生成摘要文本
    def copy_text(self) -> str:
        #按固定顺序拼接视频基本信息
        return (
            f"BV号：{self.bvid}\n"
            f"标题：{self.title}\n"
            f"UP主：{self.author}\n"
            f"播放量：{self.play}\n"
            f"弹幕数：{self.danmaku}"
        )


#跨线程任务数据：锁保护状态读写，事件传播取消请求
@dataclass
class DownloadTask:
    """一个下载任务

    进度由工作线程写入、UI线程读取，因此所有可变字段的访问都走锁
    """

    item: VideoItem  #所属视频元数据
    kind: DownloadKind = DownloadKind.VIDEO  #下载类型
    format: DownloadFormat = DownloadFormat.VIDEO  #实际产物格式
    qn: int = 0  #0 = 该视频可选里最高清晰度
    audio_qn: int = 0  #0 = 最高音质；30280/30232/30216 = 高/中/低
    page_index: int = 1  #分P序号（从1开始）
    save_path: str = ""  #下载产物的目标路径
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])  #短任务标识
    created_at: float = field(default_factory=time.time)  #创建时间戳

    _status: DownloadStatus = field(default=DownloadStatus.PENDING, repr=False)  #当前状态
    _progress: float = field(default=0.0, repr=False)  #下载进度
    _message: str = field(default="", repr=False)  #当前提示
    _error: str = field(default="", repr=False)  #最近错误
    #同一任务的状态和进度共用可重入锁
    _lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False
    )
    #事件让下载线程检测取消请求
    _cancel: threading.Event = field(
        default_factory=threading.Event, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """只传kind=AUDIO时，格式自动落到M4A音频"""
        if self.kind is DownloadKind.AUDIO and self.format is DownloadFormat.VIDEO:  #仅传音频类型时
            self.format = DownloadFormat.AUDIO  #将音频类型映射为M4A

    #------------------------------------------------------------- 只读属性
    #读取当前状态
    @property
    def status(self) -> DownloadStatus:
        with self._lock:  #防止与工作线程写入交错
            return self._status  #返回锁内状态快照

    #读取下载进度
    @property
    def progress(self) -> float:
        """0-100；失败时为-1"""
        with self._lock:  #防止读取时与更新交错
            return self._progress  #返回当前进度

    #读取任务提示
    @property
    def message(self) -> str:
        with self._lock:  #防止与状态更新交错
            return self._message  #返回当前提示

    #读取任务错误
    @property
    def error(self) -> str:
        with self._lock:  #防止与失败处理交错
            return self._error  #返回错误描述

    #读取线程安全的取消标志
    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()  #事件置位即为请求取消

    #--------------------------------------------------------------- 写方法
    #更新状态及可选消息
    def set_status(self, status: DownloadStatus, message: str = "") -> None:
        with self._lock:  #串行更新状态字段
            if self._status.is_terminal:  #终态不再迁移，已结束任务不得复活
                return  #忽略迟到的工作线程写入
            if self._cancel.is_set() and status is not DownloadStatus.CANCELLED:
                #取消请求优先于迟到写入：统一落为取消终态
                self._status = DownloadStatus.CANCELLED  #以取消为最终状态
                self._message = "用户取消"  #展示取消原因
                return
            self._status = status  #写入新的任务状态
            if message:  #空消息不覆盖现有说明
                self._message = message  #更新状态说明

    #将常规进度限制在百分比范围
    def set_progress(self, value: float) -> None:
        with self._lock:  #串行更新进度
            self._progress = max(0.0, min(100.0, float(value)))  #限制在0到100

    #将执行错误写入状态及提示
    def fail(self, error: str) -> None:
        with self._lock:  #一次性写入失败信息
            if self._status.is_terminal:  #已结束任务（含已取消）不再改写
                return  #忽略迟到的失败收尾
            if self._cancel.is_set():  #取消请求先于失败到达
                #取消请求优先：以取消为最终状态而非失败
                self._status = DownloadStatus.CANCELLED  #以取消为最终状态
                self._message = "用户取消"  #展示取消原因
                return
            self._status = DownloadStatus.FAILED  #记录失败终态
            self._error = error  #保存错误文本
            self._message = error  #向界面展示错误
            self._progress = -1.0  #失败时使用专属进度值

    #标记取消并通知状态观察者
    def cancel(self) -> None:
        """请求取消。工作线程在分块循环中检查该标志"""
        #先广播取消事件，再在锁内落为取消终态
        self._cancel.set()  #广播取消请求给工作线程
        with self._lock:  #同步修改可见状态
            if not self._status.is_terminal:  #已结束任务不再改写状态
                self._status = DownloadStatus.CANCELLED  #将任务设为取消
                self._message = "用户取消"  #展示取消原因

    #为执行线程提供取消检查点
    def check_cancelled(self) -> None:
        """供工作线程调用；已取消则抛出DownloadCancelled"""
        if self._cancel.is_set():  #读取取消事件
            raise DownloadCancelled(self.task_id)  #终止当前下载流程

    #根据失败、合并和进度生成状态文字
    def progress_text(self) -> str:
        p = self.progress  #获取线程安全的进度
        if p < 0:  #负值代表任务失败
            return "失败"  #显示失败提示
        if self.status is DownloadStatus.MERGING:  #合并状态优先于百分比
            return "合并中"  #显示合并提示
        return f"{p:.0f}%"  #正常显示整数百分比

    #使用任务ID作为哈希依据
    def __hash__(self) -> int:
        return hash(self.task_id)  #同ID任务哈希相同

    #仅比较同类对象的任务ID
    def __eq__(self, other: object) -> bool:
        return isinstance(other, DownloadTask) and other.task_id == self.task_id  #按ID判等


#取消信号作为内部控制流异常向执行器传播
class DownloadCancelled(Exception):
    """任务被取消（内部控制流）"""

    #构造携带任务ID的取消异常
    def __init__(self, task_id: str) -> None:
        super().__init__(f"任务 {task_id} 已取消")  #生成可读异常信息
        self.task_id = task_id  #保留取消任务标识
