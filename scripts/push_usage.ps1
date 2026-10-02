param([string]$Python = "python")

$ErrorActionPreference = "Continue"  # git writes warnings to stderr; exit codes are checked below
Set-Location (Split-Path -Parent $PSScriptRoot)
$logDir = Join-Path $env:LOCALAPPDATA "usage-card"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logPath = Join-Path $logDir "push.log"

& {
    Write-Output "[$(Get-Date -Format o)] Usage export started"
    try {
        git pull --rebase --autostash --quiet
        if ($LASTEXITCODE -ne 0) { throw "git pull failed with exit code $LASTEXITCODE" }
        & $Python scripts/usage_card.py export
        if ($LASTEXITCODE -ne 0) { throw "usage export failed with exit code $LASTEXITCODE" }
        git add data/usage.json
        if ($LASTEXITCODE -ne 0) { throw "git add failed with exit code $LASTEXITCODE" }
        git diff --cached --quiet
        if ($LASTEXITCODE -eq 0) { exit 0 }
        if ($LASTEXITCODE -ne 1) { throw "git diff failed with exit code $LASTEXITCODE" }
        git commit -m "Update usage data" --quiet
        if ($LASTEXITCODE -ne 0) { throw "git commit failed with exit code $LASTEXITCODE" }
        git push --quiet
        if ($LASTEXITCODE -ne 0) { throw "git push failed with exit code $LASTEXITCODE" }
    } catch {
        Write-Output $_
        exit 1
    }
} *>> $logPath
