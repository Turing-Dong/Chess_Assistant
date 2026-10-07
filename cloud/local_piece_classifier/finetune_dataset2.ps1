param(
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PaddleClas = Join-Path $Root ".deps\PaddleClas"
$Config = Join-Path $Root "xiangqi_pplcnet_x1_0_dataset2.yaml"
$Output = "./output/xiangqi_pplcnet_x1_0_dataset2_augmented"
$LogDir = Join-Path $Root "logs"
$LogFile = Join-Path $LogDir "finetune_dataset2.log"
$ErrorLog = Join-Path $LogDir "finetune_dataset2.error.log"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Training environment is missing. Run setup_env.ps1 first."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Arguments = @(
    (Join-Path $PaddleClas "tools\train.py"),
    "-c", $Config
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
    Set-Content -LiteralPath (Join-Path $LogDir "finetune_dataset2.pid") -Value $Process.Id
    Write-Output "Dataset2 fine-tuning started. PID=$($Process.Id)"
    Write-Output "Log: $LogFile"
} finally {
    Pop-Location
}
