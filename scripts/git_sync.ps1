# 一轮工作结束后：把工作区进度提交并推送到 origin（幂等、不空提交、不强制推送）
#
# 用法：
#   pwsh scripts/git_sync.ps1                                   # 自动生成提交信息
#   pwsh scripts/git_sync.ps1 -Message "feat(nexent): 上传技能"   # 指定提交信息
#   pwsh scripts/git_sync.ps1 -DryRun                           # 只看会提交什么
#   pwsh scripts/git_sync.ps1 -NoPush                           # 只提交不推送
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

$ErrorActionPreference = "Stop"

# 仓库根 = 本脚本所在目录的上一级
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function Fail([string]$text) {
    Write-Host "[git-sync] 失败：$text" -ForegroundColor Red
    exit 1
}

if (-not (git rev-parse --is-inside-work-tree 2>$null)) { Fail "not a git repository: $repo" }

if (-not $Branch) { $Branch = (git rev-parse --abbrev-ref HEAD).Trim() }
if (-not $Message) { $Message = "chore: sync progress ($(Get-Date -Format 'yyyy-MM-dd HH:mm'))" }

$dirty = (git status --porcelain | Measure-Object).Count
Write-Host "[git-sync] 分支 $Branch；待入库改动 $dirty 项"

if ($dirty -eq 0) {
    Write-Host "[git-sync] 工作区干净，没有需要提交的改动"
    if ($NoPush) { exit 0 }
    # 可能仍有本地提交未推送
    $ahead = (git rev-list --count "$Remote/$Branch..$Branch" 2>$null)
    if ($ahead -and [int]$ahead -gt 0) {
        Write-Host "[git-sync] 本地有 $ahead 个提交未推送，开始推送"
    } else {
        Write-Host "[git-sync] 与 $Remote/$Branch 已同步"
        exit 0
    }
}

if ($DryRun) {
    git status --short
    Write-Host "[git-sync] -DryRun：只预览，不提交、不推送"
    exit 0
}

if ($dirty -gt 0) {
    git add -A
    $staged = (git diff --cached --name-only | Measure-Object).Count
    if ($staged -eq 0) {
        Write-Host "[git-sync] 暂存区为空（可能全被 .gitignore 忽略），跳过提交"
    } else {
        git commit -m $Message
        if ($LASTEXITCODE -ne 0) { Fail "git commit 失败（退出码 $LASTEXITCODE）" }
    }
}

if ($NoPush) {
    Write-Host "[git-sync] -NoPush：已提交，未推送"
    exit 0
}

$hasUpstream = (git rev-parse --abbrev-ref '@{upstream}' 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $hasUpstream) {
    git push -u $Remote $Branch
} else {
    git push $Remote $Branch
}
if ($LASTEXITCODE -ne 0) {
    Fail "git push 失败（退出码 $LASTEXITCODE）；不要强推，先看错误再决定"
}

# AGENTS.md 要求：远程操作完成后记录分支和提交
$head = (git log -1 --format='%h %s').Trim()
$remoteHead = (git rev-parse --short "$Remote/$Branch" 2>$null)
Write-Host "[git-sync] 已推送：分支 $Branch 提交 $head；$Remote/$Branch=$remoteHead" -ForegroundColor Green
exit 0
