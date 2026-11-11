#!/usr/bin/env bash
# 离线部署自检：镜像是否齐、容器是否起、两处 health 是否通。
# 退出码 0 = 全部通过；非 0 = 有失败项（逐条打印）。
set -u

FAIL=0
ok()   { echo "  [OK]   $1"; }
bad()  { echo "  [FAIL] $1"; FAIL=1; }

echo "== 1. 镜像 =="
for img in \
  nexent/nexent:v2.7.0 \
  nexent/nexent-web:v2.7.0 \
  nexent/nexent-data-process:v2.7.0 \
  nexent/nexent-sandbox:v2.7.0 \
  urban-twin-mcp:dev \
  docker.elastic.co/elasticsearch/elasticsearch:8.17.4 \
  postgres:15-alpine \
  supabase/postgres:15.8.1.060 \
  supabase/gotrue:v2.170.0 \
  kong:2.8.1 \
  redis:alpine \
  quay.io/minio/minio:RELEASE.2023-12-20T01-00-02Z \
  otel/opentelemetry-collector-contrib:0.151.0
do
  if docker image inspect "$img" >/dev/null 2>&1; then ok "$img"; else bad "$img 缺失（先跑 load-images.sh）"; fi
done

echo "== 2. 容器 =="
for c in nexent-config nexent-runtime nexent-northbound nexent-web nexent-elasticsearch nexent-minio nexent-postgresql nexent-redis supabase-auth-mini supabase-db-mini supabase-kong-mini urban-twin-mcp; do
  state=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
  if [ "$state" = "running" ]; then ok "$c running"; else bad "$c 状态=$state"; fi
done

echo "== 3. 服务端点 =="
check_http() {
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$1" 2>/dev/null || echo 000)
  if [ "$code" = "$2" ]; then ok "$1 -> $code"; else bad "$1 -> $code（期望 $2）"; fi
}
check_http "http://127.0.0.1:5013/nb/v1/health" 200
check_http "http://127.0.0.1:3000/zh" 200
check_http "http://127.0.0.1:18000/health" 200

echo "== 4. 断网证据 =="
pulling=$(docker ps -a --format '{{.Status}}' | grep -c 'Pulling fs layer' || true)
if [ "$pulling" = "0" ]; then ok "无容器处于拉取状态"; else bad "$pulling 个容器仍在拉取镜像（说明镜像不全）"; fi

echo
if [ "$FAIL" = "0" ]; then
  echo "全部通过。入口: http://localhost:3000"
  echo "账号: 见 <nexent>/deploy/env/.env 的 NEXENT_SUPER_ADMIN_PASSWORD"
else
  echo "有失败项，见上方 [FAIL]。常见原因见 OFFLINE.md 第 5 节。"
fi
exit "$FAIL"
