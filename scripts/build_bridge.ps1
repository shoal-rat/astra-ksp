param(
    [Parameter(Mandatory = $true)]
    [string]$KspRoot,
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$managedCandidates = @(
    (Join-Path $KspRoot "KSP_x64_Data\Managed"),
    (Join-Path $KspRoot "KSP_Data\Managed")
)
$managed = $managedCandidates | Where-Object { Test-Path (Join-Path $_ "Assembly-CSharp.dll") } | Select-Object -First 1
if (-not $managed) {
    throw "Could not find KSP managed assemblies under $KspRoot"
}

$env:KSP_MANAGED = $managed
$project = Join-Path $PSScriptRoot "..\csharp\KspAutomationBridge\KspAutomationBridge.csproj"
Write-Host "Using KSP managed assemblies: $managed"
Write-Host "Building $project"

$msbuild = (Get-Command msbuild -ErrorAction SilentlyContinue)
if ($msbuild) {
    & $msbuild.Source $project /p:Configuration=$Configuration /p:KSP_MANAGED="$managed"
} elseif (Get-Command dotnet -ErrorAction SilentlyContinue) {
    dotnet msbuild $project /p:Configuration=$Configuration /p:KSP_MANAGED="$managed"
} else {
    $cscCandidates = @(
        "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
    )
    $csc = $cscCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $csc) {
        throw "Could not find msbuild, dotnet, or .NET Framework csc.exe."
    }
    $projectDir = Split-Path $project
    $outputDir = Join-Path $projectDir "bin\$Configuration"
    New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
    $outputDll = Join-Path $outputDir "KspAutomationBridge.dll"
    # Reference Assembly-CSharp + every UnityEngine*.dll present in Managed. The in-game GUI +
    # crew-transfer code needs IMGUIModule (GUILayout), InputLegacyModule (F8), AnimationModule
    # (AddModApplication) and TextRenderingModule on top of the core/UI modules — referencing all
    # UnityEngine modules is the simplest robust way to cover them.
    $references = @("/reference:$(Join-Path $managed "Assembly-CSharp.dll")")
    Get-ChildItem -Path $managed -Filter "UnityEngine*.dll" | ForEach-Object {
        $references += "/reference:$($_.FullName)"
    }
    # MechJeb2: reference the installed plugin DLLs so the bridge can drive MechJeb's autopilots
    # (rendezvous / docking). Hard-referencing the installed DLL gives compile-time safety against
    # dev-build member renames. MechJebLib is referenced too so the compiler can resolve any
    # MechJeb public signatures that mention MechJebLib types.
    $mjPlugins = Join-Path $KspRoot "GameData\MechJeb2\Plugins"
    foreach ($mj in @("MechJeb2.dll", "MechJebLib.dll")) {
        $mjPath = Join-Path $mjPlugins $mj
        if (Test-Path $mjPath) {
            $references += "/reference:$mjPath"
        } else {
            Write-Warning "MechJeb DLL not found: $mjPath (MechJeb endpoints will fail to compile)"
        }
    }
    & $csc /nologo /target:library /out:$outputDll /nowarn:1701,1702 `
        $references `
        (Join-Path $projectDir "KspAutomationBridge.cs") `
        (Join-Path $projectDir "Properties\AssemblyInfo.cs")
    if ($LASTEXITCODE -ne 0) {
        throw "csc.exe failed with exit code $LASTEXITCODE"
    }
}

# Install the freshly-built DLL into GameData so KSP actually loads it. This step was MISSING: the
# build only compiled to bin\$Configuration, so KSP kept loading whatever stale DLL was last copied
# into GameData by hand — a new /mj-plan etc. would never appear in-game even after a restart. Always
# install here. (A KSP restart is still required to load a newly-installed bridge: plugins load once
# at startup.)
$builtDll = Join-Path (Split-Path $project) "bin\$Configuration\KspAutomationBridge.dll"
if (-not (Test-Path $builtDll)) {
    throw "Build reported success but the output DLL is missing: $builtDll"
}
$pluginDir = Join-Path $KspRoot "GameData\KspAutomationBridge\Plugins"
New-Item -ItemType Directory -Force -Path $pluginDir | Out-Null
Copy-Item -Path $builtDll -Destination (Join-Path $pluginDir "KspAutomationBridge.dll") -Force
Write-Host "Installed bridge DLL -> $pluginDir ($((Get-Item $builtDll).Length) bytes). Restart KSP to load it."
