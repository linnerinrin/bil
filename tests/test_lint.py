"""代码质量守卫（纯标准库实现，无需 ruff 也能运行）。

用途：本机无法联网安装 ruff 时，仍然能自动拦住重构前真实存在的两类问题：
  1. 未使用的 import（旧 login.py / merge.py 就有）；
  2. 模块名遮蔽标准库（旧项目有 list.py / search.py / profile.py）。
以及语法解析失败。

若安装了 ruff，请以 ``ruff check`` 为准；本文件是它的离线兜底。
"""

from __future__ import annotations

import ast
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
PKG = SRC / "bilidown"

# 与标准库/常用库同名的模块名，绝不允许出现在自己的包里（会遮蔽导入）
SHADOWED_NAMES = frozenset(
    {
        "list",
        "search",
        "login",
        "profile",
        "config",
        "types",
        "json",
        "copy",
        "random",
        "queue",
        "select",
        "socket",
        "stat",
        "time",
        "code",
        "test",
        "string",
        "token",
    }
)


def _iter_py(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _module_names(root: pathlib.Path) -> list[str]:
    return [p.stem for p in _iter_py(root)]


def _imported_names(tree: ast.AST) -> dict[str, int]:
    imported: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported[alias.asname or alias.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    imported[alias.asname or alias.name] = node.lineno
    return imported


def _used_names(tree: ast.AST) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            current: ast.AST = node
            while isinstance(current, ast.Attribute):
                current = current.value
            if isinstance(current, ast.Name):
                used.add(current.id)
    return used


class TestSyntax:
    def test_all_sources_parse(self) -> None:
        failures: list[str] = []
        for path in _iter_py(SRC) + _iter_py(PROJECT_ROOT / "tests") + [PROJECT_ROOT / "run.py"]:
            try:
                ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:  # pragma: no cover
                failures.append(f"{path.name}: {exc}")
        assert not failures, f"语法错误: {failures}"

    def test_run_spec_parses(self) -> None:
        spec = PROJECT_ROOT / "run.spec"
        ast.parse(spec.read_text(encoding="utf-8"))


class TestNoShadowing:
    def test_no_module_shadows_stdlib(self) -> None:
        """旧项目有 list.py / search.py / profile.py，会遮蔽标准库与第三方模块。"""
        offenders = sorted(
            name for name in _module_names(PKG) if name in SHADOWED_NAMES
        )
        assert not offenders, f"包内出现会遮蔽标准库的模块名: {offenders}"

    def test_no_module_at_project_root_shadows_stdlib(self) -> None:
        root_modules = [p.stem for p in sorted(PROJECT_ROOT.glob("*.py"))]
        offenders = [name for name in root_modules if name in SHADOWED_NAMES]
        assert not offenders, f"项目根目录出现会遮蔽标准库的模块: {offenders}"

    def test_no_legacy_modules_left(self) -> None:
        """重构后旧的平铺模块必须全部移除。"""
        legacy = [
            "core",
            "main",
            "login",
            "search",
            "list",
            "profile",
            "merge",
            "config",
        ]
        left = [name for name in legacy if (PROJECT_ROOT / f"{name}.py").exists()]
        assert not left, f"旧模块仍存在: {left}"


class TestUnusedImports:
    def test_no_unused_imports_in_package(self) -> None:
        """未使用的 import 视为错误（旧代码有 4 处）。"""
        offenders: list[str] = []
        for path in _iter_py(PKG):
            # 包的 __init__.py 负责再导出，其 import 会被 __all__ 引用
            if path.name == "__init__.py":
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            used = _used_names(tree)
            for name, lineno in _imported_names(tree).items():
                if name == "annotations" or name in used:
                    continue
                # 变量注解里的字符串引用（from __future__ import annotations）也算使用
                if name in source.replace(f"import {name}", ""):
                    continue
                offenders.append(f"{path.relative_to(SRC)}:{lineno} 未使用 {name}")
        assert not offenders, "存在未使用的 import:\n" + "\n".join(offenders)

    def test_all_exports_are_importable(self) -> None:
        """__init__.py 的 __all__ 必须真的能取到属性（防止再导出写错名字）。"""
        for pkg_name, pkg_path in (
            ("bilidown.config", PKG / "config" / "__init__.py"),
            ("bilidown.core", PKG / "core" / "__init__.py"),
            ("bilidown.ui", PKG / "ui" / "__init__.py"),
            ("bilidown.utils", PKG / "utils" / "__init__.py"),
        ):
            tree = ast.parse(pkg_path.read_text(encoding="utf-8"))
            exported: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "__all__":
                            exported = [c.value for c in node.value.elts]  # type: ignore[attr-defined]
            module = __import__(pkg_name, fromlist=["*"])
            missing = [name for name in exported if not hasattr(module, name)]
            assert not missing, f"{pkg_name}.__all__ 中这些名字取不到: {missing}"


class TestLintConfig:
    def test_pyproject_declares_lint_and_tests(self) -> None:
        import tomllib

        data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert "ruff" in data.get("tool", {}), "pyproject.toml 缺少 [tool.ruff] 配置"
        assert "pytest" in data.get("tool", {}), "pyproject.toml 缺少 [tool.pytest.ini_options]"
        assert data["tool"]["ruff"]["lint"]["select"], "ruff 未启用任何规则"

    def test_requirements_has_dev_extras(self) -> None:
        text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
        for needle in ("pytest", "ruff"):
            assert needle in text, f"requirements.txt 未声明 {needle}"

    def test_pythonpath_hint_documented(self) -> None:
        """README 必须说明如何让 python -m bilidown 可用（src 布局）。"""
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        assert "pip install -e ." in readme
        assert "python -m bilidown" in readme


class TestProjectLayout:
    def test_src_layout_package_present(self) -> None:
        for expected in (
            "__init__.py",
            "__main__.py",
            "app.py",
            "config/paths.py",
            "config/settings.py",
            "core/bridge.py",
            "core/client.py",
            "core/credential.py",
            "core/downloader.py",
            "core/models.py",
            "core/tasks.py",
            "ui/window.py",
            "ui/login_view.py",
            "ui/search_view.py",
            "ui/download_view.py",
            "ui/profile_view.py",
            "utils/logger.py",
        ):
            assert (PKG / expected).is_file(), f"缺少文件 src/bilidown/{expected}"

    def test_background_resource_bundled(self) -> None:
        """A1 回归：背景图必须存在于包资源目录，否则打包后启动即崩。"""
        resource = PKG / "resources" / "background.jpg"
        assert resource.is_file(), "缺少资源 src/bilidown/resources/background.jpg"
        assert resource.stat().st_size > 10_000

    def test_spec_bundles_resources(self) -> None:
        """run.spec 必须把 resources 目录加入 datas（原缺陷 A1 的根因）。"""
        spec = (PROJECT_ROOT / "run.spec").read_text(encoding="utf-8")
        assert "resources" in spec
        assert "datas.append" in spec
        assert "bilidown/resources" in spec

    def test_spec_is_tracked_by_git(self) -> None:
        """构建脚本必须入库，否则「如何构建」这一知识再次丢失。"""
        import subprocess

        result = subprocess.run(
            ["git", "ls-files", "run.spec"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:  # 非 git 环境则跳过
            return
        assert "run.spec" in result.stdout, "run.spec 未被 git 跟踪"

    def test_gitignore_covers_runtime_artifacts(self) -> None:
        text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("login_qrcode.png", "avatar_temp.png", "downloads/", "dist/", "build/"):
            assert pattern in text, f".gitignore 未覆盖 {pattern}"


class TestNoAsyncRunAbuse:
    def test_no_asyncio_run_in_app_layer(self) -> None:
        """B4 回归：应用层不得再滥用 asyncio.run 创建/销毁事件循环。

        注意用 AST 精确识别「调用 asyncio.run(...)」，
        因为正确用法 ``asyncio.run_coroutine_threadsafe(...)`` 含有相同的子串前缀。
        """
        offenders: list[str] = []
        for path in _iter_py(PKG):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "run"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "asyncio"
                ):
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
        assert not offenders, f"仍在调用 asyncio.run: {offenders}"

    def test_only_bridge_owns_the_event_loop(self) -> None:
        """事件循环的封装必须集中在 core/bridge.py。"""
        offenders: list[str] = []
        for path in _iter_py(PKG):
            if path.name == "bridge.py":
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in ("new_event_loop", "run_forever", "set_event_loop"):
                        offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
        assert not offenders, f"事件循环应只由 bridge.py 管理，越界处: {offenders}"

    def test_no_blocking_thread_per_task(self) -> None:
        """C10 回归：不得再出现「每任务一个裸 Thread」的写法。"""
        tasks = (PKG / "core" / "tasks.py").read_text(encoding="utf-8")
        assert "ThreadPoolExecutor" in tasks
        assert "BoundedSemaphore" in tasks
        assert "threading.Thread(" not in tasks

    def test_ui_layer_has_no_network_or_asyncio(self) -> None:
        """分层约束：ui/ 不应直接发 HTTP 请求或使用 asyncio。"""
        offenders: list[str] = []
        for path in _iter_py(PKG / "ui"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            names = _used_names(tree)
            if "requests" in names:
                offenders.append(f"{path.name} 直接 import requests")
            if "asyncio" in names:
                offenders.append(f"{path.name} 直接使用 asyncio")
        assert not offenders, f"UI 层越界: {offenders}"
