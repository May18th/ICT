# 一轮工作结束后：把工作区进度提交并推送到 origin（幂等、不空提交、不强制推送）
#
# 用法（本机只有 Windows PowerShell 5.1，没有 pwsh 7）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/git_sync.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/git_sync.ps1 -Message "feat(nexent): 上传技能"
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/git_sync.ps1 -PathList "AGENTS.md,scripts/git_sync.ps1"  # 并发时只提交自己的路径（逗号分隔，整体加引号）
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/git_sync.ps1 -DryRun
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/git_sync.ps1 -NoPush
#
# 退出码：0 已同步或无需同步；1 提交/推送失败（不会强推、不会 rebase）。

[CmdletBinding()]
param(
    [string]$Message = "",
    [string]$Remote = "origin",
    [string]$Branch = "",
    [string[]]$Paths = @(),
    [string]$PathList = "",
    [switch]$NoPush,
    [switch]$DryRun
)

# -File 调用时数组参数不好传，用 -PathList "a,b,c" 更稳；-Command 里也可直接用 -Paths @('a','b')。
if ($PathList.Trim()) {
    $Paths = @($PathList.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

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
    if ($Paths.Count -gt 0) {
        Write-Host "[git-sync] -Paths 指定：$($Paths -join ', ')"
        $statusOutput = & git status --short -- $Paths 2>&1
        foreach ($line in @($statusOutput)) { if ($null -ne $line) { Write-Host "$line" } }
    } elseif ($dirty -gt 0) {
        $statusOutput = & git status --short 2>&1
        foreach ($line in @($statusOutput)) { if ($null -ne $line) { Write-Host "$line" } }
    }
    Write-Host "[git-sync] -DryRun：只预览，不提交、不推送"
    exit 0
}

if ($dirty -gt 0) {
    if ($Paths.Count -gt 0) {
        # 并发场景：只提交自己负责的路径，别人的半成品留在工作区
        Write-Host "[git-sync] 只暂存指定路径：$($Paths -join ', ')"
        $addOutput = & git add -- $Paths 2>&1
        $addCode = $LASTEXITCODE
        foreach ($line in @($addOutput)) { if ($null -ne $line) { Write-Host "$line" } }
    } else {
        # 默认只暂存"已跟踪文件"的改动：临时脚本/变体/演示产物刚生成时不会被顺手带走。
        # 新增文件必须显式给 -PathList，脚本会把跳过的未跟踪文件列出来提醒。
        $addCode = Invoke-Git add -u
    }
    if ($addCode -ne 0) { Write-Host "[git-sync] 失败：git add 未成功"; exit 1 }
    $untracked = @(& git ls-files --others --exclude-standard)
    if ($untracked.Count -gt 0) {
        Write-Host "[git-sync] 注意：以下未跟踪文件未入库（新增文件请用 -PathList 显式指定）："
        foreach ($line in $untracked) { Write-Host "  ? $line" }
    }
    $staged = @(& git diff --cached --name-only).Count
    if ($staged -eq 0) {
        Write-Host "[git-sync] 暂存区为空（改动可能全被 .gitignore 忽略，或 -Paths 无匹配），跳过提交"
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
