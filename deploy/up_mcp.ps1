# 一键起 MCP（SSE）：接管/重建 `urban-twin-mcp`，并核到"13 个工具都在"。
#
# 为什么需要这个脚本，而不是直接 `docker compose up -d`
# --------------------------------------------------------
# 编排文件里 `container_name: urban-twin-mcp` 是**固定名**（容器间用这个名字互访，
# Nexent 侧注册的地址就是 `http://urban-twin-mcp:8000/sse`），所以不能改名躲冲突。
# 而历史上这个容器是用裸 `docker run` 起的，没有 compose 标签，compose 见到同名容器
# 会直接报 "Conflict. The container name /urban-twin-mcp is already in use"，
# 一键部署当场失败。这个脚本补的就是这一步：识别"不是自己建的"同名容器，
# 先备份它的 inspect 快照，再删掉重建。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy/up_mcp.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy/up_mcp.ps1 -Rebuild
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy/up_mcp.ps1 -KeepOld   # 只体检、不动手
#
# 退出码：0 已就绪（健康 + 13 工具）；1 环境不满足；2 存在手工容器且 -KeepOld；
#         3 compose up 失败；4 健康检查超时；5 工具数不是 13。

[CmdletBinding()]
param(
    [switch]$Rebuild,
    [switch]$KeepOld,
    [int]$TimeoutSec = 180
)

$ErrorActionPreference = "Continue"

$Name = "urban-twin-mcp"
$Project = "urban-twin"
$ComposeFile = Join-Path $PSScriptRoot "docker-compose.mcp.yml"
$RepoRoot = Split-Path $PSScriptRoot -Parent
$BackupDir = Join-Path $RepoRoot "out/backup"

function Fail([int]$code, [string]$message) {
    Write-Host "[up] 失败：$message" -ForegroundColor Red
    exit $code
}

# --- 1 环境 -------------------------------------------------------------------
docker version --format "{{.Server.Version}}" *> $null
if ($LASTEXITCODE -ne 0) { Fail 1 "Docker 不可用（Docker Desktop 没启动或引擎没就绪）" }

docker network inspect nexent_network *> $null
if ($LASTEXITCODE -ne 0) { Fail 1 "找不到 nexent_network；先起 Nexent 全栈，容器才好在同一网络里互访" }

if (-not (Test-Path $ComposeFile)) { Fail 1 "编排文件不存在：$ComposeFile" }

# --- 2 同名容器归属判定 --------------------------------------------------------
$exists = $false
docker inspect $Name *> $null
if ($LASTEXITCODE -eq 0) { $exists = $true }

if ($exists) {
    # 用 --format '{{json .Config.Labels}}' 而不是 {{index .Config.Labels "..."}}：
    # PowerShell 5.1 往原生命令传参时会吃掉内层双引号，Go 模板直接报
    # `function "com" not defined`，结果拿到空值、把自家容器误判成手工容器。
    $labelsJson = (docker inspect --format '{{json .Config.Labels}}' $Name) -join ""
    $owner = ""
    if ($labelsJson.Trim()) {
        try {
            $labels = $labelsJson | ConvertFrom-Json
            if ($labels.PSObject.Properties.Name -contains "com.docker.compose.project") {
                $owner = [string]$labels."com.docker.compose.project"
            }
        }
        catch {
            Fail 1 "解析容器标签失败：$labelsJson"
        }
    }
    $owner = $owner.Trim()
    if ($owner -ne $Project) {
        Write-Host "[up] 发现同名容器但不是我方编排创建的（compose project='$owner'）" -ForegroundColor Yellow
        Write-Host "[up] 说明：这是历史裸 docker run 留下的容器，compose 会因固定容器名冲突而拒绝启动"

        if (-not (Test-Path $BackupDir)) { New-Item -ItemType Directory -Path $BackupDir -Force *> $null }
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $backup = Join-Path $BackupDir "$Name.manual.inspect.$stamp.json"
        docker inspect $Name | Out-File -FilePath $backup -Encoding utf8
        Write-Host "[up] 已备份容器配置到 $backup"

        if ($KeepOld) { Fail 2 "按 -KeepOld 要求保留手工容器，未做改动；确认可删后去掉该开关重跑" }

        docker rm -f $Name *> $null
        if ($LASTEXITCODE -ne 0) { Fail 3 "删除手工容器失败（可能被占用）" }
        Write-Host "[up] 已移除手工容器，接下来由 compose 接管"
    }
    else {
        Write-Host "[up] 已有 compose 管理的容器（project=$owner），直接校验/更新"
    }
}

# --- 3 构建（可选） ------------------------------------------------------------
if ($Rebuild) {
    Write-Host "[up] 重建镜像 urban-twin-mcp:dev"
    docker build -t urban-twin-mcp:dev -f (Join-Path $PSScriptRoot "mcp_sse/Dockerfile") $RepoRoot
    if ($LASTEXITCODE -ne 0) { Fail 3 "镜像构建失败" }
}

# --- 4 compose up（冲突兜底重试一次） ------------------------------------------
$upOutput = docker compose -f $ComposeFile up -d 2>&1
$upCode = $LASTEXITCODE
$upText = ($upOutput | Out-String)
if ($upCode -ne 0) {
    if ($upText -match "already in use") {
        Write-Host "[up] compose 仍报容器名冲突，清掉同名容器后重试一次" -ForegroundColor Yellow
        docker rm -f $Name *> $null
        $upOutput = docker compose -f $ComposeFile up -d 2>&1
        $upCode = $LASTEXITCODE
    }
}
if ($upCode -ne 0) {
    Write-Host $upOutput
    Fail 3 "docker compose up 失败（见上方输出）"
}
Write-Host "[up] docker compose up -d 完成"

# --- 5 等健康 -----------------------------------------------------------------
# 判据是 healthcheck（容器内 /health 的 tools_count == 13），不是"进程没死"。
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$status = ""
while ((Get-Date) -lt $deadline) {
    $status = (docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' $Name) -join ""
    $status = $status.Trim()
    if ($status -eq "healthy") { break }
    Start-Sleep -Seconds 3
}
if ($status -ne "healthy") {
    docker compose -f $ComposeFile logs --tail 40 mcp
    Fail 4 "健康检查未通过（最后状态 '$status'，等待 $TimeoutSec 秒）"
}
Write-Host "[up] 容器健康：healthy"

# --- 6 工具数核对 -------------------------------------------------------------
$count = (docker exec $Name python -c "import json,urllib.request;print(json.loads(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).read())['tools_count'])" 2>$null) -join ""
$count = $count.Trim()
if ($count -ne "13") {
    Fail 5 "工具数不是 13（实际 '$count'）：容器活着但工具没注册，平台侧只会表现为智能体变笨"
}
Write-Host "[up] 工具数 13/13；SSE 地址 http://urban-twin-mcp:8000/sse"
exit 0
