#架构作用：在Tk主线程协调视图、登录、偏好、下载任务与后台工作线程
"""应用控制器：装配各层、编排异步流程、驱动视图

生命周期：
    BilidownApp()  -> 构建窗口与视图，恢复上次登录
    app.run()      -> 进入主循环
    app.shutdown() -> 关闭时取消轮询、停止任务、关闭事件循环
"""

from __future__ import annotations  #延迟解析类型标注，兼容运行时类型声明

#标准库：处理头像字节、线程、窗口及文件路径
import io  #提供头像字节缓冲区
import sys  #识别冻结运行环境
import threading  #启动并跟踪后台工作线程
import tkinter as tk  #构建窗口与捕获窗口异常
from typing import Any  #标注通用回调和用户资料

from PIL import Image  #解码头像图片

from pathlib import Path  #表示下载目录

#配置层：解析下载位置与读取界面刷新参数
from .config.paths import default_download_dir, ensure_dir, ensure_download_dir  #确保下载目录可用
from .config.settings import (  #引入超时与刷新周期配置
    DOWNLOAD_TIMEOUT,
    UI_REFRESH_INTERVAL_MS,
)
#核心层：客户端、领域模型及任务管理统一从包入口获取
from .core import (  #装配应用运行时所需核心符号
    BackgroundLoop,
    BiliClient,
    ClientError,
    DownloadFormat,
    DownloadKind,
    DownloadTask,
    TaskManager,
    VideoItem,
    parse_bv,
)
from .core.qrlogin import QrState  #匹配扫码轮询状态
from .core.network import (  #应用网络策略并创建头像请求会话
    apply_library_settings,
    create_session,
    describe,
    load_network_settings,
)
from .core.preferences import (  #读取、保存并标准化用户偏好
    Preferences,
    load as load_preferences,
    save as save_preferences,
)
from .ui.settings_view import SettingsView  #显示设置页面
from .utils.logger import get_logger, setup_logging  #初始化与记录应用日志
from .ui.download_dialog import ask_download_options  #收集下载选项
from .ui.download_view import DownloadView  #显示和操作下载任务
from .ui.login_view import LoginView  #显示扫码登录状态
from .ui.profile_view import ProfileView  #显示用户资料
from .ui.search_view import SearchView  #显示检索结果
from .ui.widgets import ask_yes_no, show_error  #提供确认和错误对话框
from .ui.window import MainWindow  #管理主窗口与导航

_log = get_logger("app")  #复用应用控制器日志器

#二维码轮询间隔（秒）
_QR_POLL_INTERVAL = 1.2  #每次扫码状态查询的等待秒数
#二维码有效期（秒），到期后主动停止轮询
_QR_LIFETIME = 180  #超时后终止当前二维码轮询


class BilidownApp:  #聚合窗口、核心服务与后台流程
    """应用主控制器"""

    def __init__(self) -> None:  #初始化网络、下载、窗口及会话恢复
        setup_logging()  #在启动其他组件前配置日志
        _log.info("启动中…（frozen=%s）", getattr(sys, "frozen", False))  #记录打包运行状态

        #统一网络策略：默认绕过失效的系统代理，可按需在config.json中开启代理
        net_settings = load_network_settings()  #读取用户代理等网络偏好
        apply_library_settings(net_settings)  #同步第三方库网络策略
        _log.info("网络策略: %s", describe(net_settings))  #记录脱敏后的代理策略描述

        self.loop = BackgroundLoop.instance()  #取得常驻后台事件循环
        self.client = BiliClient(self.loop)  #让API客户端复用该循环

        #偏好：下载目录/背景等用户设置（首次运行会自动生成配置文件）
        self.prefs: Preferences = load_preferences()  #读取磁盘上的用户偏好
        self.download_dir = self._resolve_download_dir(self.prefs)  #解析可用下载目录
        self.tasks = TaskManager(  #以有效目录和并发上限创建任务管理器
            self.client, str(self.download_dir), bridge=self.loop,
            max_workers=self.prefs.max_concurrent,
        )
        _log.debug("核心层就绪，下载目录=%s", self.download_dir)  #记录任务输出位置

        self.window = MainWindow()  #创建主窗口与导航容器
        self.window.on_navigate(self.navigate)  #注册页面切换回调
        self.window.on_logout(self.handle_logout)  #注册退出登录回调
        if self.prefs.background_path:  #有自定义背景时替换默认背景
            self.window.set_background(self.prefs.background_path)  #设置偏好背景
        _log.debug("主窗口构建完成")  #标记界面初始化完成

        self.search_view: SearchView | None = None  #保存当前搜索视图引用
        self.login_view: LoginView | None = None  #保存当前登录视图引用
        self.profile_view: ProfileView | None = None  #保存当前资料视图引用
        self.download_view: DownloadView | None = None  #保存当前下载视图引用
        self.settings_view: SettingsView | None = None  #保存当前设置视图引用

        self._current_view = ""  #记录导航中的当前页面
        self._refresh_job: str | None = None  #保存周期刷新定时器标识

        #会话/轮询状态
        self._epoch = 0  #登录代际用于丢弃旧登录资料回调
        self._search_seq = 0  #搜索请求序号用于丢弃过期搜索回调
        self._qr_poll_id = 0  #单调递增的轮询编号，用于失效旧轮询线程
        self._qr_polling = False  #控制扫码轮询线程运行
        self._user_info: dict[str, Any] | None = None  #缓存当前账号资料
        self._workers: set[threading.Thread] = set()  #跟踪在飞工作线程
        self._worker_lock = threading.Lock()  #保护工作线程集合
        self._shutting_down = False  #阻止退出阶段新增线程和回调

        self.tasks.add_listener(lambda _task: self._schedule_download_refresh())  #任务状态变化时请求界面刷新

        self._restore_session()  #后台验证历史凭据
        self.navigate("search")  #首次进入搜索页面
        _log.info("启动完成，界面已就绪")  #记录初始化完成

    #=============================================================== 导航
    def navigate(self, key: str) -> None:  #根据导航键重建页面
        """切换视图"""
        builders = {  #将页面名称映射到构建方法
            "search": self._build_search_view,
            "login": self._build_login_view,
            "profile": self._build_profile_view,
            "settings": self._build_settings_view,
            "downloads": self._build_download_view,
        }
        builder = builders.get(key)  #查找目标页面构建器
        if builder is None:  #忽略不支持的导航键
            return  #不破坏当前视图

        self._stop_qr_polling()  #离开当前页面时停止扫码轮询
        self.window.clear_content()  #清除旧页面内容
        self._current_view = key  #更新当前页面标识
        builder()  #构建目标页面
        self._update_status()  #同步登录和下载目录状态

    @staticmethod  #下载目录解析不依赖实例状态
    def _resolve_download_dir(prefs: Preferences) -> Path:  #依次尝试偏好目录与默认目录
        """把偏好里的下载目录解析成真实可用目录

        用户目录不存在或无权限时回退到默认目录
        """
        target = prefs.effective_download_dir()  #展开用户选择的下载目录
        resolved = ensure_dir(target)  #确认目录可创建并可写
        if resolved is not None:  #优先使用配置目录
            return resolved  #返回已确认可用的位置

        _log.warning("下载目录不可用，回退到默认目录: %s", target)  #prefs保留用户期望路径，仅运行时回退
        fallback = ensure_dir(default_download_dir())  #尝试程序默认下载目录
        if fallback is not None:  #默认目录可用时优先返回
            return fallback  #将新任务放进默认目录
        return ensure_download_dir()  #使用最终保底目录

    def _update_status(self) -> None:  #在状态栏显示登录状态和实际下载目录
        who = "已登录" if self.client.logged_in else "未登录"  #读取当前认证状态
        self.window.set_status(f"{who}｜下载目录：{self.download_dir}")  #展示实际工作路径

    #------------------------------------------------------------ 视图构建
    def _build_search_view(self) -> None:  #创建搜索页并接入各项用户操作
        view = SearchView(
            self.window.content_frame,
            on_search=self._on_search,
            on_copy=self._on_copy,
            on_download=self._on_download,
            on_batch=self._on_batch,
        )
        view.pack(fill=tk.BOTH, expand=True)
        self.search_view = view

    def _build_login_view(self) -> None:  #创建扫码登录页并同步当前认证状态
        view = LoginView(
            self.window.content_frame,
            on_refresh=self.start_qr_login,
            on_logout=self.handle_logout,
        )
        view.pack(fill=tk.BOTH, expand=True)
        view.set_logged_in(self.client.logged_in)
        if self.client.logged_in:
            name = (self._user_info or {}).get("name") or "已登录用户"
            view.set_status(f"已登录：{name}。如需切换账号请先退出登录。")
        self.login_view = view
        if not self.client.logged_in:
            self.start_qr_login()

    #资料视图：构建资料页并加载头像
    def _build_profile_view(self) -> None:
        view = ProfileView(
            self.window.content_frame,
            self._user_info,
            on_refresh=self.refresh_user_info,
            on_login=lambda: self.navigate("login"),
        )
        view.pack(fill=tk.BOTH, expand=True)
        self.profile_view = view
        if self._user_info:
            self._load_avatar(self._user_info)  #有资料时同步加载头像

    def _build_download_view(self) -> None:  #创建任务列表页并启动周期刷新
        view = DownloadView(
            self.window.content_frame,
            on_cancel=self.tasks.cancel,
            on_retry=self._on_retry,
            on_remove=self.tasks.remove,
            on_clear_finished=self._on_clear_finished,
            on_open_folder=lambda: DownloadView.open_path(str(self.download_dir)),
            on_open_file=self._on_open_file,
        )
        view.pack(fill=tk.BOTH, expand=True)
        self.download_view = view
        view.refresh(self.tasks.tasks())
        self._start_download_refresh()

    def _build_settings_view(self) -> None:  #创建设置页并绑定应用/恢复操作
        view = SettingsView(
            self.window.content_frame,
            self.prefs,
            on_apply=self.apply_preferences,
            on_restore_defaults=self.restore_default_preferences,
        )
        view.pack(fill=tk.BOTH, expand=True)
        self.settings_view = view

    #=============================================================== 设置
    def apply_preferences(self, draft: Preferences) -> None:  #标准化、持久化并应用设置草稿
        """应用设置页提交的草稿：校验→保存→让设置立即生效"""
        prefs = draft  #SettingsView已做范围校验，normalize再兜一层
        prefs = Preferences.normalize(prefs.to_dict())  #清洗未知/异常值并规范字段

        saved = save_preferences(prefs)  #先尝试将设置写入配置文件
        self.prefs = prefs  #保留用户填写的期望路径（即使暂时不可用），实际生效目录见状态栏

        self._apply_download_dir(prefs)  #更新后续任务使用的目录
        self._apply_runtime_options(prefs)  #更新任务并发参数
        self.window.set_background(prefs.background_path)  #立即切换背景图

        if self.settings_view is not None and self.settings_view.alive:  #仅刷新仍存在的设置页
            message = "设置已保存" if saved else "设置已在本次运行生效，但写入配置文件失败（详见日志）"  #区分落盘成功与仅运行时生效
            self.settings_view.show_saved(prefs, message)  #将最终偏好和保存状态回显给用户
        self._update_status()  #同步状态栏中的有效目录和登录状态
        _log.info(
            "设置已应用: 背景=%s 目录=%s 并发=%d 重试=%d 完成后打开=%s",
            prefs.background_path or "（内置）",
            self.download_dir,
            prefs.max_concurrent,
            prefs.download_retries,
            prefs.open_folder_when_done,
        )

    def restore_default_preferences(self) -> None:
        """恢复默认设置（保留代理等未在界面暴露的字段）"""
        defaults = Preferences.normalize(  #用默认字段重建草稿并通过统一校验
            {
                #网络设置属于用户在配置文件里的选择，恢复默认时不动它
                "use_system_proxy": self.prefs.use_system_proxy,
                "proxy": self.prefs.proxy,
            }
        )
        self.apply_preferences(defaults)  #复用常规保存和即时应用流程

    def _apply_download_dir(self, prefs: Preferences) -> None:  #更新后续任务目录及当前列表显示
        """让新的下载目录生效（只影响之后新建的任务）"""
        resolved = self._resolve_download_dir(prefs)  #解析并验证新下载目录
        if resolved != prefs.effective_download_dir():  #用户目录不可用、已回退默认目录时明确提示
            self.window.set_status(
                f"下载目录不可用，已回退到：{resolved}（设置中仍保留原路径，可修正后重新应用）"
            )
        if resolved == self.download_dir:  #路径未变时不重复更新任务管理器
            return  #保留当前目录与视图状态
        self.download_dir = resolved  #保存实际生效的目录
        self.tasks.set_download_dir(str(resolved))  #让新任务使用新位置
        if self.download_view is not None and self.download_view.alive:  #下载页可见时同步刷新列表
            self.download_view.refresh(self.tasks.tasks())

    def _apply_runtime_options(self, prefs: Preferences) -> None:
        """并发上限立即生效；重试次数由下载器在每次下载时读取偏好"""
        self.tasks.set_max_workers(prefs.max_concurrent)  #立即调整任务执行并发上限

    #=============================================================== 登录
    def _restore_session(self) -> None:
        """启动时恢复上次登录状态并在后台校验"""
        if not self.client.restore_credential():  #本地无可恢复凭据时跳过校验
            return  #保持未登录初始状态
        self.window.set_logged_in(True)  #暂时显示凭据恢复中
        self.window.set_user_text("正在恢复登录…")  #告知用户后台验证正在进行

        def worker() -> None:  #在线程中验证凭据并获取账号资料
            valid = self.client.validate_credential()  #返回None表示暂时失败，不据此登出
            if valid is False:  #凭据明确无效时清除登录并引导重新扫码
                _log.info("本地凭据已失效，需要重新登录")  #记录凭据失效
                self._marshal(self._on_credential_invalid)  #将登出状态切回Tk主线程
                return  #不再请求用户资料
            self._fetch_user_info(self._epoch)  #验证通过后加载当前账号资料

        self._spawn(worker, "bilidown-restore")  #后台运行恢复校验

    def _on_credential_invalid(self) -> None:  #清除失效会话并更新登录界面
        self.client.logout()  #删除客户端中的认证状态
        self._user_info = None  #清除缓存用户资料
        self.window.set_logged_in(False)  #将主窗口认证状态改为未登录
        self.window.set_user_text("登录已过期，请重新扫码")  #显示重新登录提示
        if self._current_view == "login" and self.login_view is not None:  #登录页可见时同步控件状态
            self.login_view.set_logged_in(False)  #解锁扫码登录操作
            self.start_qr_login()  #重新生成二维码并开始轮询

    #二维码登录：生成、轮询并按当前代际回填状态
    def start_qr_login(self) -> None:
        """生成二维码并开始轮询，完整处理四种扫码状态"""
        if self._shutting_down:  #退出时不启动新的二维码任务
            return  #让现有清理流程结束即可
        self._stop_qr_polling()  #终止上一轮二维码轮询
        epoch = self._epoch  #绑定本次请求的登录代际

        if self.login_view is not None:  #登录页存在时展示生成中状态
            self.login_view.set_refreshing(True)  #禁用重复刷新并显示忙碌状态
            self.login_view.set_status("正在生成二维码…")  #告知用户等待二维码

        def worker() -> None:  #在后台请求二维码并向主线程投递结果
            try:
                png = self.client.login_qrcode_start()  #从登录API获取二维码图像数据
            except BaseException as exc:  #noqa: BLE001 - 含CancelledError（退出时取消）
                _log.warning("生成二维码失败: %r", exc)  #记录生成异常以便排查
                self._marshal(lambda m=str(exc) or type(exc).__name__: self._on_qr_failed(m))  #在Tk线程显示失败状态
                return  #失败后不投递成功回调
            self._marshal(lambda: self._on_qr_ready(png, epoch))  #返回二维码并绑定请求代际

        self._spawn(worker, "bilidown-qr")  #后台执行二维码生成

    def _on_qr_ready(self, png: bytes, epoch: int) -> None:
        if (  #核对代际、当前视图与登录页实例，旧二维码回调不得重启轮询
            epoch != self._epoch
            or self._current_view != "login"
            or self.login_view is None
            or self._shutting_down
        ):
            return  #丢弃过期、离开登录页或关机期间的结果
        self.login_view.set_refreshing(False)  #结束页面上的忙碌状态
        self.login_view.set_qrcode(png)  #显示新生成的二维码
        self.login_view.set_status("请使用哔哩哔哩客户端扫码")  #提示用户扫码
        self._start_qr_polling(epoch)  #开始检查扫码进度

    def _on_qr_failed(self, message: str) -> None:
        if self._shutting_down:  #关闭后不再弹出错误框或触碰控件
            return  #忽略退出期间回调
        if self.login_view is not None and self.login_view.alive:  #页面仍有效时回显二维码错误
            self.login_view.set_refreshing(False)  #允许用户再次请求二维码
            self.login_view.set_status(f"二维码生成失败：{message}", accent=False)  #显示失败原因
            show_error("登录失败", f"无法生成登录二维码：\n{message}")  #通过对话框提示不可恢复错误

    def _start_qr_polling(self, epoch: int) -> None:
        #递增轮询编号，使仍在睡眠的旧轮询线程失效
        self._qr_poll_id = getattr(self, "_qr_poll_id", 0) + 1  #递增编号使旧线程失效
        poll_id = self._qr_poll_id  #把本轮编号绑进工作线程
        self._qr_polling = True  #打开当前扫码轮询开关

        def worker() -> None:  #在后台按有效期周期查询扫码状态
            import time  #使用单调时钟和间隔等待

            deadline = time.monotonic() + _QR_LIFETIME  #用单调时钟计算本轮二维码截止时间
            while (  #编号、代际、退出状态和有效期均有效时继续查询
                self._qr_polling
                and poll_id == self._qr_poll_id
                and epoch == self._epoch
                and not self._shutting_down
                and time.monotonic() < deadline
            ):
                try:
                    state = self.client.login_qrcode_poll()  #查询远端二维码当前状态
                except BaseException as exc:  #noqa: BLE001
                    _log.warning("二维码状态查询失败: %r", exc)  #记录暂时性查询错误
                    time.sleep(_QR_POLL_INTERVAL)  #等待后重试本轮查询
                    continue  #不把暂时异常当作最终状态
                self._marshal(lambda s=state: self._on_qr_state(s, epoch))  #主线程更新扫码状态
                if state in (QrState.DONE, QrState.TIMEOUT):  #成功或过期后无需继续查询
                    return  #二维码终态已处理，结束当前线程
                time.sleep(_QR_POLL_INTERVAL)  #未到终态时延时后继续轮询

        self._spawn(worker, "bilidown-qrpoll")  #在线程池管理之外启动守护线程

    def _stop_qr_polling(self) -> None:  #请求轮询线程在下一次检查时退出
        self._qr_polling = False  #关闭当前扫码轮询开关
        self._qr_poll_id = getattr(self, "_qr_poll_id", 0) + 1  #同时作废在飞旧线程的编号

    def _on_qr_state(self, state: Any, epoch: int) -> None:
        if epoch != self._epoch:  #忽略旧登录代际的轮询结果
            return  #避免过期二维码改变当前界面
        view = self.login_view  #取得当前登录页实例
        if view is None:  #页面已销毁时不更新控件
            return

        if state is QrState.SCAN:
            view.set_status("二维码已扫描，请在手机上确认登录", accent=False)
        elif state is QrState.CONF:
            view.set_status("已确认，正在完成登录…")
        elif state is QrState.TIMEOUT:
            #二维码过期时停止轮询并提示重新生成
            self._stop_qr_polling()
            view.set_refreshing(False)
            view.show_qrcode_expired()
        elif state is QrState.DONE:
            self._stop_qr_polling()
            view.set_status("登录成功！")
            self._complete_login(epoch)

    def _complete_login(self, epoch: int) -> None:
        try:
            self.client.login_qrcode_credential()
        except Exception as exc:  #noqa: BLE001
            _log.exception("获取登录凭据失败")
            show_error("登录失败", f"无法获取登录凭据：{exc}")
            return

        self.window.set_logged_in(True)
        self.window.set_user_text("登录成功，正在获取资料…")
        if self.login_view is not None:
            self.login_view.set_logged_in(True)
        self._epoch += 1
        self._sync_login_views_epoch()

        def worker() -> None:
            self._fetch_user_info(epoch + 1)

        self._spawn(worker, "bilidown-userinfo")

    def _sync_login_views_epoch(self) -> None:
        """登录成功后让已存在的视图退出轮询状态"""
        if self.login_view is not None:
            self.login_view.set_logged_in(True)

    def handle_logout(self) -> None:
        if self.client.logged_in and not ask_yes_no("退出登录", "确定要退出当前账号吗？"):
            return
        self._epoch += 1
        self._stop_qr_polling()
        self.client.logout()
        self._user_info = None
        self.window.set_logged_in(False)
        self.window.set_user_text("未登录")
        if self.login_view is not None:
            self.login_view.set_logged_in(False)
            self.login_view.set_status("已退出登录，请重新扫描二维码")
        self._update_status()

    #=============================================================== 用户资料
    def _fetch_user_info(self, epoch: int) -> None:
        try:
            info = self.client.fetch_self_info()
        except Exception as exc:  #noqa: BLE001
            _log.warning("获取用户信息失败: %s", exc)
            #获取失败时校验一次凭据，明确失效则清除并引导重新扫码
            if self.client.validate_credential() is False:  #仅在明确无效时清除，暂时失败保留凭据
                _log.info("凭据校验不通过，将清除本地凭据并要求重新登录")
                self._marshal(self._on_credential_invalid)
                return
            reason = str(exc).splitlines()[0][:120] if str(exc) else exc.__class__.__name__
            self._marshal(lambda r=reason: self._on_user_info_failed(r))
            return
        self._marshal(lambda: self._apply_user_info(info, epoch))

    def _on_user_info_failed(self, reason: str) -> None:
        if self._shutting_down:
            return
        self.window.set_user_text("已登录（资料获取失败）")
        self.window.set_status(f"资料获取失败：{reason}")
        show_error(
            "获取资料失败",
            "已登录，但获取用户资料失败：\n"
            f"{reason}\n\n"
            "可点击「我的」页面的「刷新资料」重试。",
        )

    def _apply_user_info(self, info: dict[str, Any], epoch: int) -> None:
        if epoch != self._epoch:
            return
        self._user_info = info
        name = info.get("name") or "未设置昵称"
        self.window.set_user_text(f"欢迎，{name}")
        self.window.set_logged_in(True)
        self._update_status()
        if self._current_view == "profile":
            #先销毁旧资料页再重建，重建时会加载头像
            if self.profile_view is not None and self.profile_view.alive:
                self.profile_view.destroy()
            self._build_profile_view()
        else:
            self._load_avatar(info)  #非资料页时仅按需更新已存在的头像

    def refresh_user_info(self) -> None:
        if not self.client.logged_in:
            show_error("未登录", "请先扫码登录")
            return
        epoch = self._epoch
        self._spawn(lambda: self._fetch_user_info(epoch), "bilidown-userinfo")

    def _load_avatar(self, info: dict[str, Any]) -> None:
        url = info.get("face")
        if not url or self.profile_view is None:
            return
        epoch = self._epoch

        def worker() -> None:
            session = create_session()
            try:
                resp = session.get(url, timeout=DOWNLOAD_TIMEOUT)
                resp.raise_for_status()
                image = Image.open(io.BytesIO(resp.content)).convert("RGB")
            except Exception:  #noqa: BLE001 - 头像失败不影响主流程
                _log.info("头像加载失败", exc_info=True)
                self._marshal(lambda: self._set_avatar(None, epoch))
                return
            finally:
                session.close()
            self._marshal(lambda: self._set_avatar(image, epoch))

        self._spawn(worker, "bilidown-avatar")

    def _set_avatar(self, image: Image.Image | None, epoch: int) -> None:
        if epoch != self._epoch or self.profile_view is None:
            return
        self.profile_view.set_avatar(image)

    #=============================================================== 搜索
    #搜索：后台查询结果经主线程回填当前搜索视图
    def _on_search(self, keyword: str, page: int) -> None:
        #每次搜索递增序号，慢的旧请求回调按序号丢弃
        self._search_seq = getattr(self, "_search_seq", 0) + 1  #单调递增的请求序号
        seq = self._search_seq  #把本次序号绑进回调

        def worker() -> None:
            try:
                bvid = parse_bv(keyword)
                if bvid:
                    item = self.client.fetch_by_bvid(bvid)
                    result_items = [item]
                    used_keyword = bvid
                else:
                    result = self.client.search_videos(keyword, page)
                    result_items = result.items
                    used_keyword = result.keyword
            except ClientError as exc:
                self._marshal(lambda m=str(exc): self._on_search_failed(m, seq))  #过期回调按序号丢弃
                return
            except Exception as exc:  #noqa: BLE001
                _log.exception("搜索异常")
                self._marshal(lambda m=f"搜索失败：{exc}": self._on_search_failed(m, seq))  #过期回调按序号丢弃
                return
            self._marshal(
                lambda: self._render_search(result_items, used_keyword, page, seq)  #仅最新序号可回填结果
            )

        view = self.search_view
        if view is not None:
            view.set_loading(True)
        self._spawn(worker, "bilidown-search")

    def _on_search_failed(self, message: str, seq: int | None = None) -> None:
        if seq is not None and seq != self._search_seq:  #已有更新的搜索在飞，忽略旧失败
            return
        view = self.search_view
        if view is None:
            return
        view.set_loading(False)
        view.set_tip(message)
        view.clear_canvas()

    def _render_search(
        self, items: list[VideoItem], keyword: str, page: int, seq: int | None = None
    ) -> None:
        if seq is not None and seq != self._search_seq:  #已有更新的搜索在飞，忽略旧结果
            return
        view = self.search_view
        if view is None:
            return
        view.render(items, keyword, page)

    def _on_copy(self, item: VideoItem) -> None:
        try:
            import pyperclip

            pyperclip.copy(item.copy_text())
            self.window.set_status(f"已复制：{item.title}")
        except Exception as exc:  #noqa: BLE001
            show_error("复制失败", f"无法写入剪贴板：{exc}")

    #=============================================================== 下载
    def _on_download(self, item: VideoItem, kind: str) -> None:
        self._ask_and_enqueue([item], prefill=kind)

    def _on_batch(self, items: list[VideoItem], kind: str) -> None:
        self._ask_and_enqueue(list(items), prefill=kind)

    #下载：补齐分页、读取对话框选项并逐项入队
    def _ask_and_enqueue(
        self, items: list[VideoItem], *, prefill: str = "video"
    ) -> None:
        """先补齐分P列表，再弹下载设置窗，最后按选择入队"""
        if not items:
            return

        def worker() -> None:
            pages_failed = False  #记录是否有视频的分P列表获取失败
            for item in items:
                if not item.pages:
                    try:
                        item.pages = self.client.fetch_pages(item.bvid)
                    except ClientError:
                        item.pages = []  #失败时回退单P，并由对话框提示用户
                        pages_failed = True
            self._marshal(lambda: self._open_download_dialog(items, prefill, pages_failed=pages_failed))

        self._spawn(worker, "bilidown-pages")

    def _open_download_dialog(
        self, items: list[VideoItem], prefill: str = "video", *, pages_failed: bool = False
    ) -> None:
        if pages_failed:  #明确告知分页缺失，避免用户误以为视频只有单P
            self.window.set_status("部分视频的分 P 列表获取失败，已按单 P 处理，可稍后重试")
        single = len(items) == 1
        prefill_format = {
            "audio": DownloadFormat.AUDIO,
            "cover": DownloadFormat.COVER,
        }.get(prefill, DownloadFormat.VIDEO)
        choice = ask_download_options(
            self.window.root,
            items[0],
            pages=items[0].pages if single else [],  #批量传空列表：对话框只展示单P，与强制P1的入队行为一致
            logged_in=self.client.logged_in,
            prefill_format=prefill_format,
        )
        if choice is None:
            return

        kind = (
            DownloadKind.VIDEO
            if choice.format is DownloadFormat.VIDEO
            else DownloadKind.AUDIO
        )
        #单条：勾了几个分P就建几个任务；批量：每个视频只处理P1（对话框已不展示分P选择）
        page_indices = choice.page_indices if single else [1]  #批量模式每项固定下载第1P
        created = 0
        duplicated = 0
        for item in items:
            for page_index in page_indices:
                task = DownloadTask(
                    item=item,
                    kind=kind,
                    format=choice.format,
                    qn=choice.qn,
                    audio_qn=choice.audio_qn,
                    page_index=page_index,
                )
                submitted = self.tasks.submit(task)
                if submitted is task:
                    created += 1
                else:
                    duplicated += 1

        label = choice.format.label
        message = f"已加入 {created} 个{label}下载任务，可在「下载列表」查看进度"
        if duplicated:
            message += f"（{duplicated} 个重复任务已跳过）"
        self.window.set_status(message)
        self._schedule_download_refresh()

    def _on_retry(self, task: DownloadTask) -> None:
        self.tasks.retry(task)
        self._schedule_download_refresh()

    def _on_remove(self, task: DownloadTask) -> None:
        self.tasks.remove(task)
        self._schedule_download_refresh()

    def _on_clear_finished(self) -> None:
        removed = self.tasks.clear_finished()
        self.window.set_status(f"已清理 {removed} 条已结束的任务")
        self._schedule_download_refresh()

    def _on_open_file(self, task: DownloadTask) -> None:
        path = task.save_path
        if not path:
            show_error("无法打开", "该任务还没有产出文件")
            return
        try:
            DownloadView.open_path(path)
        except Exception as exc:  #noqa: BLE001
            show_error("无法打开", f"打开文件失败：{exc}")

    def _schedule_download_refresh(self) -> None:
        """从任意线程请求一次界面刷新"""
        self._marshal(self._refresh_download_view)

    def _refresh_download_view(self) -> None:
        if self.download_view is not None and self.download_view.alive:
            self.download_view.refresh(self.tasks.tasks())

    def _start_download_refresh(self) -> None:
        """启动周期性刷新（仅当下载列表视图存在时）"""
        self._stop_download_refresh()
        self._tick_download_refresh()

    def _tick_download_refresh(self) -> None:
        if self.download_view is None or not self.download_view.alive:
            self._refresh_job = None
            return
        self.download_view.refresh(self.tasks.tasks())
        self._refresh_job = self.window.after(
            UI_REFRESH_INTERVAL_MS, self._tick_download_refresh
        )

    def _stop_download_refresh(self) -> None:
        self.window.cancel_after(self._refresh_job)
        self._refresh_job = None

    #=============================================================== 线程编排
    def _spawn(self, target: Any, name: str) -> None:
        """启动并跟踪一个守护工作线程（退出时不再接受新线程）"""
        if self._shutting_down:
            _log.debug("应用正在退出，忽略新的工作线程 %s", name)
            return

        def _wrapped() -> None:
            try:
                target()
            finally:
                with self._worker_lock:
                    self._workers.discard(thread)

        thread = threading.Thread(target=_wrapped, name=name, daemon=True)
        with self._worker_lock:
            self._workers.add(thread)
        thread.start()

    def _marshal(self, callback: Any) -> None:
        """把回调切回Tk主线程执行（线程安全）"""
        if self._shutting_down:
            return
        try:
            self.window.root.after(0, callback)
        except (RuntimeError, tk.TclError):
            _log.debug("主窗口已关闭，丢弃回调")

    #=============================================================== 生命周期
    def run(self) -> None:
        self.window.root.protocol("WM_DELETE_WINDOW", self.shutdown)
        #自绘关闭按钮同样接入清理逻辑
        self.window.on_close(self.shutdown)
        try:
            self.window.mainloop()
        finally:
            self._teardown()

    def shutdown(self) -> None:
        """用户关闭窗口：先清理资源，再销毁窗口"""
        self._teardown()
        self.window.destroy()

    def _teardown(self) -> None:
        if getattr(self, "_torn_down", False):
            return
        self._torn_down = True
        self._shutting_down = True  #先阻止新的回调与工作线程
        _log.info("正在退出…")
        self._stop_qr_polling()
        self._stop_download_refresh()
        self._join_workers(timeout=1.5)
        try:
            self.tasks.shutdown(wait=False)
        except Exception:  #noqa: BLE001
            _log.debug("关闭任务管理器失败", exc_info=True)
        try:
            self.loop.shutdown(timeout=3.0)
        except Exception:  #noqa: BLE001
            _log.debug("关闭事件循环失败", exc_info=True)
        BackgroundLoop._instance = None  #noqa: SLF001 - 允许下次重建
        _log.info("已退出")

    def _join_workers(self, timeout: float) -> None:
        """短暂等待在飞的工作线程结束，避免它们撞上已关闭的事件循环"""
        import time

        with self._worker_lock:
            workers = [t for t in self._workers if t.is_alive()]
        if not workers:
            return
        deadline = time.monotonic() + timeout
        for thread in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        with self._worker_lock:
            still = [t.name for t in self._workers if t.is_alive()]
        if still:
            _log.debug("仍有工作线程在运行（守护线程，随进程退出）: %s", still)


def main() -> None:
    """控制台入口（python -m bilidown）

    环境变量BILIDOWN_SELFTEST=1时：构建界面但不进入主循环，打印结果后退出
    用于打包产物的自动化冒烟验证（无需人工点关窗口）
    """
    import os

    self_test = os.environ.get("BILIDOWN_SELFTEST") == "1"
    app = BilidownApp()
    if self_test:
        try:
            app.window.root.update_idletasks()
            app.window.root.update()
            print("SELFTEST_OK 视图=", app._current_view)
            print("SELFTEST_LOG ", _log.handlers)
            _log.info("打包自检通过：界面已成功构建")
        finally:
            app.shutdown()
        print("SELFTEST_DONE")
        return
    app.run()


if __name__ == "__main__":  #仅直接运行模块时调用入口
    main()  #启动应用主流程
