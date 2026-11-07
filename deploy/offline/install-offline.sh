#!/usr/bin/env bash
# 离线部署：加载镜像 tar → 拉起全栈 → 健康检查
# 前提：目标机已装 Docker Engine / Docker Desktop，已取得本目录同级 images/*.tar
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGES_DIR="$HERE/images"
REPO_HINT="请把 Nexent 与 agent_poc 两个代码仓一并拷贝到离线机（compose 与 env 在仓库里）"

echo "== 1/4 加载镜像 =="
for tar in "$IMAGES_DIR"/*.tar; do
  echo "load $(basename "$tar")"
  docker load -i "$tar"
done

echo "== 2/4 校验镜像数量 =="
MISSING=0
while read -r img; do
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    echo "MISSING: $img"; MISSING=1
  fi
done < "$HERE/image-list.txt"
if [ "$MISSING" -ne 0 ]; then
  echo "镜像不齐，中止。确认 images/ 是否完整、image-list.txt 是否过期。"
  exit 1
fi
echo "镜像齐整"

echo "== 3/4 拉起服务 =="
echo "按 OFFLINE.md 第 3 节操作：先 Nexent 全栈（deploy.sh 选 general），再 urban-twin-mcp。"
echo "$REPO_HINT"

echo "== 4/4 健康检查（服务启动后手动执行）=="
cat <<'HC'
curl -s -o /dev/null -w "web=%{http_code}\n"  http://localhost:3000/zh        # 期望 200
curl -s http://localhost:18000/health                                    # urban-twin-mcp, 期望 tools_count=15
python -m decision_poc.nexent_urban                                      # 期望 8/8 一致
HC
echo "install-offline 脚本结束（服务拉起见 OFFLINE.md）"
