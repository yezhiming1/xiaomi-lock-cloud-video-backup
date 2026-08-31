$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$image = "ghcr.io/home-assistant/home-assistant@sha256:6e8225ea9de2cfe9292b634e554ebbf439118ca0c823221d794298e7a74404bb"
$runIdentity = "$PID"
$lifecycleContainer = "xiaomi-lock-cloud-ha-test-$runIdentity"
$unitContainer = "xiaomi-lock-cloud-unit-test-$runIdentity"

$lifecycleArguments = @(
    "run",
    "--rm",
    "--name", $lifecycleContainer,
    "--network", "none",
    "--env", "PYTHONDONTWRITEBYTECODE=1",
    "--env", "PYTHONPATH=/work",
    "--volume", "${projectRoot}:/work:ro",
    "--entrypoint", "python",
    $image,
    "-B",
    "/work/scripts/ha_lifecycle_check.py"
)

& docker @lifecycleArguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$unitArguments = @(
    "run",
    "--rm",
    "--name", $unitContainer,
    "--network", "none",
    "--env", "PYTHONDONTWRITEBYTECODE=1",
    "--volume", "${projectRoot}:/work:ro",
    "--workdir", "/work",
    "--entrypoint", "python",
    $image,
    "-m", "unittest", "discover",
    "-s", "tests",
    "-p", "test_*.py",
    "-v"
)

& docker @unitArguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
