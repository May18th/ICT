"""一条命令生成并核对全部对外材料。

设计原则与仓库一致：**对外宣称的数字都由命令产生，脚本只比对，不改数字**。
任一期望不成立即退出码 1，并把失败步骤的原始输出完整打出来。

用法：

    uv run --locked python scripts/reproduce.py              # 全量（含 914 项测试，约 3-6 分钟）
    uv run --locked python scripts/reproduce.py --fast       # 跳过 pytest，约 30 秒
    uv run --locked python scripts/reproduce.py --list       # 只列步骤
    uv run --locked python scripts/reproduce.py --keep-going # 出错也继续跑完，最后统一汇报

产物落点：`artifacts/reproduce/summary.json`（已被 .gitignore 忽略）+ 各命令自身的 samples/docs 输出。

已知限制：评测产物（`samples/eval/latest.json`、`docs/reports/p8-evaluation-report.md`）里带
逐例 `elapsed_ms` 与 p50/p95，是**本机实测**。断言不看这些字段（只看通过数、结构、快照），
但重跑一次它们就会变，所以全量跑完 `git status` 常有多份产物改动——那是耗时抖动，不是回归。
提交前先 `git diff` 过滤 `elapsed|p50|p95` 确认没有实质变化。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
EVENT = "WO-20261029-007"
AS_OF = "2026-10-29T09:40:00+08:00"
OUT_DIR = ROOT / "artifacts" / "reproduce"

# 与 conftest.py 同样的理由：系统临时目录不可写时 gettempdir 会回退到 cwd，
# 于是任何 mkstemp 都在仓库根留下空文件。父进程也钉到 artifacts/tmp。
_TMP_ROOT = ROOT / "artifacts" / "tmp"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(_TMP_ROOT)

# --- 权威源：对外数字从这些地方算，不在断言里写死 ---------------------------------
# 起因：`"assets":\s*18` 写死之后，资产从 18 加到 19，整个 reproduce 在第 5 步就红，
# 后面 13 步一步没跑——数字合理变化和真实回归长得一模一样。
sys.path.insert(0, str(SRC))

from decision_poc.evidence_check import EXPECTATIONS_PATH, expected_item_count  # noqa: E402
from decision_poc.mcp_server import UrbanToolkit  # noqa: E402
from decision_poc.urban_dataset import URBAN_ASSETS  # noqa: E402

EVAL_DIR = ROOT / "samples" / "eval"

#: MCP 工具名清单 —— 取自 toolkit 自身，工具增删时断言跟着变，不会出现"标题写 14、实际 13"。
TOOL_NAMES = tuple(item["name"] for item in UrbanToolkit().tools())

#: 数据集资产数 —— 取代码注册表长度。健康检查扫的是磁盘，两者相等才算真的一致；
#: 少产出一个文件、或多注册一个没落地的资产，都会红。
ASSET_TOTAL = len(URBAN_ASSETS)
#: P8 评测用例数 —— 用例文件条目数。
CASE_TOTAL = len(json.loads((EVAL_DIR / "urban_cases.json").read_text(encoding="utf-8"))["cases"])
#: 证据自检条目数 —— 与 `run_evidence_check().total` 同一套展开逻辑。
EVIDENCE_TOTAL = expected_item_count()
#: 单测收集数 —— 期望文件里登记的值；实际 pytest 收集数与之不等即红。
TEST_TOTAL = int(
    json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))["test_baseline"]["collected"]
)


@dataclass
class Step:
    name: str
    argv: list[str]
    expect: list[str] = field(default_factory=list)  # 正则；全部命中才算通过
    writes: list[str] = field(default_factory=list)  # 相对仓库根的产物路径
    slow: bool = False
    timeout: int = 900


def _uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "uv.exe", Path.home() / ".local" / "bin" / "uv"):
        if candidate.is_file():
            return str(candidate)
    return None


def build_steps(*, include_slow: bool) -> list[Step]:
    py = sys.executable
    uv = _uv()
    steps: list[Step] = []

    if uv:
        steps.append(Step("锁定文件一致", [uv, "lock", "--check"], writes=[]))
    steps.append(Step("依赖完整性", [uv, "pip", "check", "--python", py], slow=False) if uv else Step("依赖完整性(跳过: 未找到 uv)", [py, "-c", "print('SKIP')"]))

    steps.append(
        Step(
            "单元测试",
            # --basetemp 指向一个本次独有的空目录：pytest 创建后保留、退出不做批量清理。
            # 否则 pytest 会在退出时批量删除旧的 pytest-of-* 临时目录，在受限沙箱里触发
            # 批量删除拦截（fail-closed），把退出码改成 1 并吞掉末尾的 "N passed" 汇总行。
            [py, "-m", "pytest", "-q", "--basetemp", str(ROOT / "artifacts" / "tmp" / f"pytest-{uuid.uuid4().hex[:8]}")],
            expect=[rf"\b{TEST_TOTAL} passed"],
            slow=True,
            timeout=1800,
        )
    )
    steps.append(Step("字节码编译", [py, "-m", "compileall", "-q", "src", "tests"]))
    steps.append(
        Step(
            "生成城市内涝数据集",
            [py, "-m", "decision_poc.urban_dataset"],
            writes=["samples/assets/urban/manifest.json"],
        )
    )
    steps.append(
        Step(
            "数据集健康检查",
            [py, "-m", "decision_poc.health", "--dataset", "samples/assets/urban"],
            expect=[rf'"assets":\s*{ASSET_TOTAL}'],
        )
    )
    steps.append(
        Step(
            "数据门禁（来源分级 / 基准 / 跨字段）",
            [py, "-m", "decision_poc.data_gate", "--out", "docs/reports/data-gate-report.md"],
            expect=[r"PASS facts=\d+ errors=0"],
            writes=["docs/reports/data-gate-report.md"],
        )
    )
    steps.append(
        Step(
            "生成引擎无关场景布局",
            [py, "-m", "decision_poc.scene_layout"],
            writes=["samples/assets/urban/scene_layout.json"],
        )
    )
    steps.append(
        Step(
            "生成场景导出（Blender/UE 取数）",
            [py, "-m", "decision_poc.scene_export", "--out", "samples/assets/urban/scene.json"],
            writes=["samples/assets/urban/scene.json"],
        )
    )
    steps.append(
        Step(
            "生成推演回放脚本",
            [py, "-m", "decision_poc.replay_export"],
            expect=[r"个动作", r"快照 "],
            writes=["samples/replay/core_run.json"],
        )
    )
    steps.append(
        Step(
            "生成实体表（决策层 → 3D 绑定）",
            [py, "-m", "decision_poc.scene_binding"],
            expect=[r"实体 \d+", r"指纹 proj-"],
            writes=["samples/assets/urban/semantic_entities.json"],
        )
    )
    steps.append(
        Step(
            "六跳跨部门推演 + 落账",
            [py, "-m", "decision_poc.deduction", "--event", EVENT, "--as-of", AS_OF, "--record", "artifacts/ledger"],
            expect=[r"C1", r"C2", r"C3"],
        )
    )
    steps.append(
        Step(
            "反事实重放 CF1-CF3",
            [py, "-m", "decision_poc.counterfactual", "--case", "all"],
            expect=[r"CF1", r"CF2", r"CF3"],
        )
    )
    steps.append(
        Step(
            "五 Skill 编排与证据审计",
            [py, "-m", "decision_poc.skills", "--event", EVENT, "--as-of", AS_OF, "--caller-labels", "ORG-EMERG"],
            expect=[r"claim 15/15", r"引用完整率 1\.00"],
        )
    )
    steps.append(
        Step(
            f"P8 评测 {CASE_TOTAL} 用例",
            [
                py,
                "-m",
                "decision_poc.urban_eval",
                "--json",
                "samples/eval/latest.json",
                "--report",
                "docs/reports/p8-evaluation-report.md",
                "--baseline",
                "samples/eval/baseline_before_temporal_guard.json",
            ],
            expect=[rf"\b{CASE_TOTAL}/{CASE_TOTAL}\b"],
            writes=["samples/eval/latest.json", "docs/reports/p8-evaluation-report.md"],
        )
    )
    steps.append(
        Step(
            f"MCP {len(TOOL_NAMES)} 工具契约",
            [py, "-m", "decision_poc.mcp_server", "--list-tools"],
            # 逐个工具名都要出现：任何一个掉了都会红，比"数一下是 14 个"更严
            expect=[re.escape(name) for name in TOOL_NAMES],
        )
    )
    steps.append(
        Step(
            "P9 决策策略受控进化",
            [py, "-m", "decision_poc.policy_evolution", "--report", "docs/reports/p9-policy-evolution-report.md"],
            expect=[r"flood-response-v1\.\d", r"训练偏差"],
            writes=[
                "docs/reports/p9-policy-evolution-report.md",
                "samples/eval/decision_memory.json",
            ],
            slow=True,
        )
    )
    steps.append(
        Step(
            f"P11 消融阶梯（五档 × {CASE_TOTAL} 例）",
            [
                py,
                "-m",
                "decision_poc.ablation",
                "--json",
                "samples/eval/ablation.json",
                "--report",
                "docs/reports/p11-ablation-report.md",
            ],
            expect=[r"指纹 ", r"L5 全量"],
            writes=["samples/eval/ablation.json", "docs/reports/p11-ablation-report.md"],
        )
    )
    steps.append(
        Step(
            "P11 决策锚定包 + 篡改检出",
            [
                py,
                "-m",
                "decision_poc.anchoring",
                "--event",
                EVENT,
                "--as-of",
                AS_OF,
                "--tamper-demo",
                "--json",
                "samples/eval/anchor_demo.json",
            ],
            expect=[r"链校验=通过", r"检出=True"],
            writes=["samples/eval/anchor_demo.json"],
        )
    )
    steps.append(
        Step(
            f"对外数字自检 {EVIDENCE_TOTAL} 项",
            [
                py,
                "-m",
                "decision_poc.evidence_check",
                "--json",
                "samples/eval/evidence_check.json",
                "--report",
                "docs/reports/evidence-check-report.md",
            ],
            expect=[rf"\b{EVIDENCE_TOTAL}/{EVIDENCE_TOTAL}\b"],
            writes=["samples/eval/evidence_check.json", "docs/reports/evidence-check-report.md"],
        )
    )
    if not include_slow:
        steps = [s for s in steps if not s.slow]
    return steps


def run_step(step: Step) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(SRC), env["PYTHONPATH"]]) if env.get("PYTHONPATH") else str(SRC)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # uv 的缓存与临时目录默认在用户目录下；在某些受限环境里子进程写不进去会直接失败，
    # 统一指到仓库内的 artifacts/（已被 .gitignore 忽略），保证"一条命令可复现"在干净机器上也成立。
    tmp_dir = ROOT / "artifacts" / "tmp"
    cache_dir = ROOT / "artifacts" / "uv-cache"
    for directory in (tmp_dir, cache_dir):
        directory.mkdir(parents=True, exist_ok=True)
    env["UV_CACHE_DIR"] = str(cache_dir)
    env["TMP"] = env["TEMP"] = env["TMPDIR"] = str(tmp_dir)
    started = time.perf_counter()
    record: dict[str, Any] = {"name": step.name, "argv": step.argv, "status": "failed"}
    try:
        proc = subprocess.run(  # noqa: S603 - argv 全部由本文件构造，无 shell
            step.argv,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=step.timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        record["exit_code"] = proc.returncode
        missing = [pat for pat in step.expect if not re.search(pat, output)]
        ok = proc.returncode == 0 and not missing
        record["status"] = "passed" if ok else "failed"
        record["missing_expectations"] = missing
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        record["status"] = "timeout"
        record["exit_code"] = None
    except OSError as exc:  # 命令不存在等
        output = f"无法执行: {exc}"
        record["status"] = "failed"
        record["exit_code"] = None
    record["seconds"] = round(time.perf_counter() - started, 2)
    record["output_tail"] = "\n".join(output.strip().splitlines()[-40:])
    record["output_chars"] = len(output)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一条命令生成并核对全部对外材料")
    parser.add_argument("--fast", action="store_true", help="跳过 pytest 与 P9 进化（只核对数字）")
    parser.add_argument("--list", action="store_true", help="只列出步骤，不执行")
    parser.add_argument("--keep-going", action="store_true", help="失败也继续执行后续步骤")
    args = parser.parse_args(argv)

    steps = build_steps(include_slow=not args.fast)
    if args.list:
        for index, step in enumerate(steps, 1):
            tag = " (慢)" if step.slow else ""
            print(f"{index:>2}. {step.name}{tag}\n    {' '.join(step.argv)}")
        print(f"\n共 {len(steps)} 步；期望检查 {sum(len(s.expect) for s in steps)} 条")
        return 0

    print(f"共 {len(steps)} 步；仓库根 {ROOT}\n")
    results: list[dict[str, Any]] = []
    failed = 0
    for index, step in enumerate(steps, 1):
        print(f"[{index:>2}/{len(steps)}] {step.name} ... ", end="", flush=True)
        record = run_step(step)
        results.append(record)
        if record["status"] == "passed":
            print(f"ok ({record['seconds']}s)")
            continue
        failed += 1
        print(f"{record['status']} ({record['seconds']}s)")
        if record.get("missing_expectations"):
            print(f"    缺少期望: {record['missing_expectations']}")
        tail = record.get("output_tail", "")
        if tail:
            print("\n".join(f"    | {line}" for line in tail.splitlines()[-15:]))
        if not args.keep_going:
            print("\n已中止；加 --keep-going 可继续跑完其余步骤。")
            break

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repo": str(ROOT),
        "python": sys.executable,
        "steps": results,
        "failed": failed,
        "total": len(steps),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    passed = sum(1 for record in results if record["status"] == "passed")
    unrun = len(steps) - len(results)
    if passed != len(steps):
        detail = f"{passed}/{len(steps)} 步通过"
        if unrun:
            detail += f"，{unrun} 步未执行"
        print(f"\n结果：{detail}。明细 {OUT_DIR / 'summary.json'}")
        return 1

    print("\n材料清单：")
    for step in steps:
        for rel in step.writes:
            path = ROOT / rel
            if path.is_file():
                print(f"  {rel:<58} {path.stat().st_size:>9,} B")
            else:
                print(f"  {rel:<58} 缺失（该命令本次未产出）")
    print(f"\n结果：{len(steps)}/{len(steps)} 步通过。明细 {OUT_DIR / 'summary.json'}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
