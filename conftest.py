"""把进程级临时目录钉在仓库内，避免测试往仓库根写东西。

起因：本机系统临时目录对 Python 进程不可写时（受限账户/沙箱），
`tempfile.gettempdir()` 会**静默回退到当前工作目录**，于是任何 `mkstemp()` 都会在仓库根
留下一个 `tmpXXXXXXXX` 空文件。实测跑一次全量测试就能冒出两个，`git status` 里多出未跟踪项，
容易和真实改动混在一起。

这里在收集阶段把落点固定到 `artifacts/tmp`（已在 .gitignore 内）。用 `tmp_path` fixture 的
用例不受影响——那是 pytest 自己管理的目录。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP_ROOT = Path(__file__).resolve().parent / "artifacts" / "tmp"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

# 两件事都要做：
# 1) 进程内 `tempfile.tempdir` —— 覆盖本次进程里 `mkstemp`/`mkdtemp` 的落点；
# 2) 环境变量 —— 测试里会 `python -m decision_poc.xxx` 起子进程，子进程不加载 conftest，
#    只能靠继承的 TMP/TEMP 找到可写目录。只做 1) 时实测仍会冒 `tmpXXXX` 到仓库根。
for _key in ("TMP", "TEMP", "TMPDIR"):
    os.environ[_key] = str(_TMP_ROOT)
tempfile.tempdir = str(_TMP_ROOT)
