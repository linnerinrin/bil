"""bilidown —— Bilibili 视频/音频下载器。

分层结构：
    bilidown.config  : 常量与路径解析
    bilidown.core    : 事件循环、B 站 API 客户端、下载器、任务队列、凭据
    bilidown.ui      : Tkinter 界面（仅布局与事件绑定）
    bilidown.utils   : 日志等通用工具
"""

__version__ = "2.0.0"
__all__ = ["__version__"]
