param(
    [switch]$Foreground,
    [switch]$Resume
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $Root "..\..")
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PaddleClas = Join-Path $Root ".deps\PaddleClas"
$Config = Join-Path $Root "xiangqi_pplcnet_x1_0.yaml"
$LogDir = Join-Path $Root "logs"
$LogFile = Join-Path $LogDir "train.log"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Training environment is missing. Run setup_env.ps1 first."
}
if (-not (Test-Path -LiteralPath (Join-Path $PaddleClas "tools\train.py"))) {
    throw "PaddleClas source is missing. Run setup_env.ps1 first."
}

& $Python (Join-Path $Root "prepare_dataset.py")
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Arguments = @(
    (Join-Path $PaddleClas "tools\train.py"),
    "-c", $Config
)
if ($Resume) {
    $Arguments += @("-o", "Global.checkpoints=./output/xiangqi_pplcnet_x1_0/latest")
}

Push-Location $Root
try {
    if ($Foreground) {
        & $Python @Arguments 2>&1 | Tee-Object -FilePath $LogFile
        exit $LASTEXITCODE
    }
    $Process = Start-Process -FilePath $Python -ArgumentList $Arguments `
        -WorkingDirectory $Root -RedirectStandardOutput $LogFile `
        -RedirectStandardError (Join-Path $LogDir "train.error.log") `
        -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath (Join-Path $LogDir "train.pid") -Value $Process.Id
    Write-Output "Training started. PID=$($Process.Id)"
    Write-Output "Log: $LogFile"
} finally {
    Pop-Location
}

