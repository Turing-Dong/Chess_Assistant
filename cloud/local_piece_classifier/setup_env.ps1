param(
    [string]$Python = "C:\ProgramData\anaconda3\python.exe"
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$PaddleClas = Join-Path $Root ".deps\PaddleClas"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    & $Python -m venv (Join-Path $Root ".venv")
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install paddlepaddle-gpu==3.3.0 `
    -i https://www.paddlepaddle.org.cn/packages/stable/cu126/

if (-not (Test-Path -LiteralPath (Join-Path $PaddleClas ".git"))) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $PaddleClas) | Out-Null
    try {
        git clone --depth 1 --branch release/2.6 https://gitee.com/paddlepaddle/PaddleClas.git $PaddleClas
    } catch {
        if (Test-Path -LiteralPath $PaddleClas) {
            Remove-Item -LiteralPath $PaddleClas -Recurse -Force
        }
        Write-Warning "Gitee unavailable, switching to GitHub."
        git clone --depth 1 --branch release/2.6 https://github.com/PaddlePaddle/PaddleClas.git $PaddleClas
    }
}

& $VenvPython -m pip install -r (Join-Path $PaddleClas "requirements.txt")
& $VenvPython -m pip install visualdl
& $VenvPython -c "import paddle; paddle.utils.run_check(); print('CUDA:', paddle.version.cuda()); print('GPU count:', paddle.device.cuda.device_count())"
