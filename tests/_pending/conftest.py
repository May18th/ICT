"""暂缓收集的测试目录：默认不进 pytest 会话。

为什么需要这个文件
------------------
`pyproject.toml` 的 `testpaths = ["tests"]` 会递归整个 `tests/`，而 pytest 默认的
`norecursedirs` 只跳过 `.*`、`build`、`venv` 这类目录，**不认识 `_pending`**。
所以只把文件挪进 `tests/_pending/` 而不加这条，测试仍会被收集，
`test_baseline.collected` 的数字就对不上——守卫会当场报错。

`collect_ignore_glob` 是本目录级的 pytest 原生开关，只影响 `tests/_pending/`，
不动根 `conftest.py`，也不影响其它目录的收集。
"""

from __future__ import annotations

collect_ignore_glob = ["test_*.py"]
