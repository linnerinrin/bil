"""零依赖测试运行器（pytest 不可用时的兜底）。

用途：本机 pip 无法联网安装 pytest 时，仍然可以运行全部测试。

    python tests/run_tests.py            # 运行所有测试
    python tests/run_tests.py test_pure  # 只运行名字包含 test_pure 的测试模块

说明：
  - 测试用例按 pytest 风格编写（test_* 函数 / Test* 类中的 test_* 方法），
    因此两种运行方式都支持；
  - 本运行器不依赖任何第三方库，也不需要安装项目本身。
"""

from __future__ import annotations

import importlib
import pathlib
import sys
import time
import traceback

TESTS_DIR = pathlib.Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SRC = ROOT / "src"

# 把项目根目录加入 sys.path，使 `tests` 成为一个可导入的包（相对导入才能工作）
for path in (str(ROOT), str(SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _discover(pattern: str) -> list[str]:
    names = []
    for file in sorted(TESTS_DIR.glob("test_*.py")):
        if pattern and pattern not in file.stem:
            continue
        names.append(file.stem)
    return names


def _run_case(func, label: str) -> tuple[int, int, list[str]]:
    """执行单个用例，返回 (通过数, 失败数, 失败信息)。"""
    try:
        func()
    except Exception:  # noqa: BLE001
        detail = traceback.format_exc()
        print(f"  FAIL {label}")
        print("      " + detail.strip().replace("\n", "\n      "))
        return 0, 1, [detail]
    except BaseException:  # noqa: BLE001 - KeyboardInterrupt 等
        raise
    print(f"  ok   {label}")
    return 1, 0, []


def main(argv: list[str]) -> int:
    pattern = argv[1] if len(argv) > 1 else ""
    modules = _discover(pattern)
    if not modules:
        print("未找到匹配的测试模块")
        return 1

    total_pass = total_fail = 0
    failures: list[str] = []
    started = time.monotonic()

    for name in modules:
        print(f"\n=== {name} ===")
        try:
            # 以包内模块方式导入，使测试模块之间的相对导入（from .test_x import ...）可用
            module = importlib.import_module(f"tests.{name}")
        except Exception:  # noqa: BLE001
            print(f"  FAIL 导入模块 {name}")
            traceback.print_exc()
            total_fail += 1
            failures.append(f"{name}: import error")
            continue

        # 模块级 test_* 函数
        for attr in sorted(dir(module)):
            if not attr.startswith("test_"):
                continue
            obj = getattr(module, attr)
            if callable(obj):
                p, f, info = _run_case(obj, f"{name}.{attr}")
                total_pass += p
                total_fail += f
                failures.extend(info)

        # Test* 类
        for attr in sorted(dir(module)):
            if not attr.startswith("Test"):
                continue
            cls = getattr(module, attr)
            if not isinstance(cls, type):
                continue
            instance = cls()
            for method_name in sorted(dir(cls)):
                if not method_name.startswith("test_"):
                    continue
                method = getattr(instance, method_name)
                if callable(method):
                    p, f, info = _run_case(method, f"{name}.{attr}.{method_name}")
                    total_pass += p
                    total_fail += f
                    failures.extend(info)

    elapsed = time.monotonic() - started
    print("\n" + "=" * 58)
    print(f"通过 {total_pass} · 失败 {total_fail} · 耗时 {elapsed:.1f}s")
    if failures:
        print("失败详情：")
        for item in failures:
            print("  - " + item.strip().splitlines()[-1])
    print("=" * 58)
    return 1 if total_fail else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
