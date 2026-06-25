# Finalize the KspAutomationBridge DLL install once KSP has released the memory-mapped DLL.
#
# Why this exists: a running KSP keeps GameData\...\KspAutomationBridge.dll memory-mapped, so the
# freshly built DLL cannot overwrite it in place ("file with a user-mapped section open"). The build
# step therefore stages the new DLL as KspAutomationBridge.dll.new. Run THIS script after KSP exits
# (or let it wait) to atomically swap the staged DLL into place. A KSP restart then loads it.
param(
    [string]$KspRoot = "C:\Program Files (x86)\Steam\steamapps\common\Kerbal Space Program",
    [int]$WaitSeconds = 0   # >0 polls until KSP_x64 exits, up to this many seconds; 0 = swap now or fail.
)
$ErrorActionPreference = "Stop"
$plugins = Join-Path $KspRoot "GameData\KspAutomationBridge\Plugins"
$dll = Join-Path $plugins "KspAutomationBridge.dll"
$staged = "$dll.new"
if (-not (Test-Path $staged)) {
    Write-Host "No staged build ($staged); nothing to finalize. (Already installed?)"
    return
}

if ($WaitSeconds -gt 0) {
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ((Get-Process -Name "KSP_x64" -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
    }
}
if (Get-Process -Name "KSP_x64" -ErrorAction SilentlyContinue) {
    throw "KSP_x64 is still running; close it first (the DLL stays memory-mapped while it runs)."
}

# Back up the current DLL if no .bak yet, then swap the staged build in.
if ((Test-Path $dll) -and -not (Test-Path "$dll.bak")) {
    Copy-Item -Path $dll -Destination "$dll.bak" -Force
    Write-Host "Backed up existing DLL -> $dll.bak"
}
Copy-Item -Path $staged -Destination $dll -Force
Remove-Item -Path $staged -Force
Write-Host "Installed staged bridge DLL -> $dll ($((Get-Item $dll).Length) bytes). Start KSP to load it."
