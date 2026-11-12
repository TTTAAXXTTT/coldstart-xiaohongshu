param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if ($CheckOnly) {
    [pscustomobject]@{ status= $(if(Test-Path -LiteralPath $pythonPath){'environment_exists'}else{'needs_install'}); root=$PSScriptRoot; python=$pythonPath; requiredPython='3.12 recommended; core >=3.11'; requiredPillow='13.0.0'; startsApp=$false; downloads=$false } | ConvertTo-Json -Compress
    exit 0
}
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        & py -3.12 -m venv .venv
        if ($LASTEXITCODE -ne 0) { & python -m venv .venv }
    } else { & python -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw 'Python is required. Install Python 3.12, add it to PATH, then retry.' }
}
& $pythonPath -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required; Python 3.12 x64 is recommended. Rename an old .venv directory to keep a backup, then retry.' }
& $pythonPath -c "import importlib.util,sys; spec=importlib.util.find_spec('PIL'); sys.exit(1) if spec is None else None; import PIL; sys.exit(0 if PIL.__version__ == '13.0.0' else 1)"
if ($LASTEXITCODE -ne 0) {
    & $pythonPath -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Pillow installation failed. The app has not started. Check network access and retry; existing data is retained.' }
}
& $pythonPath (Join-Path $PSScriptRoot 'app.py')
