param(
    [string]$Project = "$PSScriptRoot\UrbanTwinDemo.uproject",
    [string]$Editor = "D:\UE_5.8\Engine\Binaries\Win64\UnrealEditor.exe"
)

if (-not (Test-Path -LiteralPath $Editor)) { throw "UnrealEditor.exe not found: $Editor" }
if (-not (Test-Path -LiteralPath $Project)) { throw "uproject not found: $Project" }

& $Editor $Project
