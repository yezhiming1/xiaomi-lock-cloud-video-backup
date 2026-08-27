$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Get-Command python -ErrorAction Stop
& $python.Source (Join-Path $PSScriptRoot "audit_public_tree.py")
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
