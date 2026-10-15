"""界面样式与行为常量。

从旧 config.py 的 STYLE_CONFIG 迁移而来，保留配色与字号，
并补充下载/队列相关的可调参数。
"""

from __future__ import annotations

STYLE: dict[str, object] = {
    # 颜色
    "main_bg": "#f0f2f5",
    "frame_bg": "#ffffff",
    "accent_color": "#fb7299",
    "text_color": "#333333",
    "muted_text": "#555555",
    "btn_bg": "#00a1d6",
    "btn_hover_bg": "#00b5e9",
    "entry_bg": "#2c2c2c",
    "bg_image_opacity": 0.9,
    # 字体
    "font": ("Microsoft YaHei", 10),
    "title_font": ("Microsoft YaHei", 12, "bold"),
    "normal_font": ("Microsoft YaHei", 12),
    "small_font": ("Microsoft YaHei", 10),
}

# 主窗口尺寸与标题
WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 800
# 用户可见的产品名（同时用作可执行文件名与桌面快捷方式名）。
# 注意：内部 Python 包名仍为 bilidown、用户数据目录仍为 %LOCALAPPDATA%\bilidown，
# 改名会导致资源目录与包名撞车（见 test_lint 的 resources 同名检查）并丢失已保存的配置。
PRODUCT_NAME = "Amedown"
WINDOW_TITLE = PRODUCT_NAME

# 下载行为
DOWNLOAD_CHUNK_SIZE = 1024 * 256
DOWNLOAD_TIMEOUT = (10, 60)  # (连接, 读取) 秒
DOWNLOAD_RETRIES = 3
DOWNLOAD_BACKOFF_BASE = 1.5  # 退避基数（秒）
MAX_CONCURRENT_DOWNLOADS = 2

# 进度刷新节流（毫秒）。修复 B6：不再用 50ms 全表重建
UI_REFRESH_INTERVAL_MS = 400

# 搜索结果分页
SEARCH_PAGE_SIZE = 20

# B 站请求 UA/Referer
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
