"""pytest / 零依赖运行器 共用的测试配置。

把 src 加入 sys.path，使 ``import bilidown`` 在两种运行方式下都可用。
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
