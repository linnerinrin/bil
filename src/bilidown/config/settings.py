"""界面样式与行为常量"""

from __future__ import annotations

STYLE: dict[str, object] = {
    #颜色
    "main_bg": "#f0f2f5",
    "frame_bg": "#ffffff",
    "accent_color": "#fb7299",
    "text_color": "#333333",
    "muted_text": "#555555",
    "btn_bg": "#00a1d6",
    "btn_hover_bg": "#00b5e9",
    "entry_bg": "#2c2c2c",
    "bg_image_opacity": 0.8,
    #字体
    "font": ("Microsoft YaHei", 10),
    "title_font": ("Microsoft YaHei", 12, "bold"),
    "normal_font": ("Microsoft YaHei", 12),
    "small_font": ("Microsoft YaHei", 10),
}

#主窗口尺寸与标题
WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 800
#用户可见的产品名（同时用作可执行文件名与桌面快捷方式名）
#注意：改名不影响内部包名bilidown与用户数据目录%LOCALAPPDATA%\bilidown
PRODUCT_NAME = "Amedown"
WINDOW_TITLE = PRODUCT_NAME

#下载行为
DOWNLOAD_CHUNK_SIZE = 1024 * 256
DOWNLOAD_TIMEOUT = (10, 60)  #(连接,读取)秒
DOWNLOAD_RETRIES = 3
DOWNLOAD_BACKOFF_BASE = 1.5  #退避基数（秒）
MAX_CONCURRENT_DOWNLOADS = 2

#进度刷新节流（毫秒）
UI_REFRESH_INTERVAL_MS = 400

#搜索结果分页
SEARCH_PAGE_SIZE = 20

#B站请求UA/Referer
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
