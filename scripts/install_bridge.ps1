param(
    [Parameter(Mandatory = $true)]
    [string]$KspRoot
)

$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "build_bridge.ps1") -KspRoot $KspRoot

$dll = Join-Path $PSScriptRoot "..\csharp\KspAutomationBridge\bin\Release\KspAutomationBridge.dll"
if (-not (Test-Path $dll)) {
    throw "Bridge DLL was not produced: $dll"
}

$target = Join-Path $KspRoot "GameData\KspAutomationBridge\Plugins"
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -Force $dll $target
Write-Host "Installed bridge to $target"

