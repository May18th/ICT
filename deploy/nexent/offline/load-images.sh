#!/usr/bin/env bash
# 加载离线镜像。可在 Windows(Git Bash) / Linux / macOS 上运行。
# 用法: ./load-images.sh            使用同目录下 images/
#       IMAGE_DIR=/path ./load-images.sh
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
IMG_DIR="${IMAGE_DIR:-$HERE/images}"

command -v docker >/dev/null 2>&1 || { echo "[FATAL] 未找到 docker，请先安装 Docker 24+ / Docker Desktop"; exit 1; }
docker info >/dev/null 2>&1 || { echo "[FATAL] Docker 守护进程未运行，请先启动 Docker Desktop"; exit 1; }
[ -d "$IMG_DIR" ] || { echo "[FATAL] 镜像目录不存在: $IMG_DIR"; exit 1; }
[ -f "$IMG_DIR/SHA256SUMS" ] || { echo "[FATAL] 缺 $IMG_DIR/SHA256SUMS，拒绝加载未校验的镜像"; exit 1; }

echo "[1/2] 校验镜像完整性（约 4.6 GB）"
( cd "$IMG_DIR" && sha256sum -c SHA256SUMS ) || { echo "[FATAL] 镜像校验失败，不要继续，重新获取离线包"; exit 1; }

echo "[2/2] 加载镜像"
for f in "$IMG_DIR"/*.tar; do
  printf '  %-60s' "$(basename "$f")"
  docker load -i "$f" >/dev/null
  echo "ok"
done

echo
echo "已加载的镜像:"
docker images --format '{{.Repository}}:{{.Tag}}' | grep -E 'nexent|urban-twin|elasticsearch|postgres|redis|minio|kong|gotrue|opentelemetry' | sort | sed 's/^/  /'
echo
echo "完成。下一步: ./install-offline.sh"
