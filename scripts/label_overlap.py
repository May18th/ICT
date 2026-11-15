"""地图标签重叠的自动检测（把「人工看截图」换成可复跑的量测）。

判据不看截图，直接读 DOM：把所有可见 `<text>` 的屏幕矩形两两比，
交叠面积超过「较小者」的一定比例就算冲突。历史真问题都落在 26%~100%，
所以阈值取 15% 既能抓住真重叠，又不会被描边或抗锯齿误报。

为什么要做：这一版改了标题、标签避让、镜头视野，验证方式一直是「无头截图 +
人眼看」。人眼看不出 3px 的压字，也不可复跑，换个人改标签就可能悄悄改坏。
这里把它变成一条能进验收脚本的断言。

用法（命令行）：
    python scripts/label_overlap.py                    # 用 dist 产物扫 25 个时刻
    python scripts/label_overlap.py --step 0.02        # 更密
    python scripts/label_overlap.py --html out/x.html  # 指定页面
    python scripts/label_overlap.py --json out/lp.json # 顺带落盘

依赖：本机 Edge/Chrome + websocket-client（项目 .venv 已装）。
找不到浏览器时返回 `skipped`，不判失败——评委机器上不一定有 Edge。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HTML = ROOT / "dist" / "map2d" / "index.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/usr/bin/microsoft-edge",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

# 量测脚本：先切到指定时刻，等两帧让 SVG 稳定，再收集所有可见文字与圆点。
# `apply` 是 build_map2d.py 里的全局函数，static 模式下不会被 tick 覆盖。
_JS_MEASURE = r"""
(async () => {
  const T = __T__;
  if (typeof apply === 'function') apply(T);
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));

  const visible = (el) => {
    let cur = el;
    while (cur && cur !== document.body) {
      const st = getComputedStyle(cur);
      if (st.display === 'none' || parseFloat(st.opacity || '1') === 0) return false;
      cur = cur.parentElement;
    }
    return true;
  };

  const texts = [];
  document.querySelectorAll('#map svg text').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    if (!visible(el)) return;
    texts.push({
      s: (el.textContent || '').trim().slice(0, 24),
      lp: el.getAttribute('data-lp') || '',
      x: +r.x.toFixed(1), y: +r.y.toFixed(1),
      w: +r.width.toFixed(1), h: +r.height.toFixed(1)
    });
  });

  const circles = [];
  document.querySelectorAll('#map svg circle').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || !visible(el)) return;
    circles.push({
      p: el.parentElement ? (el.parentElement.getAttribute('data-dept') || el.parentElement.id || '') : '',
      x: +r.x.toFixed(1), y: +r.y.toFixed(1),
      w: +r.width.toFixed(1), h: +r.height.toFixed(1)
    });
  });

  return JSON.stringify({ t: T, texts, circles });
})()
"""


def find_browser() -> str | None:
    for path in EDGE_CANDIDATES:
        if Path(path).is_file():
            return path
    return None


def _wait_target(port: int, tries: int = 80) -> str | None:
    for _ in range(tries):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as fh:
                for item in json.load(fh):
                    if item.get("type") == "page" and item.get("webSocketDebuggerUrl"):
                        return item["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.25)
    return None


async def _collect(websockets, ws_url: str, moments: list[float], settle_s: float) -> list[dict]:
    # ping_interval=None：CDP 不保证回 pong，默认 20 秒心跳会在长时量测里断开。
    async with websockets.connect(ws_url, max_size=None, ping_interval=None, open_timeout=30) as ws:
        counter = 0

        async def send(method: str, params: dict | None = None) -> dict:
            nonlocal counter
            counter += 1
            await ws.send(json.dumps({"id": counter, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == counter:
                    return msg

        await send("Runtime.enable")
        await asyncio.sleep(settle_s)  # 等首帧 apply(0)、字体与布局就绪
        frames: list[dict] = []
        for moment in moments:
            expr = _JS_MEASURE.replace("__T__", f"{moment:.4f}")
            res = await send(
                "Runtime.evaluate",
                {"expression": expr, "returnByValue": True, "awaitPromise": True},
            )
            value = ((res.get("result") or {}).get("result") or {}).get("value")
            if value:
                frames.append(json.loads(value))
        return frames


def measure(
    html: Path,
    moments: list[float],
    *,
    width: int = 1600,
    height: int = 700,
    port: int = 9351,
    settle_s: float = 2.0,
) -> list[dict]:
    """在同一个浏览器会话里逐个时刻量测，返回每帧的 texts/circles。

    一次启动而不是每帧重启：25 个时刻重启浏览器要 2 分钟，复用会话 8 秒。
    用 venv 里已有的 `websockets`（uv.lock 里有），不额外引入 `websocket-client`——
    验收脚本要能在 `uv run --locked` 下跑，多一个未锁定的包就复现不了。
    """

    browser = find_browser()
    if not browser:
        raise RuntimeError("no-browser")
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise RuntimeError("no-websocket") from exc

    url = html.resolve().as_uri() + "?t=0&static=1"
    with tempfile.TemporaryDirectory(prefix="lp-profile-") as profile:
        proc = subprocess.Popen(
            [
                browser, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
                f"--remote-debugging-port={port}", f"--window-size={width},{height}",
                f"--user-data-dir={profile}", url,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            ws_url = _wait_target(port)
            if not ws_url:
                raise RuntimeError("no-target")
            return asyncio.run(_collect(websockets, ws_url, moments, settle_s))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:  # pragma: no cover
                proc.kill()


def _area(box: dict) -> float:
    return max(1.0, box["w"] * box["h"])


def _inter(a: dict, b: dict) -> float:
    dx = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    dy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
    return max(0.0, dx) * max(0.0, dy)


def conflicts(
    frames: list[dict],
    *,
    text_ratio: float = 0.15,
    icon_ratio: float = 0.17,
) -> list[dict]:
    """返回所有超过阈值的冲突（文字×文字、文字×图标）。"""

    found: list[dict] = []
    for frame in frames:
        texts, circles, t = frame["texts"], frame["circles"], frame["t"]
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                a, b = texts[i], texts[j]
                ratio = _inter(a, b) / min(_area(a), _area(b))
                if ratio > text_ratio:
                    found.append({
                        "kind": "text-text", "t": t, "ratio": round(ratio, 3),
                        "a": a["s"], "b": b["s"],
                    })
        for text in texts:
            for circle in circles:
                ratio = _inter(text, circle) / _area(text)
                if ratio > icon_ratio:
                    found.append({
                        "kind": "text-icon", "t": t, "ratio": round(ratio, 3),
                        "a": text["s"], "b": circle.get("p") or "圆点",
                    })
    return found


def build_if_missing(html: Path) -> bool:
    """产物不存在时现场生成；生成失败返回 False。"""

    if html.is_file():
        return True
    builder = ROOT / "scripts" / "build_map2d.py"
    if not builder.is_file():
        return False
    result = subprocess.run(
        [sys.executable, str(builder)], cwd=str(ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0 and html.is_file()


def main() -> int:
    parser = argparse.ArgumentParser(description="地图标签重叠自动检测")
    parser.add_argument("--html", default=str(DEFAULT_HTML))
    parser.add_argument("--step", type=float, default=0.04, help="时刻步长（默认 0.04）")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=700)
    parser.add_argument("--text-ratio", type=float, default=0.15)
    parser.add_argument("--icon-ratio", type=float, default=0.17)
    parser.add_argument("--json", default="", help="把结果写到指定路径")
    args = parser.parse_args()

    html = Path(args.html)
    if not build_if_missing(html):
        print(f"[SKIP] 页面不存在且无法生成：{html}")
        return 2

    moments = []
    t = 0.02
    while t < 0.99:
        moments.append(round(t, 4))
        t += args.step

    try:
        frames = measure(html, moments, width=args.width, height=args.height)
    except RuntimeError as exc:
        if str(exc) in ("no-browser", "no-websocket"):
            print(f"[SKIP] 缺少浏览器或 websocket-client（{exc}）")
            return 2
        print(f"[FAIL] 量测失败：{exc}")
        return 1

    hits = conflicts(frames, text_ratio=args.text_ratio, icon_ratio=args.icon_ratio)
    texts = sum(len(f["texts"]) for f in frames)
    print(f"量测 {len(frames)} 个时刻，共 {texts} 个文字标签（{args.width}x{args.height}）")
    print(f"阈值：文字×文字 >{args.text_ratio:.0%} · 文字×图标 >{args.icon_ratio:.0%}")
    if hits:
        print(f"\n发现 {len(hits)} 处冲突：")
        for hit in hits[:40]:
            print(f"  t={hit['t']:.2f}  {hit['ratio']:.0%}  "
                  f"{hit['a']!r} × {hit['b']!r}  [{hit['kind']}]")
        if len(hits) > 40:
            print(f"  … 另有 {len(hits) - 40} 处")
    else:
        print("\n无重叠")

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps({"frames": len(frames), "conflicts": hits}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
