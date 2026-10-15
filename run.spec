# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

审计缺陷 A1 的根因就在这里：旧 run.spec 的 ``datas`` 里**没有** ctj.jpg，
而旧 main.py 在模块级 Image.open("ctj.jpg")，于是打包后启动即 FileNotFoundError
（console=False 所以静默退出，看起来像「神秘的 exe bug」）。

本配置要点：
  1. add_data 显式打入 src/bilidown/resources（背景图）；
  2. collect_all 收集 bilibili_api / moviepy / imageio 的数据与隐式导入
     （moviepy + imageio_ffmpeg 需要带上 ffmpeg 可执行文件用于合并音视频）；
  3. 默认 onedir：启动更快、体积更小；如需单文件可加 --onefile。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# SPECPATH 是 spec 文件所在目录（即项目根），不要再取 .parent
PROJECT_ROOT = Path(SPECPATH).resolve()
RESOURCES = PROJECT_ROOT / "src" / "bilidown" / "resources"

# 产品名：同时作为可执行文件名（Amedown.exe）与 dist 子目录名
PRODUCT_NAME = "Amedown"
ICON_FILE = RESOURCES / f"{PRODUCT_NAME}.ico"

datas = []
binaries = []
hiddenimports = []

# 1) 包资源（背景图等）—— 修复 A1 的关键一行
if RESOURCES.is_dir():
    datas.append((str(RESOURCES), "bilidown/resources"))
else:  # pragma: no cover
    raise SystemExit(f"缺少资源目录，无法打包: {RESOURCES}")

# 2) 第三方库的数据/二进制/隐式导入
for package in ("bilibili_api", "moviepy", "imageio", "imageio_ffmpeg"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception as exc:  # noqa: BLE001 - 缺包时给出明确提示而不是神秘崩溃
        print(f"[run.spec] collect_all({package}) 失败: {exc}")
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# 3) 常见隐式导入（tkinter 主题、requests 依赖等）
hiddenimports += collect_submodules("pywinstyles")
hiddenimports += [
    "PIL._tkinter_finder",
    "tkinter",
    "tkinter.ttk",
    "tkinter.messagebox",
    "requests",
    "pyperclip",
]

block_cipher = None

a = Analysis(
    [str(PROJECT_ROOT / "run.py")],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 明显用不到的重型可选依赖，减小体积
        "matplotlib",
        "pytest",
        "IPython",
        "notebook",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=PRODUCT_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX 容易被杀软误报，默认关闭
    console=False,  # GUI 程序
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 应用图标：任务栏与桌面快捷方式都会显示它
    icon=str(ICON_FILE) if ICON_FILE.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=PRODUCT_NAME,
)
