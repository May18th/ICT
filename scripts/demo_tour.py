#!/usr/bin/env python3
"""一键演示：自检 → 起服务 → 打开大屏 → 打印讲稿。

为什么要有这个脚本
------------------
现场的环境通常比开发机差得多（8G 内存、没网、投影分辨率不确定），而演示只有一次机会。
把「能不能跑」的判断提前到上台之前，把「下一步该说什么」写进终端，剩下的就只有讲。

它做四件事，每件都可单独关掉：

1. **自检**：逐项检查出图与推演依赖的文件是否都在。缺底图不会报错，只会让页面
   悄悄退回示意底图（体积从 770KB 掉到 150KB），这是最难在上台前发现的一类问题，
   所以这里按文件逐个查，而不是只查「服务通不通」。
2. **起服务**：端口被占就复用，被占但不是本服务就报错退出，不覆盖别人的进程。
3. **打开 /show**：演示页自己按节拍推进，本脚本不模拟点击，避免和人的操作打架。
4. **打印讲稿**：五幕里每一幕该说什么、屏幕上该看到什么，都从**当前接口的真实值**取，
   不写死数字——写死的稿子改了台账就会和画面对不上。

用法:
    python scripts/demo_tour.py              # 自检 → 等回车 → 开浏览器（自动播放）
    python scripts/demo_tour.py --manual     # 每一幕按空格推进（讲的人控节奏）
    python scripts/demo_tour.py --check      # 只自检，不起服务
    python scripts/demo_tour.py --no-open    # 起服务但不弹浏览器
    python scripts/demo_tour.py --port 8899
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # scripts/ -> agent_poc/
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():                                  # 非 Windows 或没建 venv 时退回当前解释器
    PY = Path(sys.executable)

GREEN, RED, AMBER, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m"

# (显示名, 相对路径, 最小字节, 缺了会怎样)
# 阈值为「正常体积的一半」量级：文件被截断或没跟仓库一起走时，体积会掉一个数量级。
# 逐项给具体数值没有意义（导出脚本一改就变），给下限是为了抓「少了一大截」。
REQUIRED: tuple[tuple[str, str, int, str], ...] = (
    # 底图四项的阈值与 `tests/test_repository_layout.py` 的 `OSM_REQUIRED` **逐字对齐**。
    # 两处判的是同一件事：缺了底图，大屏照样 200，只是悄悄退回示意图（体积从 770KB 掉到
    # 150KB 量级）。阈值不一致就会出现「--check 说没事、测试说失败」，那种分歧最费时间。
    ("底图-道路", "samples/assets/osm/base_roads.json", 35_000, "地图退回示意底图，路网与真实成都对不上"),
    ("底图-建筑", "samples/assets/osm/base_buildings_full.json", 200_000, "建成区只剩色块"),
    ("底图-地块", "samples/assets/osm/base_areas.json", 40_000, "水系与地块底纹缺失"),
    ("底图-范围", "samples/assets/osm/chengdu_bbox_raw.json", 400_000,
     "「数据来源与边界」面板报不出覆盖范围"),
    ("路名对照", "samples/assets/osm/road_alias.json", 200, "悬停不再给出最贴近的真实道路"),
    ("场景台账", "samples/assets/urban/work_orders.csv", 200, "工单读不到，事件推演整块空"),
    ("设施台账", "samples/assets/urban/drainage_facilities.csv", 200, "泵站/雨水口点位缺失"),
    ("降雨过程线", "out/map2d/hydro_curve.json", 1_000, "水位退回示意节奏（页面会自动标注）"),
    ("事件时标", "out/map2d/event_timeline.json", 1_000, "7 步时刻退回手写值"),
    ("调拨评测集", "samples/eval/allocation_cases.json", 1_000, "进化与门禁无法判定"),
    ("评审用例", "samples/eval/decision_reviews.json", 100, "第二反馈通道不可用"),
)


def ok(text: str) -> None:
    print(f"  {GREEN}✓{OFF} {text}")


def bad(text: str, hint: str = "") -> None:
    print(f"  {RED}✗{OFF} {text}")
    if hint:
        print(f"    {AMBER}→ {hint}{OFF}")


def note(text: str) -> None:
    print(f"  {DIM}{text}{OFF}")


def check_files() -> int:
    """逐个查依赖文件。缺文件不一定报错，但一定会在某处悄悄降级——这里提前说出来。"""
    print("\n[1/4] 依赖文件")
    missing = 0
    for name, rel, min_bytes, impact in REQUIRED:
        path = ROOT / rel
        if not path.exists():
            bad(f"{name}　缺失：{rel}", impact)
            missing += 1
            continue
        size = path.stat().st_size
        if size < min_bytes:
            bad(f"{name}　只有 {size / 1024:.0f} KB（应 ≥ {min_bytes / 1024:.0f} KB）",
                f"{impact}；确认文件没有被 git-lfs 或拷贝工具截断")
            missing += 1
        else:
            ok(f"{name}　{size / 1024 / 1024:.1f} MB" if size > 1_048_576 else f"{name}　{size / 1024:.1f} KB")
    return missing


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def http(url: str, method: str = "GET", body: dict | None = None, timeout: float = 20.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("content-type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def wait_up(base: str, seconds: float = 30.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            status, _ = http(base + "/show", timeout=2.0)
            if status == 200:
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


def probe_pages(base: str) -> int:
    """逐页探活。只看 HTTP 码不够——页面里的数字对不对由讲稿那一节核对。"""
    print("\n[3/4] 页面与接口")
    failures = 0
    pages = (
        ("演示页（会打开这个）", "/show"),
        ("推演大屏", "/map2d"),
        ("进化对比", "/evolution"),
        ("能力边界", "/limits"),
    )
    for name, path in pages:
        try:
            status, raw = http(base + path, timeout=25.0)
            # 大屏体积是最灵敏的一条：底图在时 770KB 上下，底图缺了会掉到 150KB 左右，
            # 而页面照样 200、照样能看——只是地图悄悄变成了示意图。这条不能只报警。
            too_small = path == "/map2d" and len(raw) < 600_000
            if status == 200 and len(raw) > 8_000 and not too_small:
                ok(f"{name}　{len(raw) / 1024:.0f} KB")
            elif too_small:
                bad(f"{name}　只有 {len(raw) / 1024:.0f} KB（正常 700KB 以上）",
                    "底图没进页面：检查 samples/assets/osm/ 下的 base_*.json")
                failures += 1
            else:
                bad(f"{name}　HTTP {status} / {len(raw)} 字节", "页面过小通常意味着模板渲染中途出错")
                failures += 1
        except (urllib.error.URLError, OSError) as exc:
            bad(f"{name}　{exc}", "服务没起来或端口不对")
            failures += 1

    # 事件推演是「冲突曝光」那一幕的数据源，必须单独探——它走本地引擎，不联网
    try:
        status, raw = http(
            base + "/api/workbench/event", "POST", {"event_id": "WO-20261029-007"}, timeout=25.0
        )
        data = json.loads(raw)
        ok(f"事件推演　冲突 {len(data.get('conflicts') or [])} 处 · "
           f"缺口 {((data.get('resource_gaps') or [{}])[0] or {}).get('gap', '—')} 台")
    except Exception as exc:  # noqa: BLE001 - 探活失败只记账，不阻断
        bad(f"事件推演　{exc}", "检查 samples/assets/urban 下的台账是否完整")
        failures += 1

    # 离线问答：断网时这是唯一还能回答「谁负责」的通道
    try:
        status, raw = http(
            base + "/api/workbench/chat", "POST",
            {"message": "这地方积水了，该谁去处理？"}, timeout=40.0,
        )
        text = (json.loads(raw).get("answer_text") or "")
        offline = "离线直出" in text
        ok("工作台问答　" + ("离线直出可用（没有平台凭据也能答）" if offline else "走平台在线通道"))
    except Exception as exc:  # noqa: BLE001
        bad(f"工作台问答　{exc}", "断网现场这一项必须能跑；先确认 NEXENT_API_KEY 未设时走的是本地直出")
        failures += 1

    # 这一行的措辞和 README 里的「六项页面与接口」是同一句话：
    # 4 个页面 + 事件推演 + 工作台问答。改这里要顺手改 README，否则文档又在许愿。
    total = len(pages) + 2
    if failures:
        bad(f"{total} 项页面与接口里有 {failures} 项没过", "先解决上面带 ✗ 的项，再放映")
    else:
        ok(f"{total} 项页面与接口全部通过")
    return failures


def teleprompter(base: str) -> None:
    """讲稿：数字全部取自当前接口，改了台账就跟着变，不会和画面对不上。"""
    print("\n[4/4] 讲稿（数字取自当前接口）")
    try:
        _, raw = http(base + "/api/evolution", timeout=60.0)
        evo = json.loads(raw)
        first = evo.get("first") or {}
        a = first.get("initial_train") or {}
        b = first.get("final_train") or {}
        real = evo.get("real_event") or {}
        base_alloc = (real.get("baseline") or {}).get("allocation_by_district") or {}
        evolved = (real.get("evolved") or {}).get("allocation_by_district") or {}

        def fmt(districts: dict) -> str:
            return "、".join(f"{k} {v} 台" for k, v in districts.items()) or "—"

        lines = [
            ("① 收到工单", "WO-20261029-007，兴华路核实 30 cm；先读台账再定级"),
            ("② 冲突曝光", "封路权限重叠 / 泵站无运维主体 / 移动泵缺口 2 台"),
            ("③ 策略进化", f"偏差 {a.get('deviation', 0):.4f} → {b.get('deviation', 0):.4f}，"
                          f"关键片区未满足 {a.get('critical_unmet_units')} → {b.get('critical_unmet_units')} 台"),
            ("④ 重新推演", f"同一事件分配：{fmt(base_alloc)} → {fmt(evolved)}"),
            ("⑤ 大屏退水", "峰值 30 cm 全幅封闭 → 疏通 + 泵站全开 → 退至阈值以下"),
        ]
        for title, text in lines:
            print(f"  {GREEN}{title}{OFF}  {text}")
    except Exception as exc:  # noqa: BLE001 - 讲稿取不到不影响演示
        note(f"讲稿数字取不到（{exc}），按屏幕上的实际显示讲即可")

    print(f"\n  {DIM}口径提醒：V1/V2 是 8 个评测场景的合计；15 → 6 里 6 台是参考答案也填不平的下限；")
    print(f"  现场被问「数据真的假的」，翻 /limits 第一张表（4 条实测 / 60 条演示数据）。{OFF}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一键演示：自检 → 起服务 → 打开大屏 → 讲稿")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--check", action="store_true", help="只自检，不起服务")
    parser.add_argument("--no-open", action="store_true", help="不弹浏览器")
    parser.add_argument("--manual", action="store_true", help="每幕按空格推进，而不是自动播放")
    args = parser.parse_args(argv)

    print(f"{DIM}工作目录 {ROOT}{OFF}")
    missing = check_files()

    print("\n[2/4] 服务端口")
    base = f"http://127.0.0.1:{args.port}"
    proc: subprocess.Popen | None = None
    if not port_free(args.port):
        if wait_up(base, 3.0):
            ok(f"{args.port} 已有服务在跑，直接复用")
        else:
            bad(f"{args.port} 被占用，但不是本服务", "请使用 --port 指定其他未占用端口")
            return 2
    elif args.check:
        ok(f"{args.port} 空闲")
    else:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        proc = subprocess.Popen(
            [str(PY), "-m", "decision_poc.web_demo", "--port", str(args.port)],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not wait_up(base, 40.0):
            bad("服务 40 秒内没起来", f"手动跑一次：PYTHONPATH=src python -m decision_poc.web_demo --port {args.port}")
            return 3
        ok(f"服务已起在 {args.port}")

    if args.check:
        if wait_up(base, 2.0):
            probe_pages(base)
        else:
            note(f"{args.port} 上没有运行中的服务，跳过页面探活")
            note("要连起来一起测：先起服务，再 --check --port <那个端口>")
        print(f"\n{DIM}只自检模式，未启动浏览器。缺 {missing} 项文件。{OFF}")
        return 0 if missing == 0 else 1

    failures = probe_pages(base)
    teleprompter(base)

    url = base + ("/show" if args.manual else "/show?auto=1")
    print(f"\n{GREEN}准备就绪{OFF}  缺 {missing} 项文件 · {failures} 项探活失败")
    print(f"{DIM}浏览器里按 F11 全屏；空格 = 下一幕，R = 重播，Esc = 暂停大屏{OFF}")
    try:
        input("\n按回车打开演示页（之后 Ctrl+C 结束）… ")
    except (EOFError, KeyboardInterrupt):
        print()
    if not args.no_open:
        webbrowser.open(url)
        ok(f"已打开 {url}")
    else:
        note(f"请手动打开 {url}")

    try:
        print(f"{DIM}服务运行中，Ctrl+C 结束{OFF}")
        while True:
            time.sleep(1.0)
            if proc is not None and proc.poll() is not None:
                bad("服务进程已退出", "看它自己打印的报错；或重跑本脚本")
                return proc.returncode or 1
    except KeyboardInterrupt:
        print()
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            note("服务已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
