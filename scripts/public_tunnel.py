"""守护式公网通道：起服务 + 起隧道 + 断线自动重连 + 当前地址落盘。

为什么不用一条命令
-----------------
`cloudflared tunnel --url ...` 跑两三个小时会偶发掉线（网络抖动、quic 连接被回收），
掉线后那个窗口看着还在，队友那边却已经 530。录屏中途断一次就得重录。

这个脚本做三件事：
1. 先确认本地服务活着（没起就自己起，带平台凭据，走真链路）；
2. 隧道进程退出就立刻重连，重连拿到的新地址写进 `out/public_url.txt` 并在窗口里
   打一条醒目的「地址变了」，同时把四个页面地址一起列出来；
3. Ctrl+C 时把子进程一起收掉，不留孤儿进程占着端口。

用法：
    .venv/Scripts/python.exe scripts/public_tunnel.py
    .venv/Scripts/python.exe scripts/public_tunnel.py --port 18770

队友那边只需要 out/public_url.txt 里那一行 https 地址。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import re
import socket
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
URL_FILE = ROOT / "out" / "public_url.txt"
KEY_FILE = ROOT / "artifacts" / "nexent_key.env"
CLOUDFLARED = pathlib.Path(os.getenv("CLOUDFLARED", r"C:\Users\34984\cloudflared.exe"))
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _start_server(port: int) -> subprocess.Popen | None:
    """端口没监听时才起服务；带上平台凭据，否则工作台会退化成「离线直出」。"""

    if _port_open(port):
        _log(f"本地服务已在线：http://127.0.0.1:{port}")
        return None

    env = dict(os.environ)
    if KEY_FILE.exists():
        env["NEXENT_API_KEY"] = KEY_FILE.read_text(encoding="utf-8").strip()
        env.setdefault("NEXENT_BASE_URL", "http://127.0.0.1:5013")
        _log("已读到平台凭据，工作台提问走真实链路")
    else:
        _log("警告：没有 artifacts/nexent_key.env，工作台会退化成【离线直出】")

    log = open(ROOT / "out" / "serve_public.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "decision_poc.web_demo", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    for _ in range(30):
        if _port_open(port):
            _log(f"本地服务已启动：http://127.0.0.1:{port}")
            return proc
        time.sleep(0.5)
    raise SystemExit("服务起不来，看 out/serve_public.log")


def _write_url(url: str, first: bool) -> None:
    URL_FILE.parent.mkdir(parents=True, exist_ok=True)
    old = URL_FILE.read_text(encoding="utf-8").strip() if URL_FILE.exists() else ""
    URL_FILE.write_text(url + "\n", encoding="utf-8")

    bar = "=" * 62
    print(f"\n{bar}")
    if first:
        print("  公网地址已就绪，整条复制发给队友 B：")
    elif old and old != url:
        print("  注意：地址变了！旧地址已失效，把下面这条发过去（队友要重新打开）")
    else:
        print("  隧道重连成功，地址未变：")
    print(f"\n  {url}\n")
    print(f"  门户        {url}/")
    print(f"  大地图      {url}/map2d")
    print(f"  证据工作台  {url}/workbench")
    print(f"  能力边界    {url}/limits")
    print(f"\n  已写入 out/public_url.txt")
    print(f"{bar}\n", flush=True)


def _tunnel_once(port: int, got: dict) -> int:
    proc = subprocess.Popen(
        [str(CLOUDFLARED), "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stderr is not None
    for line in proc.stderr:
        m = URL_RE.search(line)
        if m and not got.get("url"):
            got["url"] = m.group(0)
            _write_url(m.group(0), first=not got.get("seen"))
            got["seen"] = True
    return proc.wait()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18770)
    args = ap.parse_args()

    if not CLOUDFLARED.exists():
        raise SystemExit(f"找不到 cloudflared：{CLOUDFLARED}\n用环境变量 CLOUDFLARED=<路径> 指定")

    server = _start_server(args.port)
    got: dict = {}
    try:
        while True:
            got["url"] = None
            code = _tunnel_once(args.port, got)
            _log(f"隧道断开（退出码 {code}），3 秒后重连；旧地址从此刻起失效")
            time.sleep(3)
    except KeyboardInterrupt:
        _log("收到 Ctrl+C，正在收尾")
    finally:
        if server is not None:
            server.terminate()
            _log("本地服务已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
