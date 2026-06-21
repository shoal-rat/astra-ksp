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
    $references = @(
        "/reference:$(Join-Path $managed "Assembly-CSharp.dll")",
        "/reference:$(Join-Path $managed "UnityEngine.dll")",
        "/reference:$(Join-Path $managed "UnityEngine.CoreModule.dll")",
        "/reference:$(Join-Path $managed "UnityEngine.UI.dll")"
    )
    & $csc /nologo /target:library /out:$outputDll `
        $references `
        (Join-Path $projectDir "KspAutomationBridge.cs") `
        (Join-Path $projectDir "Properties\AssemblyInfo.cs")
    if ($LASTEXITCODE -ne 0) {
        throw "csc.exe failed with exit code $LASTEXITCODE"
    }
}
