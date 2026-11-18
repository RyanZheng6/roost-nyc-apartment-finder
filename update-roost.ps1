# Updates Roost from an official Roost ZIP without overwriting local data or secrets.
# Run from PowerShell after stopping the running Roost server with Ctrl+C.
param(
    [Parameter(Mandatory=$true)]
    [string]$ZipPath
)
$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$archive = (Resolve-Path -LiteralPath $ZipPath).Path
$backups = Join-Path $project 'backups'
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('roost-update-' + [guid]::NewGuid().ToString('N'))
try {
    if (-not (Test-Path -LiteralPath (Join-Path $project 'server.py'))) { throw 'Run this script from your existing apt-scout project folder.' }
    if (-not $archive.EndsWith('.zip', [StringComparison]::OrdinalIgnoreCase)) { throw 'Provide a Roost .zip release.' }
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $tmp -Force
    $src = Join-Path $tmp 'apt-scout'
    if (-not (Test-Path (Join-Path $src 'server.py'))) { throw 'Invalid release: apt-scout/server.py is missing.' }
    if (-not (Test-Path (Join-Path $src 'requirements.txt'))) { throw 'Invalid release: requirements.txt is missing.' }

    # Back up every persistent file before modifying application code.
    New-Item -ItemType Directory -Path $backups -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = Join-Path $backups $stamp
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    foreach ($file in @('.env','scout.db','scout.db-wal','scout.db-shm')) {
        $path = Join-Path $project $file
        if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination (Join-Path $backup $file) -Force }
    }
    Write-Host "Backup created: $backup" -ForegroundColor Green

    # Explicit allowlist: no .env, scout.db, .venv, or backups are copied from a release.
    foreach ($file in @('server.py','requirements.txt','README.md','.env.example','.gitignore')) {
        $from = Join-Path $src $file
        if (Test-Path -LiteralPath $from) { Copy-Item -LiteralPath $from -Destination (Join-Path $project $file) -Force }
    }
    foreach ($folder in @('static','tests')) {
        $from = Join-Path $src $folder
        if (Test-Path -LiteralPath $from) {
            $dest = Join-Path $project $folder
            New-Item -ItemType Directory -Path $dest -Force | Out-Null
            Copy-Item -Path (Join-Path $from '*') -Destination $dest -Recurse -Force
        }
    }
    $python = Join-Path $project '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $python) {
        Write-Host 'Installing/updating Python dependencies...'
        & $python -m pip install -r (Join-Path $project 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Your .env and database are unchanged; see backup.' }
    } else {
        Write-Warning 'No .venv found. Create a Python virtual environment and install requirements before starting Roost.'
    }
    Write-Host 'Update complete. Existing .env, scout.db, and .venv were preserved.' -ForegroundColor Green
    Write-Host 'Restart: .\.venv\Scripts\python.exe -m uvicorn server:app --reload'
} finally {
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
}
