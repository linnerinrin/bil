"""程序入口（兼容旧用法）。

    python run.py          # 等价于 python -m bilidown

真正的实现位于 src/bilidown/ 包内，本文件只负责把 src 加入 sys.path 后转发。
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bilidown.app import main  # noqa: E402

if __name__ == "__main__":
    main()
