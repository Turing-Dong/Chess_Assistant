param(
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PaddleClas = Join-Path $Root ".deps\PaddleClas"
$Config = Join-Path $Root "xiangqi_pplcnet_x1_0.yaml"
$Pretrained = "./output/xiangqi_pplcnet_x1_0/best_model"
$Output = "./output/xiangqi_pplcnet_x1_0_finetune"
$LogDir = Join-Path $Root "logs"
$LogFile = Join-Path $LogDir "finetune.log"
$ErrorLog = Join-Path $LogDir "finetune.error.log"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Training environment is missing. Run setup_env.ps1 first."
}
if (-not (Test-Path -LiteralPath (Join-Path $Root ($Pretrained.TrimStart("./") + ".pdparams")))) {
    throw "Base best_model.pdparams is missing."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Arguments = @(
    (Join-Path $PaddleClas "tools\train.py"),
    "-c", $Config,
    "-o", "Global.pretrained_model=$Pretrained",
    "-o", "Global.output_dir=$Output",
    "-o", "Global.save_inference_dir=$Output/inference",
    "-o", "Global.epochs=40",
    "-o", "Optimizer.lr.learning_rate=0.001",
    "-o", "Optimizer.lr.warmup_epoch=2"
)

Push-Location $Root
try {
    if ($Foreground) {
        & $Python @Arguments 2>&1 | Tee-Object -FilePath $LogFile
        exit $LASTEXITCODE
    }
    $Process = Start-Process -FilePath $Python -ArgumentList $Arguments `
        -WorkingDirectory $Root -RedirectStandardOutput $LogFile `
        -RedirectStandardError $ErrorLog -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath (Join-Path $LogDir "finetune.pid") -Value $Process.Id
    Write-Output "Fine-tuning started. PID=$($Process.Id)"
    Write-Output "Log: $LogFile"
} finally {
    Pop-Location
}
