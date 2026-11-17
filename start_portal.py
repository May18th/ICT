"""本地一键启动门户（源码版，文案/逻辑实时生效）。

注意：不要用 `deploy-webdemo/` 那份打包副本演示——它是发布快照，页面文案会停在
构建时刻（踩过：改了源码但起的副本，看起来"口径没变"）。这里直接读 `src/`。

用法：
    python start_portal.py                 # 自动挑端口 + 开浏览器
    python start_portal.py --no-browser
    python start_portal.py --port 8791
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PREFERRED_PORT = 8791
PORT_SCAN = 60


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def port_in_use(port: int) -> bool:
    """判断端口是否已被占用。

    Windows 上「别人绑了 0.0.0.0:8791，我还能绑 127.0.0.1:8791」是允许的，
    只用 127.0.0.1 探测会误判成空闲（踩过：两个进程并存，请求打到谁看运气）。
    所以这里加 SO_EXCLUSIVEADDRUSE 并绑 0.0.0.0 探测。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except (AttributeError, OSError):
            pass
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.2)
        return client.connect_ex(("127.0.0.1", port)) == 0


def pick_port(start: int = PREFERRED_PORT, span: int = PORT_SCAN) -> int:
    for port in range(start, start + span):
        if not port_in_use(port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _parse_argv(argv: list[str]) -> tuple[int, bool]:
    port = 0
    open_browser = True
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--no-browser":
            open_browser = False
        elif arg == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
            i += 1
        elif arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])
        else:
            print(f"[warn] 忽略未知参数：{arg}")
        i += 1
    return port, open_browser


def main() -> None:
    _load_dotenv(ROOT / ".env")
    sys.path.insert(0, str(ROOT / "src"))

    wanted, open_browser = _parse_argv(sys.argv[1:])
    port = wanted if wanted else pick_port()
    if wanted and port_in_use(wanted):
        print(f"[err] 端口 {wanted} 已被占用，去掉 --port 让脚本自动挑一个")
        raise SystemExit(2)

    url = f"http://127.0.0.1:{port}/"
    print("=" * 60)
    print(f"  门户地址（源码版）：{url}")
    print("  页面：/workbench  /map2d  /data  /limits  /evolution")
    print("  Ctrl+C 停止服务")
    print("=" * 60)

    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    from decision_poc.web_demo import serve  # noqa: PLC0415

    serve("127.0.0.1", port)


if __name__ == "__main__":
    main()
