"""B 站 API 客户端封装。

设计要点：
  - **UI 层不接触 asyncio**：对外只暴露同步接口，内部提交到 BackgroundLoop。
  - 纯函数（parse_bv / sanitize_filename）与网络调用分离，便于单元测试。
  - 修复 B1：cid 统一从 ``pages[0]["cid"]`` 解析；搜索结果不再预先串行拉取 20 次详情。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from bilibili_api import search, user, video
from bilibili_api.utils.network import Credential

from ..config.settings import SEARCH_PAGE_SIZE
from ..utils.logger import get_logger
from .bridge import BackgroundLoop
from .credential import CredentialStore
from .models import DownloadKind, VideoItem
from .qrlogin import LoginSession, QrState

_log = get_logger("client")

# ---------------------------------------------------------------- 纯函数部分

# 与库内部校验保持一致：BV + 10 位 [A-Za-z0-9]
_BV_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")

# Windows 非法文件名字符 + 控制字符
_ILLEGAL_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MAX_STEM_LENGTH = 80


def parse_bv(text: str) -> str | None:
    """从用户输入中解析 BV 号。

    支持整条链接（https://www.bilibili.com/video/BV1xx411c7mD/?p=1）与裸 BV 号。
    修复 B11：旧正则 ``{10,}`` 放过了长度不为 12 的串，导致「格式错误」被误报为「搜索失败」。
    """
    if not text:
        return None
    match = re.search(r"BV[0-9A-Za-z]{10}", text.strip())
    return match.group(0) if match else None


def looks_like_link(text: str) -> bool:
    return "http://" in text or "https://" in text


def sanitize_filename(name: str, *, max_length: int = _MAX_STEM_LENGTH) -> str:
    """把视频标题净化成安全的文件名主干。

    - 换行/制表等控制字符先转成空格（保持可读性），再折叠连续空白；
    - 其余 Windows 非法字符替换为下划线（旧代码只处理了 ``/ \\ :``，
      遇到 ``? * " < > |`` 或结尾的点/空格仍会在 Windows 上失败）。
    """
    cleaned = (name or "").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    cleaned = _ILLEGAL_FS_CHARS.sub("_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")
    if cleaned.upper() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned or "untitled"


def human_size(num_bytes: float) -> str:
    """字节数转可读字符串。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.0f}{unit}" if unit == "B" else f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f}PB"


def parse_duration(value: Any) -> int:
    """把各种真实形态的时长解析成秒数，解析不了就返回 0。

    B 站接口在不同端点/版本下返回值并不统一，实测遇到过的形态：

    - ``469``           搜索接口的纯秒数
    - ``"7:49"``        搜索接口常见的 ``"分:秒"`` 字符串
                         （旧代码直接 ``int()`` 会抛
                         ``invalid literal for int() with base 10: '7:49'``）
    - ``"1:07:49"``     ``"时:分:秒"``
    - ``"469"``         数字字符串
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return 0
    if ":" in text:
        total = 0
        for part in text.split(":"):
            part = part.strip()
            if not part.isdigit():
                return 0
            total = total * 60 + int(part)
        return total
    try:
        return int(float(text.replace(",", "")))
    except (TypeError, ValueError):
        return 0


def parse_count(value: Any) -> int:
    """把播放量/弹幕数等解析成整数，解析不了就返回 0。

    兼容 ``12345``、``"12345"``、``"1,234"``、``"12.3万"``、``"1.2亿"``，
    以及接口偶尔返回的带高亮标签的字符串。
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = _clean_html(str(value)).strip()
    if not text:
        return 0

    multiplier = 1
    for suffix, factor in (("万", 10_000), ("亿", 100_000_000)):
        if text.endswith(suffix):
            multiplier = factor
            text = text[: -len(suffix)]
            break
    text = text.replace(",", "").strip()
    try:
        return int(float(text) * multiplier)
    except (TypeError, ValueError):
        return 0


def _stream_url(stream: dict[str, Any]) -> str:
    """取流的下载地址，兼容 baseUrl / base_url 两种写法。"""
    return str(stream.get("baseUrl") or stream.get("base_url") or "")


def _stream_backups(stream: dict[str, Any]) -> list[str]:
    """取备用地址列表（B 站 dash 的 backupUrl 可能是列表，也可能是单个字符串）。"""
    raw = stream.get("backupUrl") or stream.get("backup_url") or []
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, (list, tuple)):
        return [str(url) for url in raw if url]
    return []


def codecs_to_kind(codecs: Any) -> str:
    """把 dash 里的 ``codecs`` 字符串归类为 ``avc`` / ``hev`` / ``av01`` / ``other``。

    真实取值形如 ``avc1.64001F``、``hvc1.1.6.L120.90``、
    ``av01.0.08M.08.0.110.01.01.01.0``。
    """
    text = str(codecs or "").lower()
    if text.startswith("avc") or text.startswith("h264"):
        return "avc"
    if text.startswith("hev") or text.startswith("hvc") or text.startswith("h265"):
        return "hev"
    if text.startswith("av01") or text.startswith("av1"):
        return "av01"
    return "other"


# 编码偏好：AVC(H.264) 兼容性最好，几乎所有播放器与剪辑软件都吃
_CODEC_PREFERENCE = ("avc", "hev", "av01", "other")


def select_streams_from_dash(
    data: dict[str, Any], kind: DownloadKind = DownloadKind.VIDEO
) -> StreamSelection:
    """从 ``get_download_url`` 的响应里挑选音视频流。

    挑选规则（这是修复「库检测器崩溃后回退到纯带宽选择，结果拿到 HEVC/AV1」的关键）：
      1. **先按清晰度**（``id``，即 qn）取最高档；
      2. 同一档内**按编码偏好**选（AVC 优先，兼容性最好）；
      3. 再按带宽取该编码下最高的。

    只按带宽排序是错的：实测同一个视频里 HEVC 档的带宽可能高于 AVC 档，
    会选到兼容性差、剪辑软件打不开的编码。
    """
    dash = (data or {}).get("dash") or {}
    select = StreamSelection()

    audios = [s for s in (dash.get("audio") or []) if isinstance(s, dict)]
    if audios:
        best_audio = max(
            audios, key=lambda s: (parse_count(s.get("id")), parse_count(s.get("bandwidth")))
        )
        select.audio_url = _stream_url(best_audio)
        select.audio_backups = _stream_backups(best_audio)
        select.audio_label = f"id{parse_count(best_audio.get('id'))}"

    if kind is DownloadKind.VIDEO:
        videos = [
            s
            for s in (dash.get("video") or [])
            if isinstance(s, dict) and _stream_url(s)
        ]
        if videos:
            best = max(
                videos,
                key=lambda s: (
                    parse_count(s.get("id")),  # 清晰度优先
                    -_CODEC_PREFERENCE.index(codecs_to_kind(s.get("codecs"))),  # AVC 优先
                    parse_count(s.get("bandwidth")),  # 同档内取高码率
                ),
            )
            select.video_url = _stream_url(best)
            select.video_backups = _stream_backups(best)
            quality = parse_count(best.get("id"))
            select.quality_label = f"qn{quality}·{codecs_to_kind(best.get('codecs'))}"

    if not select.video_url and not select.audio_url:
        raise ClientError("未能解析出可用的音视频流，可能是会员/地区限制")
    if kind is DownloadKind.VIDEO and not select.video_url:
        raise ClientError("未能解析出可用的视频流（可能只提供试看片段）")
    if not select.audio_url:
        raise ClientError("未能解析出可用的音频流")
    return select


# ---------------------------------------------------------------- 数据模型


@dataclass(slots=True)
class SearchResult:
    """一次搜索的结果。"""

    items: list[VideoItem] = field(default_factory=list)
    page: int = 1
    keyword: str = ""

    def __len__(self) -> int:
        return len(self.items)


@dataclass(slots=True)
class StreamSelection:
    """挑选好的音视频流地址。

    ``*_backups`` 是 B 站 dash 响应里自带的备用 CDN 地址，主地址失败时可回退。
    """

    video_url: str = ""
    audio_url: str = ""
    quality_label: str = ""
    audio_label: str = ""
    video_backups: list[str] = field(default_factory=list)
    audio_backups: list[str] = field(default_factory=list)

    @property
    def has_video(self) -> bool:
        return bool(self.video_url)


class ClientError(RuntimeError):
    """面向用户的可读错误。"""


# ---------------------------------------------------------------- 客户端


class BiliClient:
    """bilibili_api 的同步门面。

    所有网络方法都是同步的，内部通过 BackgroundLoop 提交协程并等待结果。
    因此**不得在 Tk 主线程调用**，否则仍会阻塞界面。
    """

    def __init__(
        self,
        bridge: BackgroundLoop | None = None,
        store: CredentialStore | None = None,
    ) -> None:
        self._bridge = bridge or BackgroundLoop.instance()
        self._store = store or CredentialStore()
        self.credential: Credential | None = None
        self._qr: login_v2.QrCodeLogin | None = None

    # ------------------------------------------------------------ 凭据管理
    def restore_credential(self) -> bool:
        """启动时尝试恢复上次登录。返回是否恢复成功（不代表仍然有效）。"""
        cookies = self._store.load()
        if not cookies:
            return False
        try:
            self.credential = Credential.from_cookies(cookies)
        except Exception:  # noqa: BLE001
            _log.warning("恢复凭据失败", exc_info=True)
            return False
        _log.info("已从本地恢复登录凭据")
        return True

    def save_credential(self) -> None:
        if self.credential is None:
            return
        try:
            self._store.save(self.credential.get_cookies())
        except Exception:  # noqa: BLE001
            _log.warning("保存凭据时出错", exc_info=True)

    def logout(self) -> None:
        self.credential = None
        self._store.clear()

    @property
    def logged_in(self) -> bool:
        return self.credential is not None

    def validate_credential(self) -> bool:
        """同步校验凭据是否仍然有效（失败返回 False，不抛错）。"""
        if self.credential is None:
            return False
        try:
            return bool(self._bridge.run_sync(self.credential.check_valid(), timeout=20))
        except Exception:  # noqa: BLE001
            _log.info("凭据校验未通过", exc_info=True)
            return False

    # ---------------------------------------------------------------- 登录
    def login_qrcode_start(self) -> bytes:
        """生成二维码，返回 PNG 字节流。"""
        self._qr = LoginSession()
        return self._bridge.run_sync(self._qr.generate(), timeout=30)

    def login_qrcode_poll(self) -> QrState:
        """查询一次扫码状态（修复 B3：调用方必须处理全部状态）。"""
        if self._qr is None:
            raise ClientError("尚未生成二维码")
        result = self._bridge.run_sync(self._qr.poll(), timeout=30)
        if result.state is QrState.ERROR:
            # 网络抖动等可恢复错误：返回 SCAN 让上层继续轮询，只记日志
            _log.warning("扫码轮询出现可恢复错误: %s", result.message)
            return QrState.SCAN
        return result.state

    def login_qrcode_credential(self) -> Credential:
        """登录成功后取凭据并持久化。

        凭据由 ``LoginSession`` 自己从官方响应解析（见 core/qrlogin.py），
        因此不会出现「扫码成功但 sessdata 为空」的情况；若确实解析不到，
        这里抛出可读错误并在日志中留下原始响应。
        """
        if self._qr is None:
            raise ClientError("尚未生成二维码")
        try:
            cred = self._qr.credential()
        except RuntimeError as exc:
            raise ClientError(str(exc)) from exc
        self.credential = cred
        self.save_credential()
        return cred

    # ------------------------------------------------------------ 用户信息
    def fetch_self_info(self) -> dict[str, Any]:
        """获取登录用户信息（异步失败只记日志，由调用方决定 UI 表现）。"""
        if self.credential is None:
            raise ClientError("未登录")
        return self._bridge.run_sync(user.get_self_info(credential=self.credential), timeout=30)

    # ---------------------------------------------------------------- 搜索
    def search_videos(self, keyword: str, page: int = 1) -> SearchResult:
        """关键词搜索。

        与旧实现不同：这里**不再**为每条结果串行请求一次详情（旧代码 20 次串行请求
        导致界面冻结），cid 在真正下载时再解析（见 ensure_cid）。
        """
        keyword = keyword.strip()
        if not keyword:
            raise ClientError("请输入搜索关键词")
        try:
            raw = self._bridge.run_sync(
                search.search_by_type(
                    keyword=keyword,
                    search_type=search.SearchObjectType.VIDEO,
                    page=page,
                    page_size=SEARCH_PAGE_SIZE,
                ),
                timeout=45,
            )
        except Exception as exc:  # noqa: BLE001
            raise ClientError(f"搜索失败：{exc}") from exc

        items: list[VideoItem] = []
        skipped = 0
        for entry in raw.get("result", []) or []:
            try:
                bvid = entry.get("bvid")
                if not bvid:
                    continue
                items.append(
                    VideoItem(
                        bvid=bvid,
                        cid=0,  # 延迟解析
                        title=_clean_html(entry.get("title", "")),
                        author=entry.get("author", ""),
                        # 搜索结果里 duration 可能是 "7:49"、play 可能是 "12.3万"，
                        # 一律走健壮解析，绝不让类型问题炸掉整个搜索
                        play=parse_count(entry.get("play")),
                        danmaku=parse_count(entry.get("danmaku")),
                        duration=parse_duration(entry.get("duration")),
                    )
                )
            except Exception:  # noqa: BLE001 - 单条脏数据不应拖垮整页结果
                skipped += 1
                _log.debug("跳过无法解析的搜索结果项: %r", entry, exc_info=True)
        if skipped:
            _log.warning("搜索结果中有 %d 条无法解析，已跳过", skipped)
        return SearchResult(items=items, page=page, keyword=keyword)

    def fetch_by_bvid(self, bvid: str) -> VideoItem:
        """按 BV 号取单条视频信息。

        修复 B1：cid 正确地从 ``pages[0]["cid"]`` 提取。
        """
        try:
            info = self._bridge.run_sync(
                video.Video(bvid=bvid, credential=self.credential).get_info(), timeout=30
            )
        except Exception as exc:  # noqa: BLE001
            raise ClientError(f"获取视频信息失败：{exc}") from exc

        pages: Sequence[dict[str, Any]] = info.get("pages") or []
        if not pages:
            raise ClientError("该视频没有可用分 P")
        cid = parse_count(pages[0].get("cid"))
        if not cid:
            raise ClientError("该视频的分 P 信息缺少 cid")
        stat = info.get("stat") or {}
        owner = info.get("owner") or {}
        return VideoItem(
            bvid=info.get("bvid") or bvid,
            cid=cid,
            title=_clean_html(info.get("title", "")),
            author=owner.get("name", ""),
            play=parse_count(stat.get("view")),
            danmaku=parse_count(stat.get("danmaku")),
            duration=parse_duration(info.get("duration")),
            cover=info.get("pic", ""),
        )

    def ensure_cid(self, item: VideoItem) -> int:
        """确保 item.cid 可用（搜索结果里为 0 时再请求一次）。"""
        if item.cid:
            return item.cid
        return self._bridge.run_sync(self.ensure_cid_async(item), timeout=30)

    async def ensure_cid_async(self, item: VideoItem) -> int:
        """ensure_cid 的协程版本（供后台 loop 内部使用，避免同 loop 死锁）。"""
        if not item.cid:
            item.cid = await video.Video(
                bvid=item.bvid, credential=self.credential
            ).get_cid(0)
        return item.cid

    # ------------------------------------------------------------ 下载地址
    def select_streams(
        self, item: VideoItem, kind: DownloadKind = DownloadKind.VIDEO
    ) -> StreamSelection:
        """挑选合适的音视频流（同步封装）。"""
        return self._bridge.run_sync(self.select_streams_async(item, kind), timeout=60)

    async def select_streams_async(
        self, item: VideoItem, kind: DownloadKind = DownloadKind.VIDEO
    ) -> StreamSelection:
        """挑选合适的音视频流。

        旧实现用 ``max(dash['video'], key=bandwidth)`` 直接取带宽最大者，
        在 AV1/HEVC 与 AVC 并存时会选到兼容性差的编码；这里交给库的检测器，
        并优先 AVC 以保证播放器/剪辑软件兼容。
        """
        cid = await self.ensure_cid_async(item)
        try:
            data = await video.Video(
                bvid=item.bvid, credential=self.credential
            ).get_download_url(cid=cid)
        except Exception as exc:  # noqa: BLE001
            raise ClientError(f"获取下载地址失败：{exc}") from exc

        # 首选自己实现的选择器。库的 VideoDownloadURLDataDetecter 在真实数据上
        # 会稳定崩溃（video.py:2584 假设 video_codecs 不为 None，而 dash 里的
        # dolby/flac 流没有该字段），所以不能依赖它作为主路径。
        try:
            return select_streams_from_dash(data, kind)
        except ClientError:
            raise
        except Exception:  # noqa: BLE001 - 解析异常则退回库的检测器
            _log.warning("内置流选择失败，尝试库检测器", exc_info=True)

        try:
            detecter = video.VideoDownloadURLDataDetecter(data)
            streams = detecter.detect_best_streams(codecs=[video.VideoCodecs.AVC])
        except Exception:  # noqa: BLE001
            _log.warning("库检测器同样失败，改用最简单的带宽选择", exc_info=True)
            return self._fallback_streams(data, kind)

        select = StreamSelection()
        for stream in streams or []:
            if isinstance(stream, video.VideoStreamDownloadURL) and not select.video_url:
                select.video_url = stream.url
                select.quality_label = getattr(stream.video_quality, "name", "") or ""
            elif isinstance(stream, video.AudioStreamDownloadURL) and not select.audio_url:
                select.audio_url = stream.url
                select.audio_label = getattr(stream.audio_quality, "name", "") or ""

        if not select.video_url and not select.audio_url:
            return self._fallback_streams(data, kind)
        return select

    @staticmethod
    def _fallback_streams(data: dict[str, Any], kind: DownloadKind) -> StreamSelection:
        """最后兜底：完全不挑编码，只取带宽最大者（保证「至少能下」）。"""
        dash = (data or {}).get("dash") or {}
        select = StreamSelection()
        audios = dash.get("audio") or []
        videos = dash.get("video") or []
        if audios:
            best_audio = max(audios, key=lambda x: x.get("bandwidth", 0))
            select.audio_url = _stream_url(best_audio)
        if videos and kind is DownloadKind.VIDEO:
            best_video = max(videos, key=lambda x: x.get("bandwidth", 0))
            select.video_url = _stream_url(best_video)
        if not select.video_url and not select.audio_url:
            raise ClientError("未能解析出可用的音视频流，可能是会员/地区限制")
        return select

    # ---------------------------------------------------------- 异步提交封装
    def submit(
        self,
        factory: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        """给 UI 用的通用异步入口：factory() 必须返回协程。

        例如：client.submit(lambda: ..., on_success=...)。
        这里保留原始协程对象由 bridge 调度。
        """
        coro = factory()
        self._bridge.submit(coro, on_success=on_success, on_error=on_error)


def _clean_html(text: str) -> str:
    """去掉搜索结果里用于高亮的 <em> 等标签。"""
    return re.sub(r"<[^>]+>", "", text or "")
