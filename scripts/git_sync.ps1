# 一轮工作结束后：把工作区进度提交并推送到 origin（幂等、不空提交、不强制推送）
#
# 用法（本机只有 Windows PowerShell 5.1，没有 pwsh 7）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/git_sync.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/git_sync.ps1 -Message "feat(nexent): 上传技能"
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/git_sync.ps1 -DryRun
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/git_sync.ps1 -NoPush
#
# 退出码：0 已同步或无需同步；1 提交/推送失败（不会强推、不会 rebase）。

[CmdletBinding()]
param(
    [string]$Message = "",
    [string]$Remote = "origin",
    [string]$Branch = "",
    [switch]$NoPush,
    [switch]$DryRun
)

# PowerShell 5.1 下 git 的进度/警告走 stderr：ErrorActionPreference 用 Continue，
# 失败一律靠退出码判断（下面每步都检查），不要靠异常。
$ErrorActionPreference = "Continue"

# 仓库根 = 本脚本所在目录的上一级
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# 统一执行 git：把 stderr 也当普通输出打印，避免 PowerShell 把进度信息显示成错误。
function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GitArgs)
    $output = & git @GitArgs 2>&1
    $code = $LASTEXITCODE
    foreach ($line in @($output)) { if ($null -ne $line) { Write-Host "$line" } }
    return $code
}

$inside = ((@(& git rev-parse --is-inside-work-tree 2>$null) -join "").Trim())
if ($inside -ne "true") {
    Write-Host "[git-sync] 失败：$repo 不是 git 仓库"
    exit 1
}

$currentBranch = (@(& git rev-parse --abbrev-ref HEAD) -join "").Trim()
if (-not $Branch) { $Branch = $currentBranch }
if (-not $Message) { $Message = "chore: sync progress ($(Get-Date -Format 'yyyy-MM-dd HH:mm'))" }

$dirty = @(& git status --porcelain).Count
$outstanding = @(& git rev-list --count "$Remote/$Branch..$Branch" 2>$null)
$ahead = if ($outstanding -and $outstanding[0] -match '^\d+$') { [int]$outstanding[0] } else { 0 }
Write-Host "[git-sync] 分支 $Branch；待入库改动 $dirty 项；未推送提交 $ahead 个"

if ($dirty -eq 0 -and $ahead -eq 0) {
    Write-Host "[git-sync] 工作区干净且与 $Remote/$Branch 已同步"
    exit 0
}

if ($DryRun) {
    if ($dirty -gt 0) { Invoke-Git status --short | Out-Null }
    Write-Host "[git-sync] -DryRun：只预览，不提交、不推送"
    exit 0
}

if ($dirty -gt 0) {
    if ((Invoke-Git add -A) -ne 0) { Write-Host "[git-sync] 失败：git add -A"; exit 1 }
    $staged = @(& git diff --cached --name-only).Count
    if ($staged -eq 0) {
        Write-Host "[git-sync] 暂存区为空（改动可能全被 .gitignore 忽略），跳过提交"
    } elseif ((Invoke-Git commit -m $Message) -ne 0) {
        Write-Host "[git-sync] 失败：git commit 未成功"
        exit 1
    }
}

if ($NoPush) {
    Write-Host "[git-sync] -NoPush：已提交，未推送"
    exit 0
}

if ((Invoke-Git push $Remote $Branch) -ne 0) {
    Write-Host "[git-sync] 失败：git push 未成功；不要强推，先看上面的错误再决定"
    exit 1
}

# AGENTS.md 要求：远程操作完成后记录分支和提交
$head = (@(& git log -1 --format='%h %s') -join "").Trim()
$remoteHead = ((@(& git rev-parse --short "$Remote/$Branch") -join "").Trim())
Write-Host "[git-sync] 已推送：分支 $Branch 提交 $head；$Remote/$Branch=$remoteHead"
exit 0
