#架构作用：解析后的流在此分块下载、断点续传、合并/转码并落盘
"""音视频下载器：分块下载 + 断点续传 + 重试退避 + 合并

临时文件按「BV+分P+格式+画质/音质」命名；音频流使用.m4a扩展名；
失败时保留断点文件并可取消"""

#延迟解析类型注解
from __future__ import annotations

#文件更名与路径状态检查
import os
#匹配续传响应的Content-Range头
import re
#检查下载磁盘剩余空间
import shutil
#会话创建、活跃计数与关闭的线程同步
import threading
#下载重试退避等待
import time
#构造下载与临时文件路径
from pathlib import Path

#发起媒体流HTTP请求
import requests

#确定默认下载根目录
from ..config.paths import ensure_download_dir
#下载超时、重试、分块及请求标识配置
from ..config.settings import (
    DOWNLOAD_BACKOFF_BASE,
    DOWNLOAD_CHUNK_SIZE,
    DOWNLOAD_RETRIES,
    DOWNLOAD_TIMEOUT,
    USER_AGENT,
)
#初始化模块日志
from ..utils.logger import get_logger
#读取运行时偏好设置
from . import preferences as preferences_mod
#解析下载流并清理文件名
from .client import BiliClient, ClientError, StreamSelection, sanitize_filename
#表示任务格式、状态及取消异常
from .models import DownloadCancelled, DownloadFormat, DownloadStatus, DownloadTask
#创建请求会话并定位网络配置
from .network import config_path, create_session

#下载器日志记录器
_log = get_logger("downloader")

#下载线程局部上下文：让三参数的merge_video_audio也能拿到当前任务用于取消
_local = threading.local()

#下载前要求的最小剩余空间（MB），低于它就提前报错
MIN_FREE_SPACE_MB = 200


#动态下载配置：重试次数与完成后定位目录
def _current_retries() -> int:
    """当前生效的重试次数（可被设置页调整，读不到就用默认值）"""
    try:  #读取可能随设置页改变的次数
        return int(preferences_mod.store().load().download_retries)  #转为整数
    except Exception:  #noqa: BLE001 - 配置不可用时退回默认值
        return DOWNLOAD_RETRIES  #回退到内置次数


#查询完成后是否打开目标文件夹
def _open_folder_when_done() -> bool:
    try:  #读取偏好设置
        return bool(preferences_mod.store().load().open_folder_when_done)  #转为布尔值
    except Exception:  #noqa: BLE001
        return False  #设置不可用时不打开文件夹

#进度权重：视频轨0-70，音频轨70-95，合并95-100
_VIDEO_STAGE = (0.0, 70.0)  #视频流进度区间
_AUDIO_STAGE = (70.0, 95.0)  #音频流进度区间
MERGE_PROGRESS = 96.0  #开始合并时的进度


class DownloadError(RuntimeError):
    """下载阶段的可读错误"""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)  #可读错误文本
        #永久错误（如4xx鉴权/资源失效）不再原地重试，直接交由备用地址回退
        self.permanent = permanent


#音视频轨合并及外部ffmpeg/moviepy回退
def merge_video_audio(
    video_path: str,
    audio_path: str,
    output_path: str,
    task: DownloadTask | None = None,
) -> None:
    """把分离的视频轨与音频轨合并为mp4

    优先使用ffmpeg流拷贝（快且无损），不可用时回退到moviepy重编码
    传入task时，ffmpeg运行期间可响应取消（杀进程并抛DownloadCancelled）
    省略task时从线程局部上下文取当前任务，保持三参数调用兼容"""
    if task is None:  #兼容只传三参数的调用方
        task = getattr(_local, "task", None)  #读取当前下载线程的任务
    if _merge_with_ffmpeg(video_path, audio_path, output_path, task):  #优先做无损流拷贝
        return  #已合并成功
    if task is not None:  #回退重编码前再确认一次取消
        task.check_cancelled()  #已取消则不再启动慢速重编码
    _merge_with_moviepy(video_path, audio_path, output_path)  #失败时重编码回退


#优先通过ffmpeg合并视频轨和音频轨
def _merge_with_ffmpeg(
    video_path: str,
    audio_path: str,
    output_path: str,
    task: DownloadTask | None = None,
) -> bool:
    """ffmpeg -c copy合并。返回是否成功。task非空时合并过程可取消"""
    try:  #ffmpeg包可能尚未安装
        from imageio_ffmpeg import get_ffmpeg_exe  #type: ignore[import-untyped]
    except ImportError:  #不可用时启用备用合并器
        return False  #告知上层尝试moviepy

    ffmpeg = get_ffmpeg_exe()  #获取ffmpeg可执行文件路径
    if not ffmpeg:  #未找到可执行文件
        return False  #尝试备用合并器

    import subprocess  #仅在调用ffmpeg时加载进程模块

    #指定输入轨道及无需重新编码的目标文件
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel", "error",
        "-i", video_path,
        "-i", audio_path,
        "-c", "copy",
        "-map", "0:v:0",
        "-map", "1:a:0",
        output_path,
    ]
    try:  #启动可持续监视的合并进程（Popen才能在中途杀掉）
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.SubprocessError):  #启动ffmpeg失败
        _log.warning("ffmpeg 启动失败，回退 moviepy", exc_info=True)  #保留失败日志
        return False  #标记为可回退

    deadline = time.monotonic() + 1800  #最长合并时长，超时同样中止
    while proc.poll() is None:  #进程运行期间保持轮询，保证取消可被响应
        if task is not None and task.cancelled:  #收到取消请求
            proc.kill()  #中止外部合并进程
            proc.wait()  #回收子进程，避免僵尸
            raise DownloadCancelled(task.task_id)  #以取消语义中断合并
        if time.monotonic() > deadline:  #超过合并时限
            proc.kill()  #杀掉超时进程
            proc.wait()  #回收子进程
            _log.warning("ffmpeg 流拷贝超时，回退 moviepy")  #记录超时
            return False  #尝试备用合并器
        time.sleep(0.2)  #轮询间隔，兼顾取消响应与CPU开销
    stderr = proc.stderr.read() if proc.stderr else b""  #收集失败输出
    if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:  #验证输出非空
        return True  #合并成功
    _log.warning(  #记录ffmpeg失败输出以便定位原因
        "ffmpeg 流拷贝返回 %s: %s", proc.returncode, stderr[-500:]
    )
    #告知调用方尝试备用合并器
    return False


#ffmpeg不可用时交由moviepy重新编码
def _merge_with_moviepy(video_path: str, audio_path: str, output_path: str) -> None:
    """moviepy重编码合并（兼容性最好，但慢）"""
    try:  #优先使用新版moviepy导入路径
        from moviepy import AudioFileClip, VideoFileClip  #type: ignore[import-untyped]
    except ImportError:  #moviepy < 2.0
        from moviepy.audio.io.AudioFileClip import AudioFileClip  #type: ignore[no-redef]
        from moviepy.video.io.VideoFileClip import VideoFileClip  #type: ignore[no-redef]

    video = audio = final = None  #初始化清理对象
    try:  #打开两条轨道并合并
        video = VideoFileClip(video_path)  #读取视频轨
        audio = AudioFileClip(audio_path)  #读取音频轨
        final = video.with_audio(audio)  #附加音轨
        final.write_videofile(output_path, codec="libx264", audio_codec="aac", logger=None)  #输出MP4
    finally:  #不论结果都关闭打开的clip
        for clip in (final, audio, video):  #按组合轨、音轨、视频轨顺序关闭
            if clip is not None:  #跳过尚未构造的对象
                try:  #单个关闭失败不影响其他对象
                    clip.close()  #释放clip资源
                except Exception:  #noqa: BLE001 - 清理失败不影响结果
                    _log.debug("关闭 clip 失败", exc_info=True)  #保留清理异常


#下载器持有连接会话、临时文件及最终产物的执行流程
class MediaDownloader:
    """把「一条视频」下载到本地

    用法::

        dl = MediaDownloader(client)
        dl.download(task)        #阻塞，在工作线程中调用
    """

    def __init__(self, client: BiliClient, download_dir: str | None = None) -> None:
        self.client = client  #复用视频解析客户端
        self.download_dir = Path(download_dir) if download_dir else ensure_download_dir()  #设定下载根目录
        self._session: Any = None  #懒创建的HTTP会话
        self._active = 0  #正在执行的下载数（含封面）
        self._close_pending = False  #是否等待下载结束再关闭会话
        self._session_lock = threading.Lock()  #串行化会话创建、活跃计数与关闭

    #------------------------------------------------------------------ 会话
    def _get_session(self) -> Any:
        """复用一个会话，使同一次下载的多个流与重试都能复用连接"""
        with self._session_lock:  #懒初始化加锁，多线程不会各自创建会话
            if self._session is None:  #尚未创建下载会话时懒初始化
                self._session = create_session()  #首次请求时建立连接池
            return self._session  #交给下载流程复用

    def close(self) -> None:
        """释放会话

        若此刻仍有下载在跑（``shutdown(wait=False)`` 的场景），直接关会话会让
        在飞请求报错，所以标记为延后关闭，等最后一个任务结束再真正关闭"""
        with self._session_lock:  #计数检查与标志设置需原子完成
            if self._session is None:  #尚无会话不必关闭
                return  #提前结束
            if self._active > 0:  #下载仍在运行
                self._close_pending = True  #延迟关闭
                return  #留给最后一个下载者清理
        self._close_now()  #没有活跃任务时直接关闭

    def _close_now(self) -> None:
        with self._session_lock:  #关闭动作与计数变更同锁，避免并发交叉
            if self._session is not None:  #仅关闭已创建的会话
                try:  #会话资源清理允许失败
                    self._session.close()  #释放连接池
                except Exception:  #noqa: BLE001
                    _log.debug("关闭下载会话失败", exc_info=True)  #记录关闭异常
                self._session = None  #丢弃会话引用
            self._close_pending = False  #清除延迟关闭标志

    #------------------------------------------------------------------ 入口
    async def prepare_async(self, task: DownloadTask) -> StreamSelection:
        """在后台事件循环中解析下载地址（不阻塞UI线程）

        解析cid与playurl都是网络操作，必须在loop内await"""
        task.set_status(DownloadStatus.PENDING, "解析下载地址")  #显示解析阶段
        try:  #将客户端错误转为下载错误
            #异步查询选中分页和画质的媒体流
            return await self.client.select_streams_async(
                task.item,
                task.kind,
                qn=task.qn,
                page_index=task.page_index,
                audio_qn=task.audio_qn,
            )
        except ClientError as exc:  #解析失败
            raise DownloadError(str(exc)) from exc  #保留原异常链

    def download(
        self,
        task: DownloadTask,
        streams: StreamSelection | None = None,
        *,
        download_dir: str | Path | None = None,
    ) -> str:
        """执行下载，返回产出文件路径。失败抛DownloadError / DownloadCancelled

        streams可由prepare_async预先解析好；为None时同步解析（仅限非UI线程）
        download_dir为任务提交时冻结的目录快照；缺省时读取当前配置"""
        root = Path(download_dir) if download_dir else self.download_dir  #冻结本次任务的下载根目录
        root.mkdir(parents=True, exist_ok=True)  #创建下载根目录
        item = task.item  #当前视频信息
        target_dir = self._target_dir(task, root)  #目标视频文件夹
        self._check_free_space(target_dir, root)  #检查磁盘剩余容量
        target_dir.mkdir(parents=True, exist_ok=True)  #创建视频子目录
        stem = self._stem(task)  #构造文件名主干

        with self._session_lock:  #封面与媒体统一计入活跃数，close才能安全等待
            self._active += 1  #增加活跃任务数
        try:  #下载结束后归还活跃计数
            _local.task = task  #供三参数合并调用在线程内取得取消状态
            if task.format is DownloadFormat.COVER:  #封面不需要下载DASH流
                return self._download_cover(task, target_dir, stem, root)  #返回封面文件路径

            if streams is None:  #未经准备阶段时同步解析
                try:  #将客户端异常转换为下载异常
                    streams = self.client.select_streams(
                        item,
                        task.kind,
                        qn=task.qn,
                        page_index=task.page_index,
                        audio_qn=task.audio_qn,
                    )
                except ClientError as exc:  #媒体流解析失败
                    raise DownloadError(str(exc)) from exc  #保留原异常链

            task.set_status(DownloadStatus.DOWNLOADING, "开始下载")  #更新界面状态
            if task.format is DownloadFormat.VIDEO:  #视频需要分别下载两条轨道
                return self._download_video(task, streams, target_dir, stem, root)  #返回视频产物
            return self._download_audio(task, streams, target_dir, stem, root)  #返回音频产物
        finally:  #不论成功失败均清理计数
            _local.task = None  #清除线程局部任务引用
            with self._session_lock:  #计数归还与延迟关闭判断需原子完成
                self._active -= 1  #归还活跃名额
                need_close = self._close_pending and self._active <= 0  #是否最后一个任务
            if need_close:  #最后一个任务结束
                self._close_now()  #兑现延迟关闭

    #------------------------------------------------------------- 目录/命名
    def _target_dir(self, task: DownloadTask, root: Path) -> Path:
        """每个视频一个子文件夹，名字用视频标题（净化后）

        多P视频共用同一个文件夹；同名不同视频只会落进同一个目录，
        文件名再靠(2)、(3)去重，不会互相覆盖"""
        folder = sanitize_filename(task.item.title) or task.item.bvid  #过滤不安全标题
        return root / folder  #将视频目录放在冻结的下载根下

    def _stem(self, task: DownloadTask) -> str:
        """产出文件名主干：单P用标题，多P用「P序号｜该P标题」"""
        base = sanitize_filename(task.item.title) or task.item.bvid  #安全文件名主干
        pages = task.item.pages or []  #视频可能无分页列表
        if len(pages) > 1:  #多分页时加序号避免同目录冲突
            page = next((p for p in pages if p.index == task.page_index), None)  #寻找当前分页
            part = sanitize_filename(page.title) if page and page.title else ""  #净化分页标题
            if part:  #优先采用分页标题
                return f"P{task.page_index}_{part}"  #拼接分页序号与标题
            return f"P{task.page_index}_{base}"  #无分页标题时回退视频名
        return base  #单分页使用视频标题

    def _unique(self, path: Path) -> Path:
        """同名文件自动加 (2)、(3)…，并用O_EXCL原子占位，绝不覆盖已存在的文件"""
        for index in range(1, 1000):  #依次尝试原名与编号后缀
            candidate = (  #首次使用原路径，其后追加编号
                path if index == 1
                else path.with_name(f"{path.stem} ({index}){path.suffix}")
            )
            try:  #O_CREAT|O_EXCL原子预留产物名，消除检查与写入之间的竞争
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:  #名字已被占用
                continue  #尝试下一个编号
            os.close(fd)  #占位文件由后续os.replace / ffmpeg -y覆盖
            return candidate  #返回已预留的路径
        raise DownloadError(f"同名文件过多，无法命名：{path.name}")  #后缀已耗尽

    def _check_free_space(self, target_dir: Path, root: Path) -> None:
        """下载前粗查剩余空间，不足时提前报错"""
        try:  #获取目标所在分区的可用空间
            usage = shutil.disk_usage(target_dir if target_dir.exists() else root)  #读取磁盘数据
        except OSError:  #无法获取时不拦截下载
            return  #交给实际写入报告错误
        free_mb = usage.free / (1024 * 1024)  #将剩余字节转换为MB
        if free_mb < MIN_FREE_SPACE_MB:  #小于最低要求则拒绝下载
            raise DownloadError(
                f"磁盘剩余空间不足（{free_mb:.0f} MB），请清理后重试"
            )

    #------------------------------------------------------------- 视频流程
    def _download_video(
        self, task: DownloadTask, streams: StreamSelection, target_dir: Path, stem: str, root: Path
    ) -> str:
        if not streams.video_url or not streams.audio_url:  #检查两条必需媒体轨
            raise DownloadError("该视频缺少可用的音视频流（可能需要大会员或受地区限制）")  #报告缺失轨道

        video_tmp = self._tmp_path(task, "video.m4s", root)  #视频轨缓存路径
        audio_tmp = self._tmp_path(task, "audio.m4a", root)  #音频轨缓存路径
        output = self._unique(target_dir / f"{stem}.mp4")  #原子预留目标视频路径
        task.save_path = str(output)  #记录预期产物位置

        try:  #依次取得媒体轨并合并
            #下载视频轨道
            self._download_stream(
                task, streams.video_url, video_tmp, _VIDEO_STAGE, label="视频",
                backups=streams.video_backups,
            )
            #下载对应音频轨道
            self._download_stream(
                task, streams.audio_url, audio_tmp, _AUDIO_STAGE, label="音频",
                backups=streams.audio_backups,
            )

            task.check_cancelled()  #合并前确认未取消
            task.set_status(DownloadStatus.MERGING, "合并音视频")  #通知合并状态
            task.set_progress(MERGE_PROGRESS)  #更新合并阶段进度
            #合并进程运行中可响应取消（内部会杀进程并抛DownloadCancelled）
            merge_video_audio(str(video_tmp), str(audio_tmp), str(output))  #运行音视频合并（task经线程局部传递）
            #只有成功了才清临时文件：失败时保留.part，重试可以续传
            self._safe_remove(video_tmp)  #移除成功下载的视频轨
            self._safe_remove(audio_tmp)  #移除成功下载的音频轨
        except DownloadCancelled:  #取消任务时清理两条轨道与半成品产物
            self._safe_remove(video_tmp)  #删除视频缓存
            self._safe_remove(audio_tmp)  #删除音频缓存
            self._safe_remove(output)  #删除占位文件或合并半成品
            raise  #继续向上报告取消
        except Exception:  #其他失败时保留断点文件
            _log.info("保留临时文件以便断点续传：%s / %s", video_tmp.name, audio_tmp.name)  #记录缓存名
            raise  #继续向上报告失败

        if not output.exists() or output.stat().st_size == 0:  #校验合并结果非空
            raise DownloadError("合并后文件为空")  #报告空文件
        self._finish(task, output)  #执行完成收尾
        return str(output)  #返回产物路径

    #------------------------------------------------------------- 音频流程
    def _download_audio(
        self, task: DownloadTask, streams: StreamSelection, target_dir: Path, stem: str, root: Path
    ) -> str:
        if not streams.audio_url:  #确认音频轨可用
            raise DownloadError("该视频没有可用的音频流")  #报告缺少流

        audio_tmp = self._tmp_path(task, "audio.m4a", root)  #选择音频缓存位置
        want_convert = task.format in (DownloadFormat.AUDIO_MP3, DownloadFormat.AUDIO_WAV)  #判断是否需要转码
        ext = "mp3" if task.format is DownloadFormat.AUDIO_MP3 else "wav"  #选取转码目标扩展名
        output = self._unique(target_dir / (f"{stem}.{ext}" if want_convert else f"{stem}.m4a"))  #原子预留输出路径
        task.save_path = str(output)  #记录预期产物

        try:  #下载并按所选格式处理音频
            #下载音频原始流
            self._download_stream(
                task, streams.audio_url, audio_tmp, (0.0, 99.0), label="音频",
                backups=streams.audio_backups,
            )
            task.check_cancelled()  #转码或落位前检查取消
            if want_convert:  #MP3/WAV需运行转码（进程运行中可取消）
                self._convert_audio(task, audio_tmp, output, task.format)  #将原始流转为目标格式
                self._safe_remove(audio_tmp)  #成功后清理原始流
            else:  #M4A无需转码
                #输出名已被_unique原子预留，这里直接原子落位即可
                os.replace(audio_tmp, output)  #原子落位，避免半成品文件
        except DownloadCancelled:  #取消时清除缓存与半成品产物
            self._safe_remove(audio_tmp)  #删除未完成的音频流
            self._safe_remove(output)  #删除占位文件或转码半成品
            raise  #保留取消语义

        if not output.exists() or output.stat().st_size == 0:  #校验最终音频非空
            raise DownloadError("下载后文件为空")  #报告空文件
        self._finish(task, output)  #更新完成状态
        return str(output)  #返回音频路径

    def _convert_audio(
        self, task: DownloadTask, src: Path, dest: Path, fmt: DownloadFormat
    ) -> None:
        """m4a -> mp3/wav。没有ffmpeg时直接报错，不静默降级成m4a"""
        try:  #查找可选的转码依赖
            from imageio_ffmpeg import get_ffmpeg_exe  #type: ignore[import-untyped]
        except ImportError:  #包未安装时明确报错
            raise DownloadError("缺少 ffmpeg，无法转码；请改选 M4A 或先安装 ffmpeg") from None  #提示替代格式
        ffmpeg = get_ffmpeg_exe()  #定位可执行程序
        if not ffmpeg:  #依赖存在但没有ffmpeg
            raise DownloadError("找不到 ffmpeg 可执行文件，无法转码")  #拒绝静默降级

        import subprocess  #启动外部转码程序

        if fmt is DownloadFormat.AUDIO_MP3:  #选择MP3编码器
            codec_args = ["-codec:a", "libmp3lame", "-q:a", "2"]  #使用VBR MP3
            label = "MP3"  #状态中显示目标格式
        else:  #其他可转码格式为WAV
            codec_args = ["-codec:a", "pcm_s16le", "-ar", "44100"]  #采用PCM音频
            label = "WAV"  #状态中显示目标格式

        task.set_status(DownloadStatus.MERGING, f"转换 {label}")  #通知转码阶段
        task.set_progress(99.0)  #更新转码进度
        #Popen轮询等待：进程运行中收到取消请求时立即杀掉子进程
        proc = subprocess.Popen(  #启动选定格式的音频转码
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(src), *codec_args, str(dest)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 1800  #最长转码时长
        while proc.poll() is None:  #转码运行期间保持可取消
            if task.cancelled:  #收到取消请求
                proc.kill()  #中止外部转码进程
                proc.wait()  #回收子进程
                raise DownloadCancelled(task.task_id)  #以取消语义中断转码
            if time.monotonic() > deadline:  #超过转码时限
                proc.kill()  #杀掉超时进程
                proc.wait()  #回收子进程
                raise DownloadError(f"{label} 转码超时")  #报告超时失败
            time.sleep(0.2)  #轮询间隔，兼顾取消响应与CPU开销
        stderr = proc.stderr.read() if proc.stderr else b""  #收集失败输出
        if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:  #校验返回码和文件大小
            raise DownloadError(
                f"{label} 转码失败：{stderr[-300:].decode('utf-8', 'ignore')}"
            )

    #------------------------------------------------------------- 封面流程
    def _download_cover(self, task: DownloadTask, target_dir: Path, stem: str, root: Path) -> str:
        """下载封面原图到视频目录下的cover.jpg"""
        url = task.item.cover  #读取封面原图地址
        if not url:  #没有封面则无法下载
            raise DownloadError("该视频没有封面地址")  #报告缺失地址
        output = self._unique(target_dir / "cover.jpg")  #原子预留不重名的输出位置
        task.save_path = str(output)  #提前记录产物路径
        tmp = self._tmp_path(task, "cover.jpg", root)  #临时文件存放下载内容
        #设置站点来源与请求标识
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": f"https://www.bilibili.com/video/{task.item.bvid}",
        }
        session = self._get_session()  #获取可复用网络会话
        try:  #流式读取封面并写入临时文件
            with session.get(url, headers=headers, stream=True, timeout=DOWNLOAD_TIMEOUT) as resp:  #发起封面请求
                resp.raise_for_status()  #拒绝非成功响应
                with open(tmp, "wb") as fh:  #截断并写入封面缓存
                    for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):  #逐块读取响应
                        task.check_cancelled()  #在每块写入前检查取消
                        if chunk:  #忽略空数据块
                            fh.write(chunk)  #写入封面字节
            #封面缓存键含画质字段（见_tmp_path），取消中的重试任务不会共用同一份缓存
            os.replace(tmp, output)  #将封面缓存落位
        except DownloadCancelled:  #取消后删除封面缓存
            self._safe_remove(tmp)  #清理临时文件
            self._safe_remove(output)  #删除封面占位文件
            raise  #上报取消
        except requests.RequestException as exc:  #包装网络错误
            self._safe_remove(tmp)  #清理失败下载
            raise DownloadError(f"封面下载失败：{exc}") from exc  #保留异常链

        if not output.exists() or output.stat().st_size == 0:  #检查封面产物非空
            raise DownloadError("封面文件为空")  #报告文件为空
        self._finish(task, output)  #标记任务完成
        return str(output)  #返回封面路径

    def _finish(self, task: DownloadTask, output: Path) -> None:
        """统一收尾：标记完成，并按设置决定是否打开所在文件夹"""
        #收尾前最后核对取消，避免把取消期间跑完的产物标记为「完成」
        task.check_cancelled()  #已取消则抛DownloadCancelled，不覆盖取消状态
        task.set_progress(100.0)  #展示下载进度为完成
        task.set_status(DownloadStatus.COMPLETED, "完成")  #更新完成状态
        if _open_folder_when_done():  #按用户偏好打开所在文件夹
            self._reveal(output)  #在文件管理器定位产物

    @staticmethod
    def _reveal(path: Path) -> None:
        """在文件管理器里定位产出文件（失败不影响任务结果）"""
        import subprocess  #启动系统文件管理器
        import sys  #判断当前操作系统

        try:  #打开失败不改变下载结果
            if sys.platform == "win32":  #Windows选中文件
                #/select会打开目录并选中该文件
                subprocess.Popen(["explorer", "/select,", str(path)])  #noqa: S603,S607
            elif sys.platform == "darwin":  #macOS使用Finder
                subprocess.Popen(["open", "-R", str(path)])  #noqa: S603,S607
            else:  #其他系统打开父目录
                subprocess.Popen(["xdg-open", str(path.parent)])  #noqa: S603,S607
        except Exception:  #noqa: BLE001
            _log.debug("打开下载目录失败: %s", path, exc_info=True)  #仅记录打开异常

    #------------------------------------------------------------- 单流下载
    def _download_stream(
        self,
        task: DownloadTask,
        url: str,
        dest: Path,
        stage: tuple[float, float],
        *,
        label: str,
        backups: list[str] | None = None,
    ) -> None:
        """下载单个流到dest，带断点续传、重试与备用地址回退"""
        #为媒体请求指定站点来源及浏览器标识
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": f"https://www.bilibili.com/video/{task.item.bvid}",
            "Origin": "https://www.bilibili.com",
        }

        candidates = [url, *(backups or [])]  #主地址后依序尝试备用地址
        last_error: Exception | None = None  #保存最后一次失败原因

        for index, candidate in enumerate(candidates):  #遍历可选下载源
            if index > 0:  #切换到备用源
                _log.info("%s 主地址失败，改用备用地址 #%d", label, index)  #记录源序号
                dest.unlink(missing_ok=True)  #备用地址重下，清掉旧残留
            try:  #对本地址执行有限次重试
                self._download_with_retries(task, candidate, dest, headers, stage, label)  #下载该流
                return  #成功后不再访问备用源
            except DownloadCancelled:  #保留用户取消语义
                raise  #不尝试其他地址
            except DownloadError as exc:  #当前地址失败
                last_error = exc  #供最终错误消息引用
                if index == len(candidates) - 1:  #已到最后地址
                    break  #退出地址轮询

        raise DownloadError(f"{label}下载失败：{last_error}") from last_error  #报告所有地址失败

    #------------------------------------------------------------- 重试与断点续传
    def _download_with_retries(
        self,
        task: DownloadTask,
        url: str,
        dest: Path,
        headers: dict[str, str],
        stage: tuple[float, float],
        label: str,
    ) -> None:
        low, high = stage  #拆分当前流的进度区间
        last_error: Exception | None = None  #缓存最后一次下载异常
        #每次下载开始时读取最新重试次数
        max_attempts = max(1, _current_retries())  #至少执行一次

        for attempt in range(1, max_attempts + 1):  #逐次尝试当前地址
            task.check_cancelled()  #发起请求前检查取消
            try:  #执行单次流式下载
                self._download_once(task, url, dest, headers, low, high, label)  #可能续传已有文件
                return  #下载成功后结束重试
            except DownloadCancelled:  #取消不纳入重试
                raise  #原样上报取消
            except (
                requests.RequestException,  #5xx、连接错误等暂时故障；4xx已转为永久DownloadError
                DownloadError,
                OSError,
                TimeoutError,
            ) as exc:  #所列错误进入退避分支，永久错误除外
                last_error = exc  #更新最后错误
                if isinstance(exc, DownloadError) and exc.permanent:  #永久错误（如4xx）重试无意义
                    break  #直接结束重试，交由备用地址回退
                if isinstance(exc, requests.exceptions.ProxyError):  #特别提示代理故障
                    _log.error(
                        "下载被代理拦截：%s。若代理已关闭，请在 %s 中确认 "
                        "use_system_proxy=false（或修好代理后改为 true）",
                        exc,
                        config_path(),
                    )
                if attempt >= max_attempts:  #次数耗尽
                    break  #结束重试
                delay = DOWNLOAD_BACKOFF_BASE ** attempt  #计算指数退避秒数
                _log.warning(  #记录当前尝试的错误及下一次等待
                    "%s 第 %d 次下载失败(%s)，%.1fs 后重试", label, attempt, exc, delay
                )
                #告知任务页面当前正在重试
                task.set_status(
                    DownloadStatus.DOWNLOADING,
                    f"{label}第 {attempt} 次失败，重试中",
                )
                #可取消的退避等待：事件置位立即返回
                if task._cancel.wait(delay):  #noqa: SLF001 - 同包内复用取消事件做等待
                    raise DownloadCancelled(task.task_id)  #退避期间即时响应取消

        raise DownloadError(f"{label}下载失败：{last_error}") from last_error  #保留末次错误链

    def _download_once(
        self,
        task: DownloadTask,
        url: str,
        dest: Path,
        headers: dict[str, str],
        low: float,
        high: float,
        label: str,
    ) -> None:
        """下载一次。支持断点续传：服务端无视Range时必须截断，否则会追加出损坏文件"""
        resume_from = dest.stat().st_size if dest.exists() else 0  #从已有文件长度决定续传起点
        if resume_from <= 0:  #空文件应从头写入
            resume_from = 0  #标记全量下载

        req_headers = dict(headers)  #不修改共享基础请求头
        if resume_from > 0:  #本地有缓存时请求剩余字节
            req_headers["Range"] = f"bytes={resume_from}-"  #设置HTTP Range

        #使用自有会话（trust_env=False），不读取系统代理
        session = self._get_session()  #延迟创建并复用下载会话
        try:  #使用上下文关闭当前响应对象
            #发起允许跳转的流式HTTP请求
            with session.get(
                url,
                headers=req_headers,
                stream=True,
                timeout=DOWNLOAD_TIMEOUT,
                allow_redirects=True,
            ) as resp:
                try:  #区分永久与暂时HTTP错误
                    resp.raise_for_status()  #对4xx/5xx抛出异常
                except requests.HTTPError as exc:  #响应状态非成功
                    if 400 <= resp.status_code < 500:  #4xx多为鉴权失效/资源不存在，重试无意义
                        raise DownloadError(
                            f"{label}服务器返回 {resp.status_code}，不再重试",
                            permanent=True,
                        ) from exc  #保留原异常链
                    raise  #5xx等暂时错误交给重试退避

                expected_total = 0  #服务端声明的资源总长（仅206续传时可核对）
                if resume_from > 0 and resp.status_code != 206:  #非206不按续传处理
                    #服务端不支持Range（返回200全量）：必须从0重写，绝不能追加
                    _log.info("%s 服务端不支持断点续传，重新下载", label)  #说明回退为全量下载
                    resume_from = 0  #后续以写模式截断旧文件
                elif resume_from > 0:  #206续传响应需核对起点与总长
                    content_range = resp.headers.get("Content-Range", "")  #形如 "bytes 8-11/12"
                    match = re.match(r"bytes (\d+)-\d+/(\d+|\*)", content_range)  #解析续传元数据
                    if match and int(match.group(1)) != resume_from:  #起点与本地长度不符，可能是另一版本流
                        _log.warning(
                            "%s 续传起点不符（Content-Range: %s），重新下载", label, content_range
                        )
                        resume_from = 0  #截断重下，避免拼接出损坏文件
                    elif match and match.group(2) != "*":  #服务端给出了完整资源长度
                        expected_total = int(match.group(2))  #供下载完成后核对总长

                declared = int(resp.headers.get("Content-Length") or 0)  #读取响应声明的字节数
                total = declared + resume_from  #估计本流总大小
                done = resume_from  #续传从已保存的长度计进度
                self._report(task, low, high, done, total, label)  #汇报起始进度

                mode = "ab" if resume_from > 0 else "wb"  #续传追加，否则覆盖缓存
                with open(dest, mode) as fh:  #打开临时文件用于写入
                    for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):  #逐块接收响应
                        task.check_cancelled()  #每块写入前允许取消
                        if not chunk:  #跳过保活空块
                            continue  #读取下一块
                        fh.write(chunk)  #持久化当前块
                        done += len(chunk)  #累加已收字节
                        self._report(task, low, high, done, total, label)  #更新任务进度
        finally:  #请求结束后仍由下载器管理会话生命周期
            pass  #会话由MediaDownloader.close()统一释放

        if not dest.exists() or dest.stat().st_size == 0:  #校验目标文件存在且非空
            raise DownloadError(f"{label}下载后文件为空")  #拒绝空产物
        size = dest.stat().st_size  #读取最终落盘长度
        if expected_total > 0 and size != expected_total:  #续传完成后核对资源总长
            raise DownloadError(
                f"{label}续传后长度不符：期望 {expected_total} 字节，实际 {size} 字节"
            )
        if declared > 0 and size < resume_from + declared:  #核对本次响应声明的字节数（含续传分支）
            raise DownloadError(
                f"{label}下载不完整：期望至少 {resume_from + declared} 字节，实际 {size} 字节"
            )  #报告文件长度不足
        task.set_status(DownloadStatus.DOWNLOADING, f"{label}已完成")  #当前流下载结束

    @staticmethod
    def _report(
        task: DownloadTask,
        low: float,
        high: float,
        done: int,
        total: int,
        label: str,
    ) -> None:
        if total > 0:  #仅在获知总长时计算百分比
            ratio = min(1.0, done / total)  #上限锁在百分之百
            task.set_progress(low + (high - low) * ratio)  #映射到当前流的进度区间
            task.set_status(DownloadStatus.DOWNLOADING, f"下载{label}中 {ratio:.0%}")  #展示流进度

    #------------------------------------------------------------------ 工具
    def _tmp_path(self, task: DownloadTask, suffix: str, root: Path | None = None) -> Path:
        """临时文件路径：按「BV + 分P + 格式 + 画质/音质」命名

        这样重试（新任务、新task_id）能命中同一个.part继续续传；
        不同画质或音质的流各自独立缓存，不会混写同一份.part"""
        key = sanitize_filename(
            f"{task.item.bvid}_p{task.page_index}_{task.format.value}_q{task.qn}_a{task.audio_qn}"
        )  #缓存键包含画质与音质，区分不同流
        base = root if root is not None else self.download_dir  #优先使用任务冻结的目录
        return base / f".{key}.{suffix}.part"  #临时缓存直接放在下载根目录

    @staticmethod
    def _safe_remove(path: Path) -> None:
        try:  #临时文件清理不影响任务主异常
            if path.exists():  #仅处理现存文件
                path.unlink()  #删除缓存
        except OSError:  #清理失败仅写日志
            _log.debug("清理临时文件失败: %s", path, exc_info=True)  #保留异常上下文

