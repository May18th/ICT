"""窄屏实测：在多个宽度下量真实布局，输出 JSON，供 `docs/reports/窄屏实测报告.md` 引用。

为什么不用「数 @media 断点」的办法
--------------------------------
断点数不等于覆盖度。一个页面写了 7 档断点，另两页可能一档都没有；同一份 `web_demo.py`
里有三套互不共享的 `<style>`，写在 A 页面的 `@media` 不会救 B 页面。所以只能量。

为什么需要一个同源转发
--------------------
测量靠父页读 iframe 的 `contentDocument`。跨端口就是跨域，读不到布局。所以本脚本在
测量页与演示服务之间加一层转发，让两者同源——`@media` 才会按 iframe 宽度生效。

用法::

    # 1) 先起演示服务
    .venv/Scripts/python.exe -m decision_poc.web_demo --port 8901

    # 2) 再跑本脚本（自动找 Edge/Chrome，自动驱动 headless）
    .venv/Scripts/python.exe scripts/measure_narrow.py --target http://127.0.0.1:8901

    # 只出测量页，自己拿浏览器打开（调试用）
    .venv/Scripts/python.exe scripts/measure_narrow.py --serve-only

产出：`artifacts/reports/narrow-measure.json`（每页每宽一条记录，含溢出元素明细）。
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
PAGE = Path(__file__).resolve().parent / "narrow_measure_page.html"
DEFAULT_OUT = ROOT / "artifacts" / "reports" / "narrow-measure.json"

#: 浏览器位置。Edge 优先——Windows 上几乎必然存在，且版本比 Chrome 统一。
BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

SKIP_HEADERS = {"host", "connection", "accept-encoding", "content-length"}


def find_browser() -> str | None:
    for path in BROWSERS:
        if Path(path).is_file():
            return path
    for name in ("msedge", "chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def make_handler(upstream: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, ctype: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionAbortedError):
                pass

        def _proxy(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            req = urllib.request.Request(
                upstream + self.path,
                data=body,
                method=method,
                headers={k: v for k, v in self.headers.items() if k.lower() not in SKIP_HEADERS},
            )
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    self._send(resp.status, resp.headers.get("Content-Type", "application/octet-stream"), resp.read())
            except urllib.error.HTTPError as exc:
                self._send(exc.code, exc.headers.get("Content-Type", "text/plain"), exc.read())
            except Exception as exc:  # noqa: BLE001
                self._send(502, "text/plain", f"{type(exc).__name__}: {exc}".encode())

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] in ("/measure", "/measure.html"):
                self._send(200, "text/html; charset=utf-8", PAGE.read_bytes())
                return
            self._proxy("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._proxy("POST")

        def log_message(self, *_args) -> None:
            return

    return Handler


def run_headless(browser: str, url: str, budget_ms: int) -> str:
    with tempfile.TemporaryDirectory(prefix="narrow-profile-") as profile:
        cmd = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-extensions",
            f"--user-data-dir={profile}",
            f"--virtual-time-budget={budget_ms}",
            "--dump-dom",
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=budget_ms / 1000 + 120)
        return proc.stdout.decode("utf-8", errors="replace")


def parse_result(html: str) -> list[dict]:
    match = re.search(r'<pre id="R">([^<]*)</pre>', html)
    if not match:
        log = re.search(r'id="log">([^<]*)', html)
        raise RuntimeError(f"测量页没有回填结果（状态：{log.group(1) if log else '未知'}）")
    return json.loads(base64.b64decode(match.group(1)).decode("utf-8"))


def summarize(records: list[dict]) -> str:
    lines = [
        f"{'page':<14}{'w':>5}{'scrollW':>9}{'clientW':>9}{'溢出':>6}{'滚动':>6}{'裁切':>6}  结论",
        "-" * 84,
    ]
    for r in records:
        if "error" in r:
            lines.append(f"{r['page']:<14}{r['width']:>5}  ERROR {r['error'][:40]}")
            continue
        if not r.get("loaded", True):
            lines.append(
                f"{r['page']:<14}{r['width']:>5}  ERROR 页面没加载成功"
                f"（body {r.get('bodyLen', 0)} 字符）——这张表不可用"
            )
            continue
        over = r["scrollW"] - r["clientW"]
        items = r.get("items") or []
        # 祖先设了 overflow-x:auto 且自身可滚 —— 用户可以横向滑动看到，属设计内行为
        hosted = [i for i in items if (i.get("host") or {}).get("canScroll")]
        clipped = [i for i in items if not (i.get("host") or {}).get("canScroll")]
        svg = r.get("svg")
        inner = r.get("clippedInside") or []
        if svg and "map" in r["page"]:
            note = f"地图 {svg['cssW']}px"
        elif over > 1:
            note = f"整页横向溢出 {over}px"
        elif clipped:
            note = f"内容被裁 {len(clipped)} 处"
        elif hosted:
            note = f"卡内可滚 {len(hosted)} 处（可用）"
        elif inner:
            note = f"文字被容器裁 {len(inner)} 处"
        else:
            note = "正常"
        flag = "是" if over > 1 else "-"
        lines.append(
            f"{r['page']:<14}{r['width']:>5}{r['scrollW']:>9}{r['clientW']:>9}{flag:>6}"
            f"{len(hosted):>6}{len(clipped):>6}  {note}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多宽度真实布局量测")
    parser.add_argument("--target", default="http://127.0.0.1:8901", help="演示服务地址")
    parser.add_argument("--port", type=int, default=8903, help="测量转发端口")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="结果 JSON 路径")
    parser.add_argument("--budget", type=int, default=150000, help="headless 虚拟时间预算（ms）")
    parser.add_argument("--serve-only", action="store_true", help="只起转发，不自动截图测")
    args = parser.parse_args(argv)

    if not PAGE.is_file():
        print(f"缺少测量页：{PAGE}", file=sys.stderr)
        return 2

    handler = make_handler(args.target.rstrip("/"))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    measure_url = f"http://127.0.0.1:{args.port}/measure"
    print(f"测量页：{measure_url}  →  {args.target}")

    # 先确认目标服务在跑。服务没起时 iframe 加载空白页，所有页面都会「无溢出」，
    # 静默产出一张全绿的表——比直接报错危险得多。
    try:
        with urllib.request.urlopen(args.target, timeout=10) as probe:
            if probe.status != 200:
                print(f"目标服务返回 {probe.status}：{args.target}", file=sys.stderr)
                return 2
    except Exception as exc:  # noqa: BLE001
        print(f"目标服务不可达：{args.target}（{type(exc).__name__}: {exc}）", file=sys.stderr)
        print("先把演示服务起起来：python -m decision_poc.web_demo --port 8901", file=sys.stderr)
        return 2

    if args.serve_only:
        print("只起转发模式，Ctrl+C 结束。")
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
        return 0

    browser = find_browser()
    if not browser:
        print("找不到 Edge/Chrome，无法自动测量。用 --serve-only 手动打开测量页。", file=sys.stderr)
        return 2

    print(f"浏览器：{browser}")
    html = run_headless(browser, measure_url, args.budget)
    records = parse_result(html)
    print()
    print(summarize(records))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已写入 {out}（{len(records)} 组）")

    failed = [r for r in records if not r.get("loaded", True)]
    if failed:
        pages = sorted({f"{r['page']}@{r['width']}" for r in failed})
        print(
            f"\n{len(failed)} 组页面没加载成功（{', '.join(pages[:6])}…），"
            "这次的结论不可用。检查服务与转发端口。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
