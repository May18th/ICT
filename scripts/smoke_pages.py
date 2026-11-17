"""页面冒烟：逐路由调 route()，查状态码 + 关键 DOM 标记。

为什么要直接调 route()：起 HTTP 服务再 curl 会引入端口占用/启动时序的假失败，
而 route() 就是 Handler 调的那个函数，测它等价且确定。

每个页面查三样：
1. 状态码 200；
2. 返回体非空且是 HTML/JSON；
3. 一两个**功能性**标记字符串（不是"能打开就算过"）——
   `/map2d` 必须带 `event_timeline` 注入的 `timeline` 字段与 `alarm-layer`，
   `/workbench` 必须带自检数字。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from decision_poc.web_demo import route  # noqa: E402

PAGES = [
    ("/", ("text/html",), ("Nexent", "内涝")),
    # 108 项证据比对在工作台自检栏；/status 是 7 项系统自洽检查，两者不是一回事
    ("/workbench", ("text/html",), ("自检", "108")),
    ("/map2d", ("text/html",), ("alarm-layer", '"timeline"', "台账反推")),
    # 现场入口：五幕推进全靠脚本，页面壳能不能出来就看 chips 容器有没有渲染
    ("/show", ("text/html",), ("一键演示", "id=chips", "board")),
    # 「能力边界」：A 组 8/8、B 组 2/8 都是抓取产物里的值，页面渲染不出这两个数就是数据没读到
    ("/limits", ("text/html",), ("能力边界", "8/8", "2/8")),
    # 「全部通过」是本机台账同步状态，不是渲染结果：本机跑过反馈、台账还没 sync 时
    # 这一行会变成「1 项需注意」，页面本身没坏。冒烟只该判「页面壳与检查表渲染出来没有」，
    # 同步状态由 /status 自己逐项列、由 selfcheck 负责——两者混在一起会让冒烟假红。
    ("/status", ("text/html",), ("共 7 项检查", "通过")),
    ("/review", ("text/html",), ("评分",)),
    ("/evolution", ("text/html",), ("评分",)),
    ("/api/portal", ("application/json",), ()),
]

#: 已下线的页面：状态码必须是 410（永久移除），不能再是 200。
RETIRED = [("/demo", 410)]

fails: list[str] = []
print("=" * 68)
print("页面冒烟：route() 直调")
print("=" * 68)
for path, ctypes, musts in PAGES:
    status, ctype, payload = route(path, "GET", None)
    body = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
    ok = status == 200
    detail = f"{status} {ctype.split(';')[0]} {len(payload)}B"
    if not ok:
        fails.append(f"{path} 状态 {status}")
    if not any(ctype_ in ctype for ctype_ in ctypes):
        ok = False
        fails.append(f"{path} Content-Type={ctype}")
        detail += " (类型不符)"
    missing = [m for m in musts if m not in body]
    if missing:
        ok = False
        fails.append(f"{path} 缺标记 {missing}")
    print(f"  [{'OK ' if ok else 'FAIL'}] {path:12s} {detail}"
          + (f"  缺 {missing}" if missing else ""))

for path, expect in RETIRED:
    status, _ctype, _payload = route(path, "GET", None)
    ok = status == expect
    if not ok:
        fails.append(f"{path} 已下线，但状态是 {status}（应为 {expect}）")
    print(f"  [{'OK ' if ok else 'FAIL'}] {path:12s} {status} (已下线)")

print()
print("=" * 68)
if fails:
    print(f"冒烟失败 {len(fails)} 项：")
    for f in fails:
        print(f"  - {f}")
    raise SystemExit(1)
print(f"全部 {len(PAGES)} 个路由通过，{len(RETIRED)} 个已下线页面返回 410")
