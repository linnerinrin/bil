# Amedown

> 内部 Python 包名仍是 `bilidown`（`python -m bilidown`），用户数据目录也仍是
> `%LOCALAPPDATA%\bilidown` —— 改名会让资源目录与包名冲突，并丢失已保存的配置与登录状态。
> 界面上看到的名字统一为 **Amedown**。

Bilibili 视频/音频下载器（Tkinter 桌面客户端）。

本项目经过一次完整的分层重构：修复了原版本「打包后启动即崩溃」「BV 号搜索必然失败」
「下载进度条死循环」等 19 处缺陷，并把 UI、网络、下载、任务编排拆分成可测试的层次。

---

## 功能

- 扫码登录（登录凭据加密保存，下次启动自动恢复）
- 关键词搜索 / BV 号直查 / 直接粘贴视频链接，支持分页
- 下载视频（自动合并音视频为 MP4，优先 AVC 编码以保证兼容性）
- 仅下载音频（保存为 `.m4a`）
- 批量下载当前页结果
- 下载任务列表：进度、状态、取消、重试、移除、清理已结束、打开文件/目录
- 个人信息展示、复制视频信息到剪贴板
- 断点续传、失败自动重试（指数退避）、并发上限控制

## 运行

### 方式一：直接用源码运行（最省事）

```bash
# 1. 创建并激活虚拟环境（Python 3.10+）
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动
python run.py
```

### 方式二：安装为包后以模块方式运行

本项目采用 `src/` 布局，因此**必须先安装**（可编辑安装即可，秒级完成、无需联网）：

```bash
pip install -e . --no-deps --no-build-isolation   # 离线也可成功
python -m bilidown
```

安装后 `bilidown.exe`（Windows）控制台脚本也会生成，可直接调用：

```bash
bilidown
```

> 注意：在 `src/` 布局下，未安装时执行 `python -m bilidown` 会报
> `No module named bilidown` —— 这是 Python 的正常行为（`src/` 不在
> `sys.path` 上）。请二选一：用 `python run.py`，或先 `pip install -e .`。

### 方式三：使用打包好的可执行文件

```bash
pip install pyinstaller
pyinstaller run.spec --noconfirm
# 产物：dist/Amedown/Amedown.exe
```

> `run.spec` 已显式把 `src/bilidown/resources`（背景图）打入包内。
> 这是原版本最致命的缺陷：资源未打包导致 exe 启动即静默退出。

## 测试

```bash
# 有 pytest 时
pytest

# 没有 pytest 时（零依赖运行器，纯标准库）
python tests/run_tests.py
```

测试覆盖：纯函数（BV 解析、文件名净化）、凭据加密往返、事件循环桥接、
下载器（临时文件隔离 / 断点续传 / 重试 / 取消）、任务管理器全链路、
控制器状态机（扫码四态 / 登出 / 线程编组守卫）、界面构建与销毁守卫，
以及代码质量守卫（未使用 import、标准库同名遮蔽、`asyncio.run` 滥用、
分层越界）。当前共 **137 项，全部通过**。

> `tests/test_lint.py` 是 ruff 的离线兜底：本机无法联网安装 ruff 时，
> 它仍然会自动拦住重构前真实存在的未使用 import 与模块名遮蔽问题。

## 打包产物自检

打包完成后可以无需人工点窗口地验证产物是否可用：

```bash
set BILIDOWN_SELFTEST=1
dist\Amedown\Amedown.exe
echo %ERRORLEVEL%          # 期望 0
```

同时在 `%LOCALAPPDATA%\bilidown\bilidown.log` 会看到
`frozen=True` 与「界面已成功构建」。这正是原版本最致命缺陷（A1）
的自动化回归手段。

## 常见问题排查

| 现象 | 原因与处理 |
| --- | --- |
| exe 双击没反应（旧版「神秘的 exe bug」） | 资源未打包或路径相对 CWD。现已修复：资源走 `sys._MEIPASS`，用户数据走 `%LOCALAPPDATA%`。 |
| 界面文字/日志乱码 | 日志文件为 UTF-8；控制台查看请用 `Get-Content -Encoding UTF8`。 |
| 搜索报「获取视频信息失败」 | 视频可能已失效、需要登录或受地区限制；日志里有完整异常。 |
| 合并失败 | 需要 ffmpeg。`imageio-ffmpeg` 会随包提供；若被裁剪，请确认 `collect_all("imageio_ffmpeg")` 生效。 |
| 下载速度慢 | 并发上限可在「设置」页调整，或改 `max_concurrent` 字段（1–5，默认 2）。 |
| 请求报 ProxyError / 连接被拒 | 系统代理失效（例如 Clash 关了但系统代理还在）。本项目默认直连；详见「网络与代理」一节。 |

## 桌面快捷方式（点击即用）

打包完成后执行一次即可在桌面创建快捷方式：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1
```

- 目标为 `dist\Amedown\Amedown.exe`，工作目录设为其所在目录，图标取自 exe 自身
- 加 `-Remove` 可删除快捷方式；`-Name`/`-ExePath` 可自定义
- 注意：快捷方式指向 `dist\` 下的文件，若把 `dist` 删掉或移动，快捷方式会失效
  （重新打包后再跑一次上面的脚本即可）



## 项目结构

```
downloader/
├─ run.py                    # 入口（等价于 python -m bilidown）
├─ run.spec                  # PyInstaller 打包配置
├─ pyproject.toml            # 依赖与工具配置
├─ requirements.txt
├─ src/bilidown/
│  ├─ __main__.py            # python -m bilidown
│  ├─ app.py                 # 控制器：装配各层、编排异步流程
│  ├─ config/
│  │  ├─ paths.py            # 只读资源 / 可写用户数据的路径解析
│  │  └─ settings.py         # 样式与行为常量
│  ├─ core/
│  │  ├─ bridge.py           # 常驻后台事件循环（结构性支点）
│  │  ├─ client.py           # B 站 API 门面 + 纯函数
│  │  ├─ credential.py       # DPAPI 加密的凭据持久化
│  │  ├─ downloader.py       # 分块下载 / 断点续传 / 合并
│  │  ├─ models.py           # 领域模型与状态机
│  │  └─ tasks.py            # 任务队列与生命周期
│  ├─ ui/                    # 仅布局与事件绑定
│  │  ├─ base.py  window.py  widgets.py
│  │  └─ login_view.py  search_view.py  download_view.py  profile_view.py
│  ├─ utils/logger.py
│  └─ resources/background.jpg
└─ tests/
```

## 设置

导航栏的「设置」按钮（位于「我的」与「下载列表」之间）提供以下选项，
改完点「应用」统一生效并写入配置文件，下次启动自动恢复：

| 设置项 | 说明 |
| --- | --- |
| **设置背景** | 可选用内置背景（按文件名列表选择，带缩略图预览），或自选 jpg/png/bmp/webp 图片。图片等比放大铺满并居中裁掉多余边缘，不拉伸、无黑边 |
| **设置下载目录** | 选择或手动输入下载目录。**立即对新建任务生效**；已在下载/排队的任务保留原目录，避免同一任务文件分散 |
| **最大并发下载数** | 1–5，默认 2。调大可提速，但更占带宽 |
| **下载失败重试次数** | 1–5，默认 3。网络不稳时可调高 |
| **下载完成后自动打开文件夹** | 默认关闭；开启后完成时在文件管理器中定位产出文件 |

「恢复默认」会把上述各项恢复为默认值（**不会**改动你在配置文件里设置的代理）。

### 增加内置背景

把图片丢进 `src/bilidown/resources/` 即可（支持 jpg/jpeg/png/bmp/webp），
设置页的「内置背景」列表会**自动列出文件名**，无需改代码；
打包时该目录会整体打入包内。配置里以 `builtin:<文件名>` 表示选中的内置背景，
留空则表示使用默认内置背景。

### 配置文件

所有偏好都存在同一个文件里，也可以直接编辑它（改完重启生效）：

`%LOCALAPPDATA%\bilidown\config.json`

```json
{
  "background_path": "",
  "download_dir": "",
  "max_concurrent": 2,
  "download_retries": 3,
  "open_folder_when_done": false,
  "use_system_proxy": false,
  "proxy": ""
}
```

- `background_path` 为空表示使用默认内置背景；`"builtin:文件名"` 表示指定的内置背景；
  其它值视为自定义图片的绝对路径（文件不存在时自动回退内置背景）
- `download_dir` 为空表示使用默认目录 `~/Downloads/bilidown`
- 文件缺失会自动生成，损坏则回退默认值；不认识的字段会被原样保留

## 数据存放位置

| 内容 | 位置 |
| --- | --- |
| 登录凭据 | `%LOCALAPPDATA%\bilidown\credential.dat`（Windows DPAPI 加密） |
| 运行日志 | `%LOCALAPPDATA%\bilidown\bilidown.log`（2 MB 轮转 ×3） |
| 网络配置 | `%LOCALAPPDATA%\bilidown\config.json` |
| 下载文件 | `~/Downloads/bilidown/` |

凭据使用 Windows DPAPI 加密，密文只能由当前 Windows 用户解开；
在非 Windows 平台退化为 base64 混淆（会在文件中标注 scheme）。

## 网络与代理

**默认直连，不读取系统代理。** 这是刻意的。

`requests` 与 `aiohttp` 默认会**自动读取系统代理**。若系统里配置了一个没有运行的
代理（例如 Clash / V2Ray 已关闭但系统代理设置还留着），所有请求都会被这个死代理
拦住，报错形如：

```
ProxyError('Unable to connect to proxy',
  HTTPSConnection(host='127.0.0.1', port=7890): WinError 10061 拒绝连接)
```

表现就是「浏览器能上网，这个程序却连不上」。为消除这种困惑，本项目统一让所有出网
请求（下载、扫码换票、头像、以及 bilibili_api 的搜索与取流）走自己的会话策略。

如果你**确实需要走代理**（例如访问受限地区内容），编辑
`%LOCALAPPDATA%\bilidown\config.json`：

```json
{
  "use_system_proxy": false,
  "proxy": "http://127.0.0.1:7890"
}
```

- `proxy` 指定具体代理地址（优先级最高）
- 或把 `use_system_proxy` 设为 `true`，使用系统代理设置
- 两者都未设置或为 `false` 时直连

配置在启动时读取，启动日志会打印实际生效的策略，例如
`网络策略: 代理：不使用（直连）`。

> 注意：本项目的登录状态由 `Credential`（显式传给每个请求）维护，
> **不依赖** aiohttp 的 `cookies.txt`，所以直连模式不会影响登录。

## 架构要点

**常驻后台事件循环**（`core/bridge.py`）是本项目的结构性支点。
`bilibili_api` 的会话池按「事件循环」分桶（`session_pool[client][loop]`），
原版本每次操作都调用一次 `asyncio.run()`，导致每次都新建一个事件循环和一个
aiohttp 会话却永不关闭（连接与文件句柄泄漏），并且在 Tk 主线程里等待网络
而使界面完全冻结。现在全进程只有一个事件循环：

- 网络与解析在后台线程完成，UI 线程只负责渲染；
- 会话被复用，不再泄漏；
- 任务可被取消（`Future.cancel()`）。

**分层原则**：`ui/` 不出现 `asyncio`、不发 HTTP 请求、不做文件 IO；
所有业务通过控制器 `app.py` 与 `core/` 交互。禁用 Python 内置模块同名文件
（旧项目有 `list.py` / `search.py`，会遮蔽标准库与第三方模块）。

## 许可

仅供个人学习与技术研究使用。请遵守哔哩哔哩用户协议，勿用于传播受版权保护的内容。
