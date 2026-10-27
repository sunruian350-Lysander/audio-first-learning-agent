param(
    [string]$Python = "python",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    $venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host "Creating a Python environment (requires Python 3.11+)..."
        & $Python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Python setup failed. Install Python 3.11+ or pass -Python with its executable path." }
    }
    & $venvPython -c "import fastapi, uvicorn, pypdf, httpx, multipart" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing project dependencies..."
        & $venvPython -m pip install -c requirements-lock.txt -e .
        if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Check the connection and run .venv\Scripts\python.exe -m pip install -c requirements-lock.txt -e ." }
    }
    Write-Host "Open http://127.0.0.1:$Port in your browser. Press Ctrl+C to stop."
    & $venvPython -m uvicorn audio_learning.main:app --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw "The app stopped with an error." }
} finally {
    Pop-Location
}
