$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PaddleClas = Join-Path $Root ".deps\PaddleClas"
$Config = Join-Path $Root "xiangqi_pplcnet_x1_0.yaml"
$BestModel = Join-Path $Root "output\xiangqi_pplcnet_x1_0\best_model"

Push-Location $Root
try {
    & $Python (Join-Path $PaddleClas "tools\export_model.py") `
        -c $Config `
        -o "Global.pretrained_model=$BestModel"
    exit $LASTEXITCODE
} finally {
    Pop-Location
}

