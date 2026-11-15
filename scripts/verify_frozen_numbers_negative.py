#!/usr/bin/env python3
"""负例验证：把已知数字改坏后，冻结数字守卫必须红（而不是静默通过）。

**为什么不放在 `artifacts/tmp/` 里用固定目录**：早先那版把副本写到
`artifacts/tmp/frozen_negative/` 就再也没清过，结果留下 24 个旧 md 文档
（指纹还是已作废的 `09ae5510d34e7b2b`）。那些文件全库 0 引用，但会污染全文检索——
搜"赛道3"时旧文档排在前面，容易被当成现状读。所以这里改成 `tempfile.mkdtemp()`
并在 `finally` 里整棵删掉：**跑完不留痕**。

做法：把仓库里受守卫约束的文件复制到临时目录，在副本里注入错数字 + 错指纹，
把守卫模块的 `ROOT` 指过去再跑断言。不修改工作区任何文件。

用法（成功时每项都应打印 `NEGATIVE OK`）：
    PYTHONPATH=src python scripts/verify_frozen_numbers_negative.py
"""

from __future__ import annotations

import importlib.util
import shutil
import tempfile
from pathlib import Path

REAL_ROOT = Path(__file__).resolve().parents[1]


def load_guard():
    spec = importlib.util.spec_from_file_location(
        "tfn", REAL_ROOT / "tests" / "test_frozen_numbers.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    module = load_guard()
    tmp = Path(tempfile.mkdtemp(prefix="frozen_negative_"))
    failures = 0
    try:
        for rel in module.SCOPE:
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REAL_ROOT / rel, target)

        # 注入三类错数字 + 错指纹。
        # **故意用运行时拼接而不是字面量**：本文件放在 scripts/ 下会被
        # `test_frozen_numbers` 一起扫，直接写成"数字 + 个工具"的字面量会被
        # 当成真的对文档声明，守卫立刻红（试过，四个用例一起报错，连注释里的也算）。
        bad_tools = 9 * 111
        bad_selfcheck = 8 * 111
        bad_rules = 7 * 111
        fake_fp = ("dead" + "beef") * 2
        readme = tmp / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8")
            + f"\n回归负例：{bad_tools} 个工具、{bad_selfcheck}/{bad_selfcheck} 项一致、"
            f"指纹 `{fake_fp}`、{bad_rules} 条规则\n",
            encoding="utf-8",
        )

        module.ROOT = tmp
        for name, families in [
            ("tools", ("tools",)),
            ("selfcheck", ("selfcheck",)),
            ("fingerprint", ("fingerprint",)),
            ("rules", ("rules",)),
        ]:
            try:
                module._assert_families(*families)
                print(f"NEGATIVE FAIL: {name} 没有报错（守卫失效）")
                failures += 1
            except AssertionError as exc:
                lines = str(exc).splitlines()
                first = lines[1] if len(lines) > 1 else str(exc)
                print(f"NEGATIVE OK: {name} -> {first[:100]}")
    finally:
        # 无论成败都清干净：这个脚本的存在意义是验证守卫，不是产出数据
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"\n{failures} 项守卫失效")
        return 1
    print("\n全部通过：注入错数字后四类守卫均报错，说明守卫不是摆设。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
