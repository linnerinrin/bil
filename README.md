# Amedown

Bilibili 视频/音频下载器（Tkinter 桌面客户端）。内部 Python 包名为 `bilidown`
（`python -m bilidown`），用户数据目录为 `%LOCALAPPDATA%\bilidown`。

---

## 1. 项目结构、核心文件与技术栈

```
downloader/
├─ run.py                    # 入口（等价于 python -m bilidown）
├─ run.spec                  # PyInstaller 打包配置
├─ pyproject.toml            # 依赖与工具配置
├─ src/bilidown/
│  ├─ __main__.py            # python -m bilidown 入口
│  ├─ app.py                 # 控制器：装配各层、编排异步流程、页面导航
│  ├─ config/
│  │  ├─ paths.py            # 只读资源/可写用户数据的路径解析（打包/开发双模式）
│  │  └─ settings.py         # 样式与行为常量（窗口尺寸、颜色、透明度等）
│  ├─ core/
│  │  ├─ bridge.py           # 常驻后台事件循环，全进程唯一，UI 线程只通过回调更新
│  │  ├─ client.py           # B 站 API 门面：搜索、视频信息、流地址、登录态；含纯函数工具
│  │  ├─ qrlogin.py          # 扫码登录：二维码生成/轮询/换票/Cookie 解析
│  │  ├─ credential.py       # 凭据持久化，Windows DPAPI 加密，原子写入
│  │  ├─ downloader.py       # 分块下载、断点续传、重试退避、ffmpeg 音视频合并
│  │  ├─ tasks.py            # 任务队列与生命周期：提交/取消/重试/并发控制/查重
│  │  ├─ models.py           # 领域模型与状态机：DownloadTask、VideoItem、枚举
│  │  ├─ network.py          # 网络策略：代理配置、会话工厂、bilibili_api 联动
│  │  └─ preferences.py      # 偏好设置模型与持久化（config.json）
│  ├─ ui/                    # 仅布局与事件绑定，不发请求、不做 IO
│  │  ├─ window.py           # 主窗口与自绘标题栏、导航、亚克力背景
│  │  ├─ base.py             # 页面基类
│  │  ├─ widgets.py          # 滚动容器等通用控件
│  │  ├─ login_view.py       # 扫码登录页
│  │  ├─ search_view.py      # 搜索页（结果列表、分页）
│  │  ├─ download_dialog.py  # 下载前弹窗（分 P/格式/清晰度选择）
│  │  ├─ download_view.py    # 下载任务列表页
│  │  ├─ profile_view.py     # 个人信息页
│  │  └─ settings_view.py    # 设置页
│  ├─ utils/logger.py        # UTF-8 日志，2MB 轮转 ×3
│  └─ resources/             # 内置背景图
└─ tests/                    # pytest 测试套件
```

### 技术栈

| 层 | 技术 |
| --- | --- |
| UI | tkinter + sv-ttk（主题）+ pywinstyles（亚克力/圆角，仅 Windows）+ Pillow（图片） |
| B 站接口 | bilibili-api-python（异步，基于 aiohttp） |
| 并发模型 | asyncio 常驻后台事件循环（`core/bridge.py`）+ 线程池执行下载任务 |
| HTTP 下载 | requests 分块流式下载 |
| 音视频处理 | ffmpeg（流拷贝合并，经 imageio-ffmpeg 提供），回退 moviepy |
| 凭据加密 | Windows DPAPI（ctypes 调 crypt32），非 Windows 降级为 base64 |
| 打包 | PyInstaller（run.spec） |

---

## 2. 可用 API 列表

以下均为后端公开接口，UI 不直接触网，一律经 `app.py`（BilidownApp）编排。

### core/client.py — BiliClient（同步门面，内部走后台事件循环）

| API | 说明 |
| --- | --- |
| `BiliClient.restore_credential() -> bool` | 从本地恢复登录凭据（不校验远端有效性） |
| `BiliClient.save_credential()` | 持久化当前凭据 |
| `BiliClient.logout()` | 退出登录并清除凭据 |
| `BiliClient.logged_in() -> bool` | 内存中是否存在凭据 |
| `BiliClient.validate_credential() -> bool \| None` | 校验凭据：True 有效 / False 失效 / None 网络异常无法判断 |
| `BiliClient.login_qrcode_start() -> bytes` | 获取登录二维码 PNG 字节 |
| `BiliClient.login_qrcode_poll() -> QrState` | 轮询扫码状态（SCANNED/CONFIRMED/EXPIRED/WAITING 等） |
| `BiliClient.login_qrcode_credential() -> Credential` | 完成扫码登录，构造凭据 |
| `BiliClient.fetch_self_info() -> dict` | 获取当前账号资料 |
| `BiliClient.search_videos(keyword, page=1) -> SearchResult` | 关键词搜索（含分页信息） |
| `BiliClient.fetch_by_bvid(bvid) -> VideoItem` | 按 BV 号获取视频详情 |
| `BiliClient.ensure_cid(item) -> int` | 确保视频项有 cid（缺则拉取） |
| `BiliClient.fetch_pages(bvid) -> list[VideoPage]` | 获取分 P 列表 |
| `BiliClient.select_streams(item, page, fmt, qn) -> StreamSelection` | 挑选音视频流地址（含备用地址） |

纯函数工具：`parse_bv(text)`（大小写不敏感、带边界校验）、`looks_like_link(text)`、
`sanitize_filename(name)`、`parse_duration(value)`、`parse_count(value)`、
`codecs_to_kind(codecs)`、`quality_permission(qn)`、`quality_label(qn)`、
`select_streams_from_dash(dash, fmt, qn)`。

### core/tasks.py — TaskManager（任务队列）

| API | 说明 |
| --- | --- |
| `submit(task) -> DownloadTask` | 提交任务（自动查重，重复则复用原任务） |
| `cancel(task)` | 取消任务 |
| `retry(task) -> DownloadTask` | 重试失败/已取消任务（先彻底取消再重新提交） |
| `remove(task)` | 从列表移除任务 |
| `clear_finished() -> int` | 清理已结束任务，返回清理数量 |
| `tasks() -> list[DownloadTask]` | 全部任务快照 |
| `snapshot_counts() -> dict[str, int]` | 各状态任务计数 |
| `active_count() -> int` | 进行中任务数 |
| `set_download_dir(dir)` | 修改下载目录（仅对新建任务生效） |
| `set_max_workers(count)` | 调整并发上限（线程安全的信号量重建） |
| `add_listener(callback)` | 注册任务结算回调（UI 通知用） |

### core/downloader.py — MediaDownloader

| API | 说明 |
| --- | --- |
| `download(task, streams=None, download_dir=None) -> Path` | 执行下载（分块、断点续传、自动重试、可取消），在工作线程调用 |
| `close()` | 关闭下载器会话 |
| `merge_video_audio(video, audio, output, task=None)` | ffmpeg 合并音视频（优先流拷贝，失败回退 moviepy） |

### core/models.py — 领域模型

`DownloadTask`：`status / progress / message / error / cancelled` 只读属性，
`set_status() / set_progress() / fail() / cancel() / check_cancelled() / progress_text()`。
枚举：`DownloadStatus`（PENDING/DOWNLOADING/MERGING/DONE/FAILED/CANCELLED）、
`DownloadKind`（VIDEO/AUDIO/COVER）、`DownloadFormat`（MP4/M4A/MP3/JPG）。
数据类：`VideoItem`（视频信息）、`VideoPage`（分 P）。
异常：`DownloadCancelled`。

### core/qrlogin.py — LoginSession（扫码登录会话）

| API | 说明 |
| --- | --- |
| `await generate() -> bytes` | 申请二维码与密钥，返回 PNG |
| `await poll() -> QrPollResult` | 轮询扫码状态 |
| `credential() -> Credential` | 登录成功后构造 bilibili_api 凭据 |
| `png / link / cookies / has_key` | 只读属性 |

模块级：`parse_login_cookies(raw_url, refresh_token)`、`build_credential(cookies)`、
`is_ticket_url(url)`（官方域名白名单校验）、`exchange_ticket(ticket_url, refresh_token)`。

### core/credential.py — CredentialStore

`save(cookies) -> bool`（DPAPI 加密 + 原子写入）、`load() -> dict | None`（损坏自动返回空）、
`clear() -> bool`。

### core/preferences.py — Preferences / PreferenceStore

模块级便捷函数：`load() -> Preferences`、`save(prefs) -> bool`、`update(**changes) -> Preferences`、
`background_hint_dir()`。模型方法：`normalize(raw)`（越界钳制）、`effective_download_dir()`、
`copy_with(**changes)`、`to_dict()`。

### core/network.py — 网络策略

`load_network_settings(force=False)`、`create_session(settings)`、
`apply_library_settings(settings)`（同步给 bilibili_api，代理地址脱敏进日志）、
`describe(settings)`（人类可读策略描述）、`config_path()`、`reset_cache()`。

### core/bridge.py — BackgroundLoop（单例）

`instance()`、`submit(coro, on_done=None) -> Future`、`run_sync(coro, timeout)`、
`shutdown()` / `shutdown_instance()`（关停时清理第三方会话与异步生成器）。

### app.py — BilidownApp（控制器）

`navigate(key)`（页面切换）、`apply_preferences(draft)`、`restore_default_preferences()`、
`start_qr_login()`、`handle_logout()`、`refresh_user_info()`、`run()`、`shutdown()`。

---

## 3. 工作流

### 分层与线程模型

```
UI 线程（tkinter 主循环）
   │  只做布局与事件绑定，不发请求、不做 IO
   ▼  调用
BilidownApp（app.py，控制器）
   │  编排流程、状态机守卫、把回调 marshal 回 UI 线程
   ▼  调用
core 层 ──┬── BiliClient ──► BackgroundLoop（唯一后台事件循环）──► bilibili_api / aiohttp
          ├── TaskManager ──► 线程池（并发上限信号量）──► MediaDownloader ──► requests / ffmpeg
          ├── CredentialStore（DPAPI 加密落盘）
          ├── PreferenceStore（config.json 原子写入）
          └── network 策略（代理/直连，统一所有出网请求）
```

- 全进程只有一个事件循环（后台线程常驻）：网络与解析在后台完成，UI 线程永不阻塞，
  会话复用不泄漏，任务可通过 `Future.cancel()` 取消
- UI 层的所有界面更新都由控制器编组回 UI 线程执行

### 启动流程

1. `run.py` / `python -m bilidown` → `app.main()`
2. 初始化日志、加载偏好设置（config.json，损坏回退默认）、应用网络策略
3. `CredentialStore.load()` 恢复本地凭据 → `validate_credential()` 联网校验
   - 有效：直接进主界面；失效：清除凭据并进入待登录态
4. 构建 MainWindow，进入 tkinter 主循环

### 扫码登录流程

```
start_qr_login() → LoginSession.generate()（拿二维码PNG，显示在登录页）
  → 后台按间隔 poll()：QrState 状态机
    WAITING（等待扫码）→ SCANNED（已扫待确认）→ CONFIRMED（成功）
    EXPIRED（过期）→ 自动重新生成二维码
  → CONFIRMED 后 exchange_ticket() 换 Cookie → build_credential()
  → CredentialStore.save()（DPAPI 加密原子落盘）→ 进入主界面
```

每次轮询带序号与视图守卫，旧二维码的迟到回调不会污染新会话。

### 搜索与下载流程

```
搜索页输入关键词/BV号/链接 → parse_bv() 识别 → 搜索或直查（带分页）
  → 点击下载 → download_dialog 选择分P、格式（MP4/M4A/MP3/JPG）、清晰度
    （清晰度按登录态/大会员权限拦截）
  → 构造 DownloadTask → TaskManager.submit()
      查重：同输出文件的任务直接复用
      排队：并发信号量控制（默认2，可在设置调1-5）
  → MediaDownloader.download()
      prepare：BiliClient.select_streams() 拿流地址与备用地址
      分块下载：临时文件按「BV+分P+格式+画质」命名，支持断点续传
      失败重试：指数退避；4xx 直接换备用地址
      视频：ffmpeg 合并音视频（流拷贝优先，可中途取消杀进程）
      完成：移到以视频标题命名的目录，同名自动加 (2)(3)
  → 状态机推进（PENDING→DOWNLOADING→MERGING→DONE/FAILED/CANCELLED）
  → 结算回调通知 UI 更新任务列表，可选自动打开文件夹
```

下载前检查剩余磁盘空间；临时文件与产出文件同卷，保证 rename 原子性。

### 数据存放

| 内容 | 位置 |
| --- | --- |
| 登录凭据 | `%LOCALAPPDATA%\bilidown\credential.dat`（DPAPI 加密） |
| 配置 | `%LOCALAPPDATA%\bilidown\config.json` |
| 日志 | `%LOCALAPPDATA%\bilidown\bilidown.log`（2MB 轮转 ×3） |
| 下载文件 | `~/Downloads/bilidown/`（可在设置页修改） |

---

## 运行与测试

```bash
pip install -r requirements.txt
python run.py          # 启动
pytest                 # 测试（无 pytest 时：python tests/run_tests.py）
pyinstaller run.spec --noconfirm   # 打包为 dist/Amedown/Amedown.exe
```

## 许可

仅供个人学习与技术研究使用。请遵守哔哩哔哩用户协议，勿用于传播受版权保护的内容。
