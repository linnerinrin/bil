"""配置与路径测试（修复 A1/A2 的路径语义）。"""

from __future__ import annotations

import pathlib
import sys

from bilidown.config import paths
from bilidown.config.settings import STYLE, WINDOW_HEIGHT, WINDOW_TITLE, WINDOW_WIDTH

# 项目根（tests/ 的上一级）
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


class FakeResponse:
    """极简 requests.Response 替身。"""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status_code = status
        self.headers = {"Content-Length": str(len(body))}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 1024, **_kwargs: object):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class TestResourcePath:
    def test_resource_root_is_inside_package(self) -> None:
        root = paths.resource_root()
        assert root.name == "resources"
        assert root.parent.name == "bilidown"

    def test_background_image_exists(self) -> None:
        """A1 回归：背景图必须作为包资源存在，否则打包后启动即崩。"""
        assert paths.resource_path("background.jpg").is_file()

    def test_resource_path_joins(self) -> None:
        assert paths.resource_path("a", "b").name == "b"


class TestUserDataDir:
    def test_user_data_dir_is_absolute(self) -> None:
        assert paths.user_data_dir().is_absolute()

    def test_user_data_dir_under_localappdata_on_windows(self) -> None:
        if sys.platform != "win32":
            return
        base = paths.user_data_dir()
        assert base.name == paths.APP_NAME

    def test_ensure_user_data_dir_creates(self) -> None:
        created = paths.ensure_user_data_dir()
        assert created.is_dir()

    def test_download_dir_is_absolute_and_created(self) -> None:
        target = paths.ensure_download_dir()
        assert target.is_absolute()
        assert target.is_dir()

    def test_not_frozen_in_tests(self) -> None:
        assert paths.is_frozen() is False

    def test_frozen_uses_meipass(self) -> None:
        """模拟 PyInstaller 环境：资源必须从 _MEIPASS 解析。"""
        fake_root = pathlib.Path(paths.__file__).resolve().parent.parent
        old = getattr(sys, "_MEIPASS", None)
        old_frozen = getattr(sys, "frozen", None)
        try:
            sys._MEIPASS = str(fake_root)  # type: ignore[attr-defined]
            sys.frozen = True  # type: ignore[attr-defined]
            assert paths.is_frozen() is True
            resolved = paths.resource_root()
            # 必须指向 _MEIPASS 下的 bilidown/resources（而不是源码树路径）
            assert fake_root in resolved.parents
            assert resolved.parent.name == "bilidown"
            assert resolved.name == "resources"
        finally:
            if old is None:
                del sys._MEIPASS  # type: ignore[attr-defined]
            else:
                sys._MEIPASS = old  # type: ignore[attr-defined]
            if old_frozen is None:
                del sys.frozen  # type: ignore[attr-defined]
            else:
                sys.frozen = old_frozen  # type: ignore[attr-defined]


class TestStyleSettings:
    def test_required_keys(self) -> None:
        for key in ("frame_bg", "accent_color", "text_color", "title_font", "normal_font"):
            assert key in STYLE

    def test_window_constants(self) -> None:
        assert WINDOW_WIDTH > 0 and WINDOW_HEIGHT > 0
        assert WINDOW_TITLE

    def test_product_name_is_used_as_window_title(self) -> None:
        """项目标题应为 Amedown（用户要求改名）。"""
        from bilidown.config.settings import PRODUCT_NAME

        assert PRODUCT_NAME == "Amedown"
        assert WINDOW_TITLE == PRODUCT_NAME

    def test_internal_package_name_unchanged(self) -> None:
        """内部包名与用户数据目录**故意**保持 bilidown，避免丢配置/与资源目录冲突。"""
        assert paths.APP_NAME == "bilidown"

    def test_window_icon_assets_exist(self) -> None:
        """图标资源必须存在，否则窗口与快捷方式只能用系统默认图标。"""
        assert paths.resource_path("Amedown.ico").is_file()
        assert paths.resource_path("Amedown.png").is_file()

    def test_spec_builds_named_exe(self) -> None:
        """run.spec 必须把产物命名为 Amedown 并挂上图标。"""
        spec = (PROJECT_ROOT / "run.spec").read_text(encoding="utf-8")
        assert 'PRODUCT_NAME = "Amedown"' in spec
        assert "name=PRODUCT_NAME" in spec
        assert "icon=str(ICON_FILE)" in spec

    def test_shortcut_script_exists_and_targets_amedown(self) -> None:
        """仓库内应提供创建桌面快捷方式的脚本，且默认指向 Amedown.exe。"""
        script = PROJECT_ROOT / "scripts" / "create_shortcut.ps1"
        assert script.is_file(), "缺少 scripts/create_shortcut.ps1"
        text = script.read_text(encoding="utf-8")
        assert "Amedown.exe" in text
        assert "GetFolderPath(\"Desktop\")" in text or "GetFolderPath('Desktop')" in text
