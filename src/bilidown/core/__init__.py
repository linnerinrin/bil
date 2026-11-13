#架构作用：集中暴露核心领域类型、异步桥接与网络下载接口，供上层统一导入
"""核心层：事件循环桥接、API客户端、下载器、任务管理与领域模型"""

from __future__ import annotations

#导出桥接器：暴露进程常驻事件循环
from .bridge import BackgroundLoop
#导出客户端：集中暴露视频检索、清晰度映射与解析函数
from .client import (
    QUALITY_ORDER,
    QUALITY_TABLE,
    BiliClient,
    ClientError,
    SearchResult,
    StreamSelection,
    codecs_to_kind,
    parse_bv,
    parse_count,
    parse_duration,
    quality_label,
    quality_permission,
    sanitize_filename,
    select_streams_from_dash,
)
from .credential import CredentialStore
from .downloader import DownloadError, MediaDownloader, merge_video_audio
#导出领域模型：任务、视频与分P信息
from .models import (
    DownloadCancelled,
    DownloadFormat,
    DownloadKind,
    DownloadStatus,
    DownloadTask,
    VideoItem,
    VideoPage,
)
#导出网络与偏好：共用客户端网络配置和本地用户设置
from .network import (
    NetworkSettings,
    apply_library_settings,
    config_path,
    create_session,
    describe,
    load_network_settings,
    reset_cache,
)
from .preferences import (
    CONCURRENT_RANGE,
    RETRIES_RANGE,
    PreferenceStore,
    Preferences,
    background_hint_dir,
)
from .preferences import load as load_preferences
from .preferences import save as save_preferences
#导出登录与任务：提供二维码流程及下载任务调度
from .qrlogin import (
    LoginSession,
    QrPollResult,
    QrState,
    build_credential,
    exchange_ticket,
    is_ticket_url,
    parse_login_cookies,
)
from .tasks import TaskManager

#公开符号表：限定从core包默认导出的接口
__all__ = [
    "BackgroundLoop",
    "BiliClient",
    "CONCURRENT_RANGE",
    "ClientError",
    "CredentialStore",
    "DownloadCancelled",
    "DownloadError",
    "DownloadFormat",
    "DownloadKind",
    "DownloadStatus",
    "DownloadTask",
    "LoginSession",
    "MediaDownloader",
    "NetworkSettings",
    "PreferenceStore",
    "Preferences",
    "QUALITY_ORDER",
    "QUALITY_TABLE",
    "QrPollResult",
    "QrState",
    "RETRIES_RANGE",
    "SearchResult",
    "StreamSelection",
    "TaskManager",
    "VideoItem",
    "VideoPage",
    "apply_library_settings",
    "background_hint_dir",
    "build_credential",
    "codecs_to_kind",
    "config_path",
    "create_session",
    "describe",
    "exchange_ticket",
    "is_ticket_url",
    "load_network_settings",
    "load_preferences",
    "merge_video_audio",
    "parse_bv",
    "parse_count",
    "parse_duration",
    "parse_login_cookies",
    "quality_label",
    "quality_permission",
    "reset_cache",
    "sanitize_filename",
    "save_preferences",
    "select_streams_from_dash",  #导出DASH流选择器
]
