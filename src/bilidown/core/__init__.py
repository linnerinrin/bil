"""核心层：事件循环桥接、API 客户端、下载器、任务管理与领域模型。"""

from __future__ import annotations

from .bridge import BackgroundLoop
from .client import (
    BiliClient,
    ClientError,
    SearchResult,
    StreamSelection,
    codecs_to_kind,
    human_size,
    parse_bv,
    parse_count,
    parse_duration,
    sanitize_filename,
    select_streams_from_dash,
)
from .credential import CredentialStore
from .downloader import DownloadError, MediaDownloader, merge_video_audio
from .models import (
    DownloadCancelled,
    DownloadKind,
    DownloadStatus,
    DownloadTask,
    VideoItem,
)
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

__all__ = [
    "BackgroundLoop",
    "BiliClient",
    "CONCURRENT_RANGE",
    "ClientError",
    "CredentialStore",
    "DownloadCancelled",
    "DownloadError",
    "DownloadKind",
    "DownloadStatus",
    "DownloadTask",
    "LoginSession",
    "MediaDownloader",
    "NetworkSettings",
    "PreferenceStore",
    "Preferences",
    "QrPollResult",
    "QrState",
    "RETRIES_RANGE",
    "SearchResult",
    "StreamSelection",
    "TaskManager",
    "VideoItem",
    "apply_library_settings",
    "background_hint_dir",
    "build_credential",
    "codecs_to_kind",
    "config_path",
    "create_session",
    "describe",
    "exchange_ticket",
    "human_size",
    "is_ticket_url",
    "load_network_settings",
    "load_preferences",
    "merge_video_audio",
    "parse_bv",
    "parse_count",
    "parse_duration",
    "parse_login_cookies",
    "reset_cache",
    "sanitize_filename",
    "save_preferences",
    "select_streams_from_dash",
]
