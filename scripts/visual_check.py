"""界面可读性自动检查：把「人工看截图」变成一条可失败的断言。

为什么**不**做成 pytest 用例
----------------------------
这个检查依赖本机浏览器（Edge / Chrome）。放进 `tests/` 会带来两个坏处：
  1. 没有浏览器的机器（评委、CI、干净 clone）会假失败，把 `reproduce.py` 拖红；
  2. 测试数会从 971 变成 982+，触发 `test_frozen_numbers.py` 的口径连锁更新
     （期望文件两处 + 20 份 md + docx + pptx 重生成），代价远大于收益。
所以做成独立脚本：有退出码、有 JSON 留证，需要时手动或写进验收流程调用。

判据（任意一条触发即失败）
--------------------------
  1. 文字 × 文字：相互覆盖占「较小者面积」> 12%
  2. 文字 × 圆点：文字被图标盖住 > 17%
  3. 出界：文字框越出地图可视区 > 2 px
白描边（paint-order: stroke）不算重叠——它本来就是压在字上的同色描边。

用法
----
    python scripts/visual_check.py                  # 6 个时刻，全景（static）
    python scripts/visual_check.py --with-cam       # 追加 3 个自动镜头帧
    python scripts/visual_check.py --json out/visual_check.json
    python scripts/visual_check.py --url file:///... --no-build

退出码：0 通过 / 1 有重叠 / 2 环境不可用（找不到浏览器、页面没渲染出来）
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
MAP_HTML = ROOT / "dist" / "map2d" / "index.html"

# 采样时刻：覆盖 7 跳的每一段，含末段的「待解封」灰字。
STATIC_TIMES = (0.05, 0.22, 0.33, 0.50, 0.68, 0.85)
# 自动镜头帧：视野收紧后字被放大，是重叠最容易出现的地方，单独再测一遍。
CAM_TIMES = (0.33, 0.56, 0.76)

TEXT_OVERLAP_RATIO = 0.12   # 文字×文字：较小者被盖 12% 以上算撞
ICON_OVERLAP_RATIO = 0.17   # 文字×圆点：字被图标盖 17% 以上算撞
OUT_OF_BOUNDS_PX = 2.0

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

COLLECT_JS = r"""
(() => {
  const stage = document.getElementById('stage');
  const map = document.getElementById('map');
  if (!map) return JSON.stringify({ error: 'no #map' });
  const sr = stage ? stage.getBoundingClientRect() : map.getBoundingClientRect();
  // 逐个祖先查 display/opacity：hidden 的泳道、未介入的部门都不该参与判重
  const vis = (el) => {
    let p = el;
    while (p && p !== document.body) {
      const s = getComputedStyle(p);
      if (s.display === 'none' || parseFloat(s.opacity || '1') === 0) return false;
      p = p.parentElement;
    }
    return true;
  };
  // 给父组编个临时 id：同一个组里的文字与圆点（如告警徽标的「!」和自己的底色圆）
  // 天然叠在一起，属于设计，不能算遮挡。
  let gidSeq = 0;
  const gid = (el) => {
    const p = el.parentElement;
    if (!p) return -1;
    if (p.__gid === undefined) p.__gid = ++gidSeq;
    return p.__gid;
  };
  const texts = [];
  // 只取 <text> 自身的文本节点：它有 <title> 子元素（悬停提示），
  // textContent 会把提示文字拼进标签名，判重时变成「兴华路片区兴华路片区（A-03）…」这种假条目。
  const own = (t) => {
    let s = '';
    t.childNodes.forEach(n => { if (n.nodeType === 3) s += n.textContent; });
    return s.replace(/\s+/g, ' ').trim();
  };
  map.querySelectorAll('text').forEach(t => {
    if (!vis(t)) return;
    const body = own(t);
    if (!body) return;
    const r = t.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    texts.push({
      s: body.slice(0, 40),
      lp: t.dataset.lp || '',
      gid: gid(t),
      x: +r.x.toFixed(1), y: +r.y.toFixed(1),
      w: +r.width.toFixed(1), h: +r.height.toFixed(1)
    });
  });
  const circles = [];
  // 只算「图标级」圆点：部门/设施点位（r 约 4~16 的实心圆）。
  // 告警虚线环 r=26、影响范围大圈、描边圆（fill=none）都不算——文字压在大圈上
  // 不构成遮挡，把它们算进去会产生成百条假阳性（实测 268 条里绝大多数是这个）。
  map.querySelectorAll('circle').forEach(c => {
    if (!vis(c)) return;
    const rAttr = parseFloat(c.getAttribute('r') || '0');
    if (rAttr < 3 || rAttr > 16) return;
    const fill = getComputedStyle(c).fill || '';
    if (fill === 'none' || fill === 'rgba(0, 0, 0, 0)') return;
    const r = c.getBoundingClientRect();
    if (r.width < 4) return;
    circles.push({
      p: c.parentElement ? (c.parentElement.dataset.dept || c.parentElement.id || '') : '',
      gid: gid(c),
      x: +r.x.toFixed(1), y: +r.y.toFixed(1),
      w: +r.width.toFixed(1), h: +r.height.toFixed(1)
    });
  });
  return JSON.stringify({
    viewport: [innerWidth, innerHeight, devicePixelRatio],
    stage: sr ? { x: +sr.x.toFixed(1), y: +sr.y.toFixed(1), w: +sr.width.toFixed(1), h: +sr.height.toFixed(1) } : null,
    texts, circles
  });
})()
"""


def find_browser() -> str | None:
    for path in EDGE_CANDIDATES:
        if Path(path).is_file():
            return path
    for name in ("msedge", "chrome", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


class MiniWebSocket:
    """最小 WebSocket 客户端（RFC 6455 子集），只够跑 CDP。

    为什么要自己写：`websocket-client` 不在项目依赖里。为了跑一条界面检查去改
    `pyproject.toml` + 锁文件，代价大于收益；而没有这个兜底，检查在干净环境里
    会直接变成「跳过」，等于没做。

    只实现客户端必需的三件事：握手、掩码发送、收完整消息（含分片与 ping）。
    """

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (f"?{u.query}" if u.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {u.hostname}:{u.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket 握手被关闭")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(f"WebSocket 握手失败：{head[:120]!r}")
        self._buf = rest

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(n - len(self._buf), 4096))
            if not chunk:
                raise ConnectionError("CDP 连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text: str) -> None:
        self._frame(0x1, text.encode())

    def recv(self) -> str:
        parts: list[bytes] = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            masked, length = b2 & 0x80, b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else None
            payload = self._read(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("收到 close 帧")
            if opcode == 0x9:
                self._frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


class _StdWS:
    """`websocket-client` 的薄包装，和自带实现同一套 send/recv 接口。"""

    def __init__(self, conn) -> None:
        self._conn = conn

    def send(self, text: str) -> None:
        self._conn.send(text)

    def recv(self) -> str:
        return self._conn.recv()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def make_ws(url: str, timeout: float):
    """优先用 `websocket-client`；没有才退回自带实现。

    实测：Edge 的 CDP 对自带实现偶发「握手成功但消息被丢弃」，换成
    `websocket-client` 就稳定。所以首选外部库，自带实现只作无依赖兜底。
    """
    try:
        import websocket  # type: ignore
    except ImportError:
        return MiniWebSocket(url, timeout=timeout)
    return _StdWS(websocket.create_connection(url, timeout=timeout))


class CDP:
    """极简 CDP 客户端：只用到 navigate / evaluate / screenshot。

    Edge 有一处不稳定行为：`/json` 已经返回 target、WebSocket 握手也返回 101，
    但此时 target 的 renderer 可能还没就绪，发过去的消息被静默丢弃（表现为收包超时）。
    实测同一个 target 有时秒回、有时 8 秒无响应，所以不能靠「握手成功」判断就绪，
    必须发一条最便宜的探活请求、真的收到响应才算连上，失败就重连。
    """

    def __init__(self, browser: str, width: int, height: int, profile: Path) -> None:
        self.port = 9400 + (os.getpid() % 200)
        self.proc = subprocess.Popen(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--enable-unsafe-swiftshader",
                f"--remote-debugging-port={self.port}",
                f"--window-size={width},{height}",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.ws: MiniWebSocket | None = None
        self._id = 0
        self.ws_url = self._wait_target(40.0)
        self._connect()
        self.send("Runtime.enable")
        self.send("Page.enable")

    def _wait_target(self, deadline: float) -> str:
        end = time.time() + deadline
        while time.time() < end:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=2) as fh:
                    for target in json.load(fh):
                        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                            return target["webSocketDebuggerUrl"]
            except Exception:
                pass
            time.sleep(0.25)
        raise RuntimeError(f"40 秒内没拿到调试目标（端口 {self.port}）")

    def _connect(self, retries: int = 15) -> None:
        last: Exception | None = None
        for attempt in range(retries):
            try:
                self.ws = make_ws(self.ws_url, 3)
                res = self.send("Runtime.evaluate", {"expression": "1", "returnByValue": True})
                if res.get("result"):
                    return
                last = RuntimeError(f"探活无结果：{str(res)[:80]}")
            except Exception as exc:
                last = exc
            try:
                if self.ws:
                    self.ws.close()
            except Exception:
                pass
            self.ws = None
            time.sleep(0.4)
            if attempt >= 3:   # 前三次大概率是 target 没就绪，之后才去重新取 target
                try:
                    self.ws_url = self._wait_target(10.0)
                except Exception:
                    pass
        raise RuntimeError(f"连不上调试通道：{type(last).__name__}: {last}")

    def send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                return msg

    def evaluate(self, expression: str) -> str:
        res = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return res["result"]["result"].get("value", "")

    def goto(self, url: str) -> None:
        self.send("Page.navigate", {"url": url})
        time.sleep(2.6)   # 等 apply(t) + 标签避让 + 镜头过渡跑完

    def shot(self, path: Path) -> None:
        res = self.send("Page.captureScreenshot", {"format": "png"})
        path.write_bytes(base64.b64decode(res["result"]["data"]))

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:
            pass


def inter(a: dict, b: dict) -> float:
    x = max(0.0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    y = max(0.0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    return x * y


def scan_once(cdp: CDP, url: str, label: str, shot_path: Path | None,
              check_bounds: bool = True) -> dict:
    cdp.goto(url)
    raw = cdp.evaluate(COLLECT_JS)
    if shot_path is not None:
        cdp.shot(shot_path)
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"label": label, "error": f"页面返回不可解析数据：{raw[:120]!r}", "problems": []}
    if "error" in data:
        return {"label": label, "error": data["error"], "problems": []}

    texts, circles, stage = data.get("texts") or [], data.get("circles") or [], data.get("stage")
    problems: list[dict] = []

    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            a, b = texts[i], texts[j]
            ov = inter(a, b)
            if ov <= 0:
                continue
            small = min(a["w"] * a["h"], b["w"] * b["h"]) or 1.0
            if ov / small > TEXT_OVERLAP_RATIO:
                problems.append({
                    "kind": "text-text",
                    "ratio": round(ov / small, 3),
                    "a": a["s"], "b": b["s"],
                    "xy": [a["x"], a["y"]],
                })

    for t in texts:
        for c in circles:
            if t.get("gid") == c.get("gid"):
                continue   # 同一个组里的图标与文字（徽标「!」+ 底色圆）本就叠着
            ov = inter(t, c)
            if ov <= 0:
                continue
            if ov / (t["w"] * t["h"] or 1.0) > ICON_OVERLAP_RATIO:
                problems.append({
                    "kind": "text-icon",
                    "ratio": round(ov / (t["w"] * t["h"]), 3),
                    "a": t["s"], "b": f"圆点<{c['p']}>",
                    "xy": [t["x"], t["y"]],
                })

    # 出界只在全景（static）下判：自动镜头是**主动**放大到局部，视口外的标签被裁掉
    # 是取景结果，不是排版缺陷（实测镜头帧能报出 20+ 条这类假阳性）。
    if stage and check_bounds:
        for t in texts:
            over_l = stage["x"] - t["x"]
            over_r = (t["x"] + t["w"]) - (stage["x"] + stage["w"])
            over_t = stage["y"] - t["y"]
            over_b = (t["y"] + t["h"]) - (stage["y"] + stage["h"])
            worst = max(over_l, over_r, over_t, over_b)
            if worst > OUT_OF_BOUNDS_PX:
                problems.append({
                    "kind": "out-of-bounds",
                    "ratio": round(worst, 1),
                    "a": t["s"], "b": "越出地图可视区",
                    "xy": [t["x"], t["y"]],
                })

    return {
        "label": label,
        "url": url,
        "viewport": data.get("viewport"),
        "stage": stage,
        "texts": len(texts),
        "circles": len(circles),
        "problems": problems,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="地图界面标签可读性自动检查")
    ap.add_argument("--url", default=None, help="直接给一个 file:// 或 http:// 地址")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=700)
    ap.add_argument("--with-cam", action="store_true", help="追加自动镜头帧（视野更紧，字更大）")
    ap.add_argument("--json", default=None, help="把结果写成 JSON 留证")
    ap.add_argument("--shots", default=None, help="截图输出目录（默认不截图）")
    ap.add_argument("--no-build", action="store_true", help="不重建 dist/map2d，直接用现有产物")
    args = ap.parse_args()

    if args.url is None:
        if not MAP_HTML.is_file():
            print(f"[FAIL] 找不到 {MAP_HTML}；先跑 python scripts/build_map2d.py")
            return 2
        if not args.no_build:
            build = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "build_map2d.py")],
                cwd=ROOT, capture_output=True, text=True,
            )
            if build.returncode != 0:
                print("[FAIL] 地图重建失败：")
                print((build.stderr or build.stdout)[-800:])
                return 2
        base = MAP_HTML.as_uri()
    else:
        base = args.url

    browser = find_browser()
    if not browser:
        print("[SKIP] 本机没有 Edge / Chrome，跳过界面检查（这一项不阻塞其它验收）")
        return 2

    shots_dir = Path(args.shots) if args.shots else None
    if shots_dir:
        shots_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[str, str]] = []
    for t in STATIC_TIMES:
        jobs.append((f"全景 t={t}", f"{base}?t={t}&static=1"))
    if args.with_cam:
        for t in CAM_TIMES:
            jobs.append((f"镜头 t={t}", f"{base}?t={t}&cam=1"))

    profile = Path(tempfile.mkdtemp(prefix="visual-check-"))
    results: list[dict] = []
    try:
        cdp = CDP(browser, args.width, args.height, profile)
        try:
            for idx, (label, url) in enumerate(jobs):
                shot = None
                if shots_dir:
                    shot = shots_dir / f"vc_{idx:02d}.png"
                results.append(scan_once(cdp, url, label, shot,
                                         check_bounds=not label.startswith("镜头")))
        finally:
            cdp.close()
    except Exception as exc:  # 浏览器起不来、CDP 断线
        print(f"[SKIP] 浏览器自动化不可用：{type(exc).__name__}: {exc}")
        return 2
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    empty = [r for r in results if not r.get("texts") and not r.get("error")]
    errors = [r for r in results if r.get("error")]
    total = sum(len(r["problems"]) for r in results)

    print(f"检查 {len(results)} 个时刻（{args.width}×{args.height}，{'含' if args.with_cam else '不含'}自动镜头）\n")
    first_vp = next((r.get("viewport") for r in results if r.get("viewport")), None)
    if first_vp:
        print(f"  实际视口 {first_vp[0]}×{first_vp[1]}（DPR {first_vp[2]}）")
        if first_vp[0] != args.width or first_vp[1] != args.height:
            print(f"  [WARN] 与请求的 {args.width}×{args.height} 不一致：窗口尺寸没生效，"
                  f"下面的坐标按实际视口理解")
        print()
    for r in results:
        if r.get("error"):
            print(f"  [ERR ] {r['label']}：{r['error']}")
            continue
        mark = "OK  " if not r["problems"] else "FAIL"
        print(f"  [{mark}] {r['label']}  文字 {r['texts']:>3} / 圆点 {r['circles']:>2} / 问题 {len(r['problems'])}")
        for p in r["problems"]:
            print(f"         {p['kind']:<13} {p['ratio']:>6}  {p['a']!r} × {p['b']!r}  @{p['xy']}")

    print()
    if errors:
        print(f"结论：{len(errors)} 个时刻页面没渲染出来，无法判定")
        code = 2
    elif total:
        print(f"结论：发现 {total} 处可读性问题")
        code = 1
    elif empty:
        print(f"结论：{len(empty)} 个时刻没有任何文字，检查本身可能失效")
        code = 2
    else:
        print(f"结论：{len(results)} 个时刻文字重叠 0、压图标 0、出界 0")
        code = 0

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "viewport": [args.width, args.height],
            "thresholds": {
                "text_text": TEXT_OVERLAP_RATIO,
                "text_icon": ICON_OVERLAP_RATIO,
                "out_of_bounds_px": OUT_OF_BOUNDS_PX,
            },
            "verdict": "pass" if code == 0 else ("fail" if code == 1 else "unavailable"),
            "results": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 已写入：{out}")

    return code


if __name__ == "__main__":
    raise SystemExit(main())
