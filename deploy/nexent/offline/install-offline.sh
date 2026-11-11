#!/usr/bin/env bash
# 离线安装 Nexent 全栈 + urban-twin-mcp。
# 前置: 已执行 ./load-images.sh（或镜像已在本机）。
# 用法: ./install-offline.sh
#       NEXENT_DIR=/path/to/nexent ./install-offline.sh
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
NEXENT_DIR="${NEXENT_DIR:-$HERE/nexent}"

[ -d "$NEXENT_DIR/deploy" ] || { echo "[FATAL] 找不到 Nexent 源码: $NEXENT_DIR/deploy"; echo "        请用 NEXENT_DIR=... 指定，或把 Nexent v2.7.0 源码放到 $HERE/nexent"; exit 1; }

# --- 已知坑 1：OTEL 协议非法会让 config / runtime 启动即退出 ---
MON="$NEXENT_DIR/deploy/env/monitoring.env"
if [ -f "$MON" ] && grep -q '^OTEL_EXPORTER_OTLP_PROTOCOL="http"$' "$MON"; then
  echo "[fix] monitoring.env 的 OTEL 协议为 http，FastAPI 只接受 http/protobuf，自动修正"
  sed -i 's|^OTEL_EXPORTER_OTLP_PROTOCOL="http"$|OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"|' "$MON"
fi

echo "[1/3] 部署（首次约 4–8 分钟，取决于磁盘）"
cd "$NEXENT_DIR"
bash deploy.sh --defaults docker

echo
echo "[2/3] 等待服务就绪"
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:5013/nb/v1/health || true)
  [ "$code" = "200" ] && { echo "  北向 API 就绪"; break; }
  sleep 3
done

echo "[3/3] 健康检查"
bash "$HERE/verify-offline.sh"
