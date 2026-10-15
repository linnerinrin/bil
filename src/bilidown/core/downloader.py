"""音视频下载器：分块下载 + 断点续传 + 重试退避 + 合并。

修复审计缺陷：
  B2   临时文件按 task_id 命名，避免「下载视频」与「下载音频」互相删除对方文件。
  B10  音频流使用正确的 .m4a 扩展名（DASH 音频是 AAC，不是 mp3）。
  B9   失败时记录完整异常并按类型回传可读信息。
  B11  合并成功但清理临时文件失败，不再把整个任务判为失败。
  C10  支持取消，支持重试。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import requests

from ..config.paths import ensure_download_dir
from ..config.settings import (
    DOWNLOAD_BACKOFF_BASE,
    DOWNLOAD_CHUNK_SIZE,
    DOWNLOAD_RETRIES,
    DOWNLOAD_TIMEOUT,
    USER_AGENT,
)
from ..utils.logger import get_logger
from . import preferences as preferences_mod
from .client import BiliClient, ClientError, StreamSelection, sanitize_filename
from .models import DownloadCancelled, DownloadKind, DownloadStatus, DownloadTask
from .network import config_path, create_session

_log = get_logger("downloader")


def _current_retries() -> int:
    """当前生效的重试次数（可被设置页调整，读不到就用默认值）。"""
    try:
        return int(preferences_mod.store().load().download_retries)
    except Exception:  # noqa: BLE001 - 配置不可用时退回默认值
        return DOWNLOAD_RETRIES


def _open_folder_when_done() -> bool:
    try:
        return bool(preferences_mod.store().load().open_folder_when_done)
    except Exception:  # noqa: BLE001
        return False

# 进度权重：视频轨 0-70，音频轨 70-95，合并 95-100
_VIDEO_STAGE = (0.0, 70.0)
_AUDIO_STAGE = (70.0, 95.0)
MERGE_PROGRESS = 96.0


class DownloadError(RuntimeError):
    """下载阶段的可读错误。"""


def merge_video_audio(video_path: str, audio_path: str, output_path: str) -> None:
    """把分离的视频轨与音频轨合并为 mp4。

    优先使用 ffmpeg 流拷贝（快且无损），不可用时回退到 moviepy 重编码。
    """
    if _merge_with_ffmpeg(video_path, audio_path, output_path):
        return
    _merge_with_moviepy(video_path, audio_path, output_path)


def _merge_with_ffmpeg(video_path: str, audio_path: str, output_path: str) -> bool:
    """ffmpeg -c copy 合并。返回是否成功。"""
    try:
        from imageio_ffmpeg import get_ffmpeg_exe  # type: ignore[import-untyped]
    except ImportError:
        return False

    ffmpeg = get_ffmpeg_exe()
    if not ffmpeg:
        return False

    import subprocess

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
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=1800,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _log.warning("ffmpeg 流拷贝合并失败，回退 moviepy", exc_info=True)
        return False
    if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        return True
    _log.warning(
        "ffmpeg 流拷贝返回 %s: %s", proc.returncode, proc.stderr[-500:] if proc.stderr else ""
    )
    return False


def _merge_with_moviepy(video_path: str, audio_path: str, output_path: str) -> None:
    """moviepy 重编码合并（兼容性最好，但慢）。"""
    try:
        from moviepy import AudioFileClip, VideoFileClip  # type: ignore[import-untyped]
    except ImportError:  # moviepy < 2.0
        from moviepy.audio.io.AudioFileClip import AudioFileClip  # type: ignore[no-redef]
        from moviepy.video.io.VideoFileClip import VideoFileClip  # type: ignore[no-redef]

    video = audio = final = None
    try:
        video = VideoFileClip(video_path)
        audio = AudioFileClip(audio_path)
        final = video.with_audio(audio)
        final.write_videofile(output_path, codec="libx264", audio_codec="aac", logger=None)
    finally:
        for clip in (final, audio, video):
            if clip is not None:
                try:
                    clip.close()
                except Exception:  # noqa: BLE001 - 清理失败不影响结果
                    _log.debug("关闭 clip 失败", exc_info=True)


class MediaDownloader:
    """把「一条视频」下载到本地。

    用法::

        dl = MediaDownloader(client)
        dl.download(task)        # 阻塞，在工作线程中调用
    """

    def __init__(self, client: BiliClient, download_dir: str | None = None) -> None:
        self.client = client
        self.download_dir = Path(download_dir) if download_dir else ensure_download_dir()
        self._session: Any = None
        self._active = 0
        self._close_pending = False

    # ------------------------------------------------------------------ 会话
    def _get_session(self) -> Any:
        """复用一个会话，使同一次下载的多个流与重试都能复用连接。"""
        if self._session is None:
            self._session = create_session()
        return self._session

    def close(self) -> None:
        """释放会话。

        若此刻仍有下载在跑（``shutdown(wait=False)`` 的场景），直接关会话会让
        在飞请求报错，所以标记为延后关闭，等最后一个任务结束再真正关闭。
        """
        if self._session is None:
            return
        if self._active > 0:
            self._close_pending = True
            return
        self._close_now()

    def _close_now(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                _log.debug("关闭下载会话失败", exc_info=True)
            self._session = None
        self._close_pending = False

    # ------------------------------------------------------------------ 入口
    async def prepare_async(self, task: DownloadTask) -> StreamSelection:
        """在后台事件循环中解析下载地址（不阻塞 UI 线程）。

        解析 cid 与 playurl 都是网络操作，必须在 loop 内 await；
        若在 Tk 主线程同步等待会重新引入界面冻结（审计缺陷 B7）。
        """
        task.set_status(DownloadStatus.PENDING, "解析下载地址")
        try:
            return await self.client.select_streams_async(task.item, task.kind)
        except ClientError as exc:
            raise DownloadError(str(exc)) from exc

    def download(self, task: DownloadTask, streams: StreamSelection | None = None) -> str:
        """执行下载，返回产出文件路径。失败抛 DownloadError / DownloadCancelled。

        streams 可由 prepare_async 预先解析好；为 None 时同步解析（仅限非 UI 线程）。
        """
        self.download_dir.mkdir(parents=True, exist_ok=True)
        item = task.item

        if streams is None:
            try:
                streams = self.client.select_streams(item, task.kind)
            except ClientError as exc:
                raise DownloadError(str(exc)) from exc

        self._active += 1
        try:
            task.set_status(DownloadStatus.DOWNLOADING, "开始下载")
            stem = f"{sanitize_filename(item.title)}_{item.bvid}"
            if task.kind is DownloadKind.VIDEO:
                return self._download_video(task, streams, stem)
            return self._download_audio(task, streams, stem)
        finally:
            self._active -= 1
            if self._close_pending and self._active <= 0:
                self._close_now()

    # ------------------------------------------------------------- 视频流程
    def _download_video(
        self, task: DownloadTask, streams: StreamSelection, stem: str
    ) -> str:
        if not streams.video_url or not streams.audio_url:
            raise DownloadError("该视频缺少可用的音视频流（可能需要大会员或受地区限制）")

        video_tmp = self._tmp_path(task, "video.m4s")
        audio_tmp = self._tmp_path(task, "audio.m4a")
        output = self.download_dir / f"{stem}.mp4"
        task.save_path = str(output)

        try:
            self._download_stream(
                task, streams.video_url, video_tmp, _VIDEO_STAGE, label="视频",
                backups=streams.video_backups,
            )
            self._download_stream(
                task, streams.audio_url, audio_tmp, _AUDIO_STAGE, label="音频",
                backups=streams.audio_backups,
            )

            task.check_cancelled()
            task.set_status(DownloadStatus.MERGING, "合并音视频")
            task.set_progress(MERGE_PROGRESS)
            merge_video_audio(str(video_tmp), str(audio_tmp), str(output))
        finally:
            # B11：临时文件清理失败不改变任务结果
            self._safe_remove(video_tmp)
            self._safe_remove(audio_tmp)

        if not output.exists() or output.stat().st_size == 0:
            raise DownloadError("合并后文件为空")
        self._finish(task, output)
        return str(output)

    # ------------------------------------------------------------- 音频流程
    def _download_audio(
        self, task: DownloadTask, streams: StreamSelection, stem: str
    ) -> str:
        if not streams.audio_url:
            raise DownloadError("该视频没有可用的音频流")

        audio_tmp = self._tmp_path(task, "audio.m4a")
        output = self.download_dir / f"{stem}.m4a"
        task.save_path = str(output)

        try:
            self._download_stream(
                task, streams.audio_url, audio_tmp, (0.0, 99.0), label="音频",
                backups=streams.audio_backups,
            )
            task.check_cancelled()
            os.replace(audio_tmp, output)  # 原子落位，避免半成品文件
        finally:
            self._safe_remove(audio_tmp)

        if not output.exists() or output.stat().st_size == 0:
            raise DownloadError("下载后文件为空")
        self._finish(task, output)
        return str(output)

    def _finish(self, task: DownloadTask, output: Path) -> None:
        """统一收尾：标记完成，并按设置决定是否打开所在文件夹。"""
        task.set_progress(100.0)
        task.set_status(DownloadStatus.COMPLETED, "完成")
        if _open_folder_when_done():
            self._reveal(output)

    @staticmethod
    def _reveal(path: Path) -> None:
        """在文件管理器里定位产出文件（失败不影响任务结果）。"""
        import subprocess
        import sys

        try:
            if sys.platform == "win32":
                # /select 会打开目录并选中该文件
                subprocess.Popen(["explorer", "/select,", str(path)])  # noqa: S603,S607
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)])  # noqa: S603,S607
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])  # noqa: S603,S607
        except Exception:  # noqa: BLE001
            _log.debug("打开下载目录失败: %s", path, exc_info=True)

    # ------------------------------------------------------------- 单流下载
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
        """下载单个流到 dest，带断点续传、重试与备用地址回退。"""
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": f"https://www.bilibili.com/video/{task.item.bvid}",
            "Origin": "https://www.bilibili.com",
        }

        candidates = [url, *(backups or [])]
        last_error: Exception | None = None

        for index, candidate in enumerate(candidates):
            if index > 0:
                _log.info("%s 主地址失败，改用备用地址 #%d", label, index)
                dest.unlink(missing_ok=True)  # 备用地址重下，清掉旧残留
            try:
                self._download_with_retries(task, candidate, dest, headers, stage, label)
                return
            except DownloadCancelled:
                raise
            except DownloadError as exc:
                last_error = exc
                if index == len(candidates) - 1:
                    break

        raise DownloadError(f"{label}下载失败：{last_error}") from last_error

    def _download_with_retries(
        self,
        task: DownloadTask,
        url: str,
        dest: Path,
        headers: dict[str, str],
        stage: tuple[float, float],
        label: str,
    ) -> None:
        low, high = stage
        last_error: Exception | None = None
        # 次数在每次下载开始时读取，使设置页的修改对之后的任务立即生效
        max_attempts = max(1, _current_retries())

        for attempt in range(1, max_attempts + 1):
            task.check_cancelled()
            try:
                self._download_once(task, url, dest, headers, low, high, label)
                return
            except DownloadCancelled:
                raise
            except (
                requests.RequestException,  # 含 HTTPError：4xx/5xx 必须可重试
                DownloadError,
                OSError,
                TimeoutError,
            ) as exc:
                last_error = exc
                if isinstance(exc, requests.exceptions.ProxyError):
                    _log.error(
                        "下载被代理拦截：%s。若代理已关闭，请在 %s 中确认 "
                        "use_system_proxy=false（或修好代理后改为 true）",
                        exc,
                        config_path(),
                    )
                if attempt >= max_attempts:
                    break
                delay = DOWNLOAD_BACKOFF_BASE ** attempt
                _log.warning(
                    "%s 第 %d 次下载失败(%s)，%.1fs 后重试", label, attempt, exc, delay
                )
                task.set_status(
                    DownloadStatus.DOWNLOADING,
                    f"{label}第 {attempt} 次失败，重试中",
                )
                time.sleep(delay)

        raise DownloadError(f"{label}下载失败：{last_error}") from last_error

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
        """下载一次。支持断点续传：服务端无视 Range 时必须截断，否则会追加出损坏文件。"""
        resume_from = dest.stat().st_size if dest.exists() else 0
        if resume_from <= 0:
            resume_from = 0

        req_headers = dict(headers)
        if resume_from > 0:
            req_headers["Range"] = f"bytes={resume_from}-"

        # 用自己的会话（trust_env=False）：避免被失效的系统代理拦住
        # （真实故障：requests 默认读系统代理，代理没开时下载必然失败）
        session = self._get_session()
        try:
            with session.get(
                url,
                headers=req_headers,
                stream=True,
                timeout=DOWNLOAD_TIMEOUT,
                allow_redirects=True,
            ) as resp:
                resp.raise_for_status()

                if resume_from > 0 and resp.status_code != 206:
                    # 服务端不支持 Range（返回 200 全量）：必须从 0 重写，绝不能追加
                    _log.info("%s 服务端不支持断点续传，重新下载", label)
                    resume_from = 0

                declared = int(resp.headers.get("Content-Length") or 0)
                total = declared + resume_from
                done = resume_from
                self._report(task, low, high, done, total, label)

                mode = "ab" if resume_from > 0 else "wb"
                with open(dest, mode) as fh:
                    for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        task.check_cancelled()
                        if not chunk:
                            continue
                        fh.write(chunk)
                        done += len(chunk)
                        self._report(task, low, high, done, total, label)
        finally:
            pass  # 会话由 MediaDownloader.close() 统一释放（多个流复用连接）

        if not dest.exists() or dest.stat().st_size == 0:
            raise DownloadError(f"{label}下载后文件为空")
        if declared > 0 and resume_from == 0 and dest.stat().st_size < declared:
            raise DownloadError(
                f"{label}下载不完整：期望 {declared} 字节，实际 {dest.stat().st_size} 字节"
            )
        task.set_status(DownloadStatus.DOWNLOADING, f"{label}已完成")

    @staticmethod
    def _report(
        task: DownloadTask,
        low: float,
        high: float,
        done: int,
        total: int,
        label: str,
    ) -> None:
        if total > 0:
            ratio = min(1.0, done / total)
            task.set_progress(low + (high - low) * ratio)
            task.set_status(DownloadStatus.DOWNLOADING, f"下载{label}中 {ratio:.0%}")

    # ------------------------------------------------------------------ 工具
    def _tmp_path(self, task: DownloadTask, suffix: str) -> Path:
        """按 task_id 生成临时文件路径（修复 B2：不同任务互不干扰）。"""
        return self.download_dir / f".{task.task_id}.{suffix}.part"

    @staticmethod
    def _safe_remove(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            _log.debug("清理临时文件失败: %s", path, exc_info=True)
