$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $root
try {
    $env:PYTHONPATH = Join-Path $root "src"
    python -m ksp_lab run --config configs/default.yaml --offline --mission "deliver payload to 80 km Kerbin orbit" --max-trials 5
} finally {
    Pop-Location
}

